from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_config

from symphony.config import HackConfig, IdeasConfig
from symphony.execution import ProviderSlots
from symphony.github import GitHubError
from symphony.hack_coordinator import HackCoordinator, HackError
from symphony.ideas_preview import PreviewEvidence
from symphony.models import ProviderOutcome
from symphony.providers.fake import FakeProvider
from symphony.providers.openhands import RESULT_MARKER
from symphony.store import Store
from symphony.workspace import WorkspaceManager


def git(root, *arguments, check=True):
    result = subprocess.run(["git", "-C", str(root), *arguments], text=True, capture_output=True, check=check)
    return result.stdout.strip() if check else result


class LocalHackBackend:
    def __init__(self, remote):
        self.remote = remote
        self.private = True
        self.publications = []

    def get_snapshot(self, repository, ref=None):
        commit = git(self.remote, "rev-parse", ref or "main", check=False)
        if commit.returncode:
            raise GitHubError("branch does not exist")

        def read(path):
            result = git(self.remote, "show", f"{commit.stdout.strip()}:{path}", check=False)
            return result.stdout.encode() if result.returncode == 0 else b""

        return {"repository": repository, "private": self.private, "default_branch": "main",
                "base_commit": commit.stdout.strip(), "board_content": read("hack/BOARD.md"),
                "lanes_content": read("hack/LANES.toml")}

    def publish_pr(self, campaign, body):
        if not self.publications:
            self.publications.append((campaign["branch"], body))
        return "https://example.test/solo/project/pull/1"


@pytest.fixture
def hack(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    (source / "README.md").write_text("Demo\n")
    (source / "hack").mkdir()
    (source / "hack/BOARD.md").write_text("# Board\n")
    git(source, "add", "--all")
    git(source, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "initial")
    git(source, "remote", "add", "origin", str(remote))
    git(source, "push", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    config = make_config(tmp_path)
    config = replace(config, hack=HackConfig(enabled=True, repositories=("solo/project",), milestone_every=100))
    cache = config.service.workspace_dir / "repositories/solo--project"
    cache.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", str(remote), str(cache)], check=True, capture_output=True)
    monkeypatch.setattr(WorkspaceManager, "github_remote", staticmethod(lambda repository: str(remote)))
    provider = FakeProvider("codex", write_files={
        "app/main.py": "print('demo')\n",
        "hack/LANES.toml": '[lanes.api]\npaths = ["app/**"]\n[lanes.ui]\npaths = ["ui/**"]\n',
        ".openhands/hack-gate.sh": "#!/bin/sh\nexit 0\n",
        ".symphony/idea.toml": 'provider = "codex"\n[preview]\nstart = ["python3", "-m", "http.server", "{port}"]\nport = 4317\nhealth_path = "/"\nstartup_timeout_seconds = 5\n',
    })
    store = Store(config.service.state_dir / "state.db")
    backend = LocalHackBackend(remote)
    coordinator = HackCoordinator(config, store, backend, {"codex": provider}, ProviderSlots({"codex": 3}, {"codex"}))

    def preview(worktree, runtime, sections, **kwargs):
        screenshots = {}
        for section in sections:
            relative = f"idea/assets/{section.slug}.png"
            target = worktree / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake screenshot")
            screenshots[section.slug] = relative
        return PreviewEvidence("http://127.0.0.1:9999/", screenshots, "")

    monkeypatch.setattr(coordinator.preview, "boot_and_capture", preview)
    return coordinator, provider, remote, source


def claim(coordinator):
    return coordinator.state.claim_next("test-worker", 30, 4, {"codex": 3}, reserve_slots=1)


def scaffold(coordinator):
    campaign = coordinator.start("solo/project")
    task = claim(coordinator)
    assert task["kind"] == "scaffold"
    assert coordinator.run_claimed(task)["state"] == "ready"
    coordinator.reconcile()
    assert coordinator.state.get_campaign(campaign["id"])["state"] == "active"
    return coordinator.state.get_campaign(campaign["id"])


def test_campaign_integrates_on_campaign_only_and_publishes_once(hack):
    coordinator, provider, remote, _ = hack
    original = git(remote, "rev-parse", "main")
    campaign = scaffold(coordinator)
    assert RESULT_MARKER in provider.starts[0][1]
    provider.write_files = {"app/feature.py": "FEATURE = True\n"}
    task = coordinator.state.enqueue_task(campaign["id"], "api-feature", "api", "Add feature", ["app/**"])
    assert coordinator.run_claimed(claim(coordinator))["state"] == "ready"
    coordinator.reconcile()
    assert coordinator.state.get_task(task["id"])["state"] == "merged"
    assert git(remote, "rev-parse", "main") == original
    assert git(remote, "show", f"{campaign['branch']}:app/feature.py") == "FEATURE = True"
    coordinator.stop("solo/project")
    coordinator.reconcile()
    provider.write_files = {}
    polish = claim(coordinator)
    assert polish["kind"] == "polish"
    assert coordinator.run_claimed(polish)["state"] == "ready"
    coordinator.reconcile()
    assert coordinator.state.get_campaign(campaign["id"])["state"] == "completed"
    assert len(coordinator.github.publications) == 1
    assert git(remote, "rev-parse", "main") == original
    assert "Campaign milestone" not in git(remote, "show", f"{campaign['branch']}:hack/BOARD.md")
    assert "hack/assets/milestone-3.png" in git(remote, "ls-tree", "-r", "--name-only", campaign["branch"])
    assert git(remote, "show", f"{campaign['branch']}:hack/STATUS.md").count("State: completed") == 1
    coordinator.reconcile()
    assert len(coordinator.github.publications) == 1


def test_two_claimed_lanes_rebase_into_one_demo_without_changing_main(hack):
    coordinator, provider, remote, _ = hack
    original = git(remote, "rev-parse", "main")
    campaign = scaffold(coordinator)
    coordinator.state.enqueue_task(campaign["id"], "api", "api", "Build the API", ["app/**"])
    coordinator.state.enqueue_task(campaign["id"], "ui", "ui", "Build the UI", ["ui/**"])
    first, second = claim(coordinator), claim(coordinator)
    assert first["base_commit"] == second["base_commit"]
    assert first["lane"] != second["lane"]
    for task in (first, second):
        path = "app/endpoint.py" if task["lane"] == "api" else "ui/screen.py"
        provider.write_files = {path: "READY = True\n"}
        assert coordinator.run_claimed(task)["state"] == "ready"
    coordinator.reconcile()
    assert all(coordinator.state.get_task(task["id"])["state"] == "merged" for task in (first, second))
    assert git(remote, "show", f"{campaign['branch']}:app/endpoint.py") == "READY = True"
    assert git(remote, "show", f"{campaign['branch']}:ui/screen.py") == "READY = True"
    assert git(remote, "rev-parse", "main") == original


def test_lane_outside_footprint_is_blocked_without_push(hack):
    coordinator, provider, remote, _ = hack
    campaign = scaffold(coordinator)
    provider.write_files = {"ui/stolen.py": "outside lane"}
    task = coordinator.state.enqueue_task(campaign["id"], "api", "api", "API work", ["app/**"])
    result = coordinator.run_claimed(claim(coordinator))
    assert result["state"] == "blocked"
    assert "outside its footprint" in result["error"]
    coordinator.reconcile()
    assert git(remote, "show", f"{campaign['branch']}:ui/stolen.py", check=False).returncode != 0
    assert "blocked" in git(remote, "show", f"{campaign['branch']}:hack/STATUS.md")
    assert coordinator.state.get_task(task["id"])["attempt"] == 1
    assert claim(coordinator) is None


def test_failed_gate_never_reaches_campaign(hack):
    coordinator, provider, remote, _ = hack
    campaign = scaffold(coordinator)
    provider.write_files = {"app/feature.py": "broken"}
    task = coordinator.state.enqueue_task(campaign["id"], "api", "api", "API work", ["app/**"])
    coordinator.run_claimed(claim(coordinator))
    coordinator._fast_gate = lambda *args, **kwargs: (_ for _ in ()).throw(HackError("boot failed"))
    coordinator.reconcile()
    assert coordinator.state.get_task(task["id"])["state"] == "blocked"
    assert git(remote, "show", f"{campaign['branch']}:app/feature.py", check=False).returncode != 0


def test_expiry_cancels_provider_and_retains_fence_when_cancellation_fails(hack):
    coordinator, provider, _, _ = hack
    campaign = scaffold(coordinator)
    task = coordinator.state.enqueue_task(campaign["id"], "api", "api", "API work", ["app/**"])
    task = claim(coordinator)
    coordinator.state.update_task(task["id"], conversation_id="running-conversation")
    coordinator.state.update_campaign(campaign["id"], expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    original_cancel = provider.cancel
    provider.cancel = lambda run: (_ for _ in ()).throw(RuntimeError("cancellation failed"))
    coordinator.reconcile()
    assert coordinator.state.active_campaign("solo/project") is not None
    assert coordinator.state.get_task(task["id"])["state"] == "running"
    provider.cancel = original_cancel
    coordinator.reconcile()
    assert "running-conversation" in provider.cancels
    assert coordinator.state.get_campaign(campaign["id"])["state"] == "completed"
    assert all(task["kind"] != "polish" for task in coordinator.state.list_tasks(campaign["id"]))


def test_dispatcher_splits_cross_lane_contract_before_implementations(hack):
    coordinator, provider, _, source = hack
    campaign = scaffold(coordinator)
    (source / "hack/BOARD.md").write_text("## Cross-lane\n- [ ] [login] Build login end to end\n")
    git(source, "add", "hack/BOARD.md")
    git(source, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "steer")
    git(source, "push", "origin", "main")
    coordinator.reconcile()
    dispatcher = claim(coordinator)
    assert dispatcher["kind"] == "dispatcher"
    provider.write_files = {"hack/dispatch.json": json.dumps({"board": (
        "## api\n- [ ] [login-contract] Define login interface\n"
        "- [ ] [login-api] Implement API <!-- depends: login-contract -->\n"
        "## ui\n- [ ] [login-ui] Implement UI <!-- depends: login-contract -->\n"
    )})}
    assert coordinator.run_claimed(dispatcher)["state"] == "merged"
    contract = claim(coordinator)
    assert contract["key"] == "login-contract"
    assert claim(coordinator) is None
    assert "contract task" in provider.starts[-1][1]
    assert RESULT_MARKER in provider.starts[-1][1]
    assert coordinator.state.get_campaign(campaign["id"])["board_hash"]


def test_publication_checks_private_before_any_push(hack, monkeypatch):
    coordinator, _, _, _ = hack
    campaign = scaffold(coordinator)
    coordinator.github.private = False
    pushes = []
    monkeypatch.setattr(coordinator.workspaces, "push", lambda *args: pushes.append(args))
    with pytest.raises(HackError, match="private"):
        coordinator._publish(campaign)
    assert pushes == []


def test_provider_question_is_reported_without_automatic_retry(hack):
    coordinator, provider, remote, _ = hack
    campaign = scaffold(coordinator)
    provider.write_files = {}
    provider.outcome = ProviderOutcome.NEEDS_GUIDANCE
    coordinator.state.enqueue_task(campaign["id"], "api", "api", "API work", ["app/**"])
    assert coordinator.run_claimed(claim(coordinator))["state"] == "question"
    coordinator.reconcile()
    assert "Choose option A or option B" in git(remote, "show", f"{campaign['branch']}:hack/STATUS.md")
    assert claim(coordinator) is None


def test_scaffold_question_closes_campaign_without_stranding_home_intake(hack):
    coordinator, provider, remote, _ = hack
    original = git(remote, "rev-parse", "main")
    provider.outcome = ProviderOutcome.NEEDS_GUIDANCE
    campaign = coordinator.start("solo/project")
    assert coordinator.run_claimed(claim(coordinator))["state"] == "question"
    coordinator.reconcile()
    assert coordinator.state.get_campaign(campaign["id"])["state"] == "failed"
    assert not coordinator.state.store.hack_active("solo/project")
    assert git(remote, "rev-parse", "main") == original
    assert coordinator.github.publications == []


def test_prepared_candidate_retries_same_commit_after_lost_push_response(hack, monkeypatch):
    coordinator, provider, remote, _ = hack
    campaign = scaffold(coordinator)
    provider.write_files = {"app/feature.py": "safe feature\n"}
    task = coordinator.state.enqueue_task(campaign["id"], "api", "api", "API work", ["app/**"])
    coordinator.run_claimed(claim(coordinator))
    original_push = coordinator.workspaces.push

    def lost_response(worktree, repository, branch):
        original_push(worktree, repository, branch)
        raise OSError("response lost after successful push")

    monkeypatch.setattr(coordinator.workspaces, "push", lost_response)
    coordinator.reconcile()
    pending = coordinator.state.get_task(task["id"])
    assert pending["state"] == "ready"
    assert pending["prepared_commit"] == git(remote, "rev-parse", campaign["branch"])
    monkeypatch.setattr(coordinator.workspaces, "push", original_push)
    coordinator.reconcile()
    assert coordinator.state.get_task(task["id"])["state"] == "merged"
    assert coordinator.state.get_task(task["id"])["result_commit"] == pending["prepared_commit"]
    assert len(provider.starts) == 2


def test_prepared_candidate_retries_without_rerunning_provider_or_gate(hack, monkeypatch):
    coordinator, provider, _, _ = hack
    campaign = scaffold(coordinator)
    provider.write_files = {"app/feature.py": "safe feature\n"}
    task = coordinator.state.enqueue_task(campaign["id"], "api", "api", "API work", ["app/**"])
    coordinator.run_claimed(claim(coordinator))
    original_push = coordinator.workspaces.push
    monkeypatch.setattr(coordinator.workspaces, "push", lambda *args: (_ for _ in ()).throw(OSError("network down")))
    coordinator.reconcile()
    pending = coordinator.state.get_task(task["id"])
    assert pending["prepared_commit"]
    monkeypatch.setattr(coordinator.workspaces, "push", original_push)
    monkeypatch.setattr(coordinator, "_fast_gate", lambda *args, **kwargs: pytest.fail("prepared candidate must not rerun gate"))
    coordinator.reconcile()
    assert coordinator.state.get_task(task["id"])["state"] == "merged"
    assert len(provider.starts) == 2


def test_revoked_opt_in_releases_home_tier_without_pushing(hack, monkeypatch):
    coordinator, _, _, _ = hack
    campaign = scaffold(coordinator)
    coordinator.state.enqueue_task(campaign["id"], "api", "api", "API work", ["app/**"])
    coordinator.config = replace(coordinator.config, hack=replace(coordinator.config.hack, enabled=False))
    monkeypatch.setattr(coordinator.workspaces, "push", lambda *args: pytest.fail("revoked campaign must not push"))
    coordinator.reconcile()
    assert coordinator.state.active_campaign("solo/project") is None
    assert coordinator.state.get_campaign(campaign["id"])["state"] == "failed"


def test_guarded_ideas_publication_pushes_main_once_after_campaign(hack):
    coordinator, _, remote, _ = hack
    coordinator.config = replace(coordinator.config,
                                 github=replace(coordinator.config.github, allowed_repositories=()),
                                 ideas=IdeasConfig(repositories=("solo/project",)),
                                 hack=replace(coordinator.config.hack, publish_ideas=True))
    original = git(remote, "rev-parse", "main")
    campaign = scaffold(coordinator)
    assert git(remote, "rev-parse", "main") == original
    coordinator.state.update_campaign(campaign["id"], expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    coordinator.reconcile()
    completed = coordinator.state.get_campaign(campaign["id"])
    assert completed["state"] == "completed"
    assert git(remote, "rev-parse", "main") == completed["result_commit"]
    assert coordinator.github.publications == []
    request = coordinator.preview_deployments.queued_request("solo/project")
    assert request.commit == completed["result_commit"]
    coordinator.reconcile()
    assert git(remote, "rev-parse", "main") == completed["result_commit"]


def test_old_worker_cannot_overwrite_recovered_task(hack, monkeypatch):
    coordinator, provider, _, _ = hack
    campaign = scaffold(coordinator)
    provider.write_files = {"app/feature.py": "late feature\n"}
    task = coordinator.state.enqueue_task(campaign["id"], "api", "api", "API work", ["app/**"])
    running = claim(coordinator)
    original_wait = provider.wait

    def recover_while_waiting(run, timeout):
        coordinator.state.finish_task(task["id"], "blocked", note="Recovered separately")
        return original_wait(run, timeout)

    monkeypatch.setattr(provider, "wait", recover_while_waiting)
    result = coordinator.run_claimed(running)
    assert result["state"] == "blocked"
    assert result["note"] == "Recovered separately"
    assert result["result_commit"] is None


def test_stale_dispatcher_cannot_apply_removed_board_tasks(hack, monkeypatch):
    coordinator, provider, _, source = hack
    campaign = scaffold(coordinator)
    board = source / "hack/BOARD.md"
    board.write_text("## api\n- [ ] [old] Old task\n")
    git(source, "add", "hack/BOARD.md")
    git(source, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "old board")
    git(source, "push", "origin", "main")
    coordinator.reconcile()
    dispatcher = claim(coordinator)
    provider.write_files = {"hack/dispatch.json": json.dumps({"board": "## api\n- [ ] [old] Old task\n"})}
    original_wait = provider.wait

    def edit_while_dispatching(run, timeout):
        board.write_text("## api\n")
        git(source, "add", "hack/BOARD.md")
        git(source, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "remove task")
        git(source, "push", "origin", "main")
        return original_wait(run, timeout)

    monkeypatch.setattr(provider, "wait", edit_while_dispatching)
    result = coordinator.run_claimed(dispatcher)
    assert result["state"] == "canceled"
    assert not any(task["kind"] == "lane" for task in coordinator.state.list_tasks(campaign["id"]))


def test_director_retry_discards_failed_worktree_before_new_base(hack):
    coordinator, provider, remote, _ = hack
    campaign = scaffold(coordinator)
    provider.write_files = {"ui/wrong.py": "bad leftover\n"}
    coordinator.state.sync_board(campaign["id"], "## api\n- [ ] [api] API work\n", {"api": ["app/**"]})
    running = claim(coordinator)
    assert coordinator.run_claimed(running)["state"] == "blocked"
    coordinator.reconcile()
    coordinator.state.sync_board(campaign["id"], "## api\n- [ ] [api] API work, clarified\n", {"api": ["app/**"]})
    provider.write_files = {"app/feature.py": "correct feature\n"}
    retried = claim(coordinator)
    assert retried["id"] == running["id"]
    assert retried["attempt"] == 2
    assert coordinator.run_claimed(retried)["state"] == "ready"
    coordinator.reconcile()
    assert coordinator.state.get_task(running["id"])["state"] == "merged"
    assert git(remote, "show", f"{campaign['branch']}:ui/wrong.py", check=False).returncode != 0
    assert git(remote, "show", f"{campaign['branch']}:app/feature.py") == "correct feature"


def test_restart_after_scaffold_merged_repairs_lifecycle(hack):
    coordinator, _, _, _ = hack
    campaign = scaffold(coordinator)
    coordinator.state.update_campaign(campaign["id"], state="starting")
    coordinator.reconcile()
    assert coordinator.state.get_campaign(campaign["id"])["state"] == "active"


def test_restoring_board_after_empty_revision_dispatches_again(hack):
    coordinator, provider, _, source = hack
    campaign = scaffold(coordinator)
    content = "## api\n- [ ] [api] API work\n"

    def push_board(value):
        (source / "hack/BOARD.md").write_text(value)
        git(source, "add", "hack/BOARD.md")
        git(source, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "steer board")
        git(source, "push", "origin", "main")
        coordinator.reconcile()

    provider.write_files = {"hack/dispatch.json": json.dumps({"board": content})}
    push_board(content)
    first = claim(coordinator)
    assert coordinator.run_claimed(first)["state"] == "merged"
    push_board("## api\n")
    assert not any(task["state"] == "queued" for task in coordinator.state.list_tasks(campaign["id"]))
    push_board(content)
    restored = claim(coordinator)
    assert restored["kind"] == "dispatcher"
    assert restored["id"] != first["id"]
    assert coordinator.run_claimed(restored)["state"] == "merged"
    assert claim(coordinator)["key"] == "api"
