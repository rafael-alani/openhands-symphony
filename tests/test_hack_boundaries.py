from __future__ import annotations

import json
from dataclasses import replace

import pytest
from conftest import FakeGitHub, issue, make_config
from test_graduation import FakeGraduationBackend, _progress, _snapshot, _spec, _write_config
from test_ideas import RUNTIME, SPEC
from test_preview_manager import FakePreviewManager
from test_vault import mode
from test_vault import setup as setup_vault

from symphony import cli
from symphony.config import HackConfig, IdeasConfig, load_config
from symphony.coordinator import Coordinator, IntakeError
from symphony.execution import ProviderSlots
from symphony.graduation import GraduationError, Graduator
from symphony.hack_store import HackStore
from symphony.ideas_contract import git_blob_hash, parse_runtime
from symphony.ideas_coordinator import IdeasCoordinator, IdeasIntakeError
from symphony.models import IdeaRunState, IdeaSnapshot
from symphony.providers.fake import FakeProvider
from symphony.store import Store


def _start(store, repository, home_tier):
    return HackStore(store).start_campaign(
        repository=repository, home_tier=home_tier, default_branch="main",
        base_commit="a" * 40, provider="codex", hours=1,
    )


class IdeaBackend:
    def __init__(self):
        self.observed = []

    def get_snapshot(self, repository, spec_path, progress_path):
        self.observed.append(repository)
        spec = SPEC.replace(b"solo/idea", repository.encode())
        return IdeaSnapshot(repository, git_blob_hash(spec), spec, RUNTIME, b"", "a" * 40, "main", True)


def _coordinators(tmp_path):
    config = replace(
        make_config(tmp_path), ideas=IdeasConfig(("solo/idea",)),
        hack=HackConfig(enabled=True, repositories=("solo/idea", "solo/project")),
    )
    store = Store(config.service.state_dir / "state.db")
    providers = {"codex": FakeProvider("codex")}
    slots = ProviderSlots(config.scheduler.provider_concurrency, set(providers))
    github = FakeGitHub([issue(), issue(number=2)])
    coordinator = Coordinator(config, store, github, providers, slots)
    ideas = IdeasCoordinator(config, store, IdeaBackend(), providers, slots)
    return config, store, coordinator, ideas


def test_active_campaign_suspends_issue_and_idea_intake_and_preserves_queued_work(tmp_path):
    config, store, coordinator, ideas = _coordinators(tmp_path)
    job, _ = coordinator.enqueue(issue())
    run, _ = ideas.observe_repository("solo/idea")
    github_campaign = _start(store, "solo/project", "github")
    ideas_campaign = _start(store, "solo/idea", "idea")

    with pytest.raises(IntakeError, match="active hack campaign"):
        coordinator.enqueue(issue(number=2))
    with pytest.raises(IdeasIntakeError, match="active hack campaign"):
        ideas.observe_repository("solo/idea")
    assert store.claim_next("issue-worker", 60, 3, {"codex": 2}) is None
    assert store.claim_next_idea("idea-worker", 60, 3, {"codex": 2}) is None
    assert len(store.list_jobs()) == len(store.list_idea_runs()) == 1

    state = HackStore(store)
    state.update_campaign(github_campaign["id"], state="completed")
    state.update_campaign(ideas_campaign["id"], state="completed")
    assert store.claim_next("issue-worker", 60, 3, {"codex": 2}).id == job.id
    assert store.claim_next_idea("idea-worker", 60, 3, {"codex": 2}).id == run.id


@pytest.mark.parametrize("change", ["spec", "mode", "missing", "duplicate", "unavailable", "reconcile-error"])
def test_vault_freezes_spec_and_home_mode_until_campaign_closes(tmp_path, monkeypatch, change):
    bridge, note, store, backend = setup_vault(tmp_path)
    assert bridge.reconcile() == []
    before = store.vault_projects()[0]
    repository = before["repository"]
    original_spec = backend.specs[repository]
    calls_before = list(backend.calls)
    campaign = _start(store, repository, "idea")
    if change == "spec":
        note.write_text(note.read_text() + "\nBuild a different app.")
    elif change == "mode":
        mode(note, "github")
    elif change == "missing":
        note.unlink()
    elif change == "duplicate":
        note.with_name("duplicate.md").write_bytes(note.read_bytes())
    elif change == "unavailable":
        note.parent.rename(note.parent.with_name("temporarily-unavailable"))
    else:
        monkeypatch.setattr(bridge, "_reconcile", lambda: (_ for _ in ()).throw(OSError("unavailable")))

    bridge.reconcile()

    project = store.vault_projects()[0]
    assert (project["mode"], project["desired_mode"]) == (before["mode"], before["desired_mode"])
    assert backend.specs[repository] == original_spec
    assert backend.calls == calls_before
    assert backend.labels == []
    if change in {"spec", "mode"}:
        HackStore(store).update_campaign(campaign["id"], state="completed")
        assert bridge.reconcile() == []
        if change == "spec":
            assert backend.specs[repository] != original_spec
        else:
            assert store.vault_projects()[0]["mode"] == "github"


def test_ideas_reconcile_keeps_campaign_preview_authority_without_redeploying_old_main(tmp_path, monkeypatch):
    config, store, _, ideas = _coordinators(tmp_path)
    run, _ = ideas.observe_repository("solo/idea")
    claimed = store.claim_next_idea("worker", 60, 3, {"codex": 2})
    assert claimed.id == run.id
    store.transition_idea_run(run.id, IdeaRunState.PUBLISHED, published_commit="b" * 40)
    _start(store, "solo/idea", "idea")
    _start(store, "solo/project", "github")
    ideas.github.observed.clear()
    deployments = []
    monkeypatch.setattr(ideas, "_dispatch_preview", lambda old: deployments.append(old.published_commit))

    assert ideas.reconcile() == []

    allowlist = json.loads((config.service.preview_dir / "control/allowlist.json").read_text())
    assert set(allowlist["repositories"]) == {"solo/idea", "solo/project"}
    assert ideas.github.observed == []
    assert deployments == []
    assert len(store.list_idea_runs()) == 1


def test_completed_campaign_demo_is_not_rolled_back_by_old_ideas_run(tmp_path, monkeypatch):
    from symphony.preview_queue import PreviewQueue

    config, store, _, ideas = _coordinators(tmp_path)
    run, _ = ideas.observe_repository("solo/idea")
    store.claim_next_idea("worker", 60, 3, {"codex": 2})
    old = store.transition_idea_run(run.id, IdeaRunState.PUBLISHED, published_commit="b" * 40)
    campaign = _start(store, "solo/idea", "idea")
    HackStore(store).update_campaign(campaign["id"], state="completed", result_commit="c" * 40)
    enqueued = []
    monkeypatch.setattr(PreviewQueue, "enqueue", lambda self, run, script: enqueued.append(run.published_commit))
    ideas._dispatch_preview(old)
    assert enqueued == []
    # A subsequent Ideas publication becomes authoritative again.
    newer = replace(old, published_commit="d" * 40, finished_at="9999-01-01T00:00:00+00:00")
    ideas._dispatch_preview(newer)
    assert enqueued == ["d" * 40]


def test_campaign_preview_health_is_not_reported_pending_for_an_old_ideas_commit(tmp_path, monkeypatch):
    from symphony.preview_queue import PreviewStatus

    config, store, _, ideas = _coordinators(tmp_path)
    run, _ = ideas.observe_repository("solo/idea")
    store.claim_next_idea("worker", 60, 3, {"codex": 2})
    store.transition_idea_run(run.id, IdeaRunState.PUBLISHED, published_commit="b" * 40)
    campaign = _start(store, "solo/idea", "idea")
    HackStore(store).update_campaign(campaign["id"], state="completed", result_commit="c" * 40)
    monkeypatch.setattr(ideas.preview_deployments, "status", lambda repo: PreviewStatus(
        repo, "c" * 40, "c" * 40, "c" * 40, "healthy", 10000, "http://localhost:10000/", "", "now",
    ))
    ideas.preview_deployments.sync_store(store, config.ideas.repositories)
    assert store.get_idea_project("solo/idea").preview_state == "healthy"


def test_completed_preview_handoff_retries_but_yields_to_newer_work(tmp_path, monkeypatch):
    config, store, _, ideas = _coordinators(tmp_path)
    run, _ = ideas.observe_repository("solo/idea")
    store.claim_next_idea("worker", 60, 3, {"codex": 2})
    old = store.transition_idea_run(run.id, IdeaRunState.PUBLISHED, published_commit="b" * 40)
    campaign = _start(store, "solo/idea", "idea")
    worktree = tmp_path / "campaign"
    (worktree / ".openhands").mkdir(parents=True)
    (worktree / ".openhands/setup.sh").write_text("#!/bin/sh\n")
    HackStore(store).update_campaign(campaign["id"], state="completed", result_commit="c" * 40, worktree=str(worktree))
    enqueued = []
    monkeypatch.setattr(ideas.preview_deployments, "enqueue", lambda run, script: enqueued.append((run.published_commit, script)) or True)
    assert ideas.preview_deployments.recover_campaign_previews(config, store)
    assert enqueued == [("c" * 40, ".openhands/setup.sh")]
    monkeypatch.setattr(store, "latest_published_idea_run", lambda repo: replace(old, finished_at="9999-01-01T00:00:00+00:00"))
    assert ideas.preview_deployments.recover_campaign_previews(config, store) == []
    monkeypatch.setattr(store, "latest_published_idea_run", lambda repo: old)
    _start(store, "solo/idea", "idea")
    assert ideas.preview_deployments.recover_campaign_previews(config, store) == []
    assert len(enqueued) == 1


@pytest.mark.parametrize("revoke", ["disable", "remove-repository"])
def test_completed_campaign_keeps_demo_until_hack_preview_authority_is_revoked(tmp_path, revoke):
    config, store, _, ideas = _coordinators(tmp_path)
    campaign = _start(store, "solo/project", "github")
    commit = "c" * 40
    HackStore(store).update_campaign(campaign["id"], state="completed", result_commit=commit)
    manager = FakePreviewManager(config.service.preview_dir)
    runtime = parse_runtime(RUNTIME)
    release = manager._release_dir("solo/project", commit)
    release.mkdir(parents=True)
    preview = manager._start_release("solo/project", commit, release, runtime)
    manager._write_status(
        "solo/project", desired_commit=commit, active_commit=commit, last_good_commit=commit,
        state="healthy", runtime=runtime, detail="final campaign preview",
    )
    try:
        ideas.reconcile()
        manager.run_once()
        assert manager._active["solo/project"] is preview
        assert preview.process.poll() is None

        hack = replace(config.hack, enabled=False) if revoke == "disable" else replace(
            config.hack, repositories=("solo/idea",),
        )
        ideas.config = replace(config, hack=hack)
        ideas.reconcile()
        manager.run_once()

        assert "solo/project" not in manager._active
        assert preview.process.poll() is not None
        assert ideas.preview_deployments.status("solo/project").state == "stopped"
        allowlist = json.loads((config.service.preview_dir / "control/allowlist.json").read_text())
        assert allowlist["repositories"] == ["solo/idea"]
    finally:
        manager.close()


@pytest.mark.parametrize("state,commit", [("failed", "c" * 40), ("completed", None)])
def test_campaign_without_successful_result_does_not_retain_preview_authority(tmp_path, state, commit):
    config, store, _, ideas = _coordinators(tmp_path)
    campaign = _start(store, "solo/project", "github")
    HackStore(store).update_campaign(campaign["id"], state=state, result_commit=commit)

    ideas.reconcile()

    allowlist = json.loads((config.service.preview_dir / "control/allowlist.json").read_text())
    assert allowlist["repositories"] == ["solo/idea"]


@pytest.mark.parametrize("provide_store", [False, True])
def test_graduation_rejects_active_campaign_without_changing_the_approved_plan(tmp_path, provide_store):
    config_path = _write_config(tmp_path / "config.toml", tmp_path)
    config = load_config(config_path)
    spec = _spec()
    backend = FakeGraduationBackend(_snapshot(spec, _progress(spec)))
    store = Store(config.service.state_dir / "state.db")
    graduator = Graduator(config, config_path, backend, store=store if provide_store else None)
    plan = graduator.plan("solo/idea")
    original_config = config_path.read_bytes()
    _start(store, "solo/idea", "idea")

    with pytest.raises(GraduationError, match="active hack campaign"):
        graduator.plan("solo/idea")
    with pytest.raises(GraduationError, match="active hack campaign"):
        graduator.apply("solo/idea", plan.approval_id)

    assert config_path.read_bytes() == original_config
    assert backend.issues == ()
    assert backend.archive_arguments is None


@pytest.mark.parametrize("approval", [None, "previously-approved-plan"])
def test_graduate_cli_rejects_campaign_before_stopping_services(tmp_path, monkeypatch, approval):
    config_path = _write_config(tmp_path / "config.toml", tmp_path)
    config = load_config(config_path)
    store = Store(config.service.state_dir / "state.db")
    _start(store, "solo/idea", "idea")
    monkeypatch.setattr(cli, "_stop_for_graduation", lambda: pytest.fail("must not stop an active campaign"))
    monkeypatch.setattr(cli, "GhGraduationBackend", lambda *_args, **_kwargs: pytest.fail("must reject before GitHub"))

    with pytest.raises(GraduationError, match="active hack campaign"):
        cli._graduate(str(config_path), "solo/idea", approval)


def test_graduation_dry_run_does_not_create_state_database(tmp_path):
    config_path = _write_config(tmp_path / "config.toml", tmp_path)
    config = load_config(config_path)
    spec = _spec()
    backend = FakeGraduationBackend(_snapshot(spec, _progress(spec)))

    Graduator(config, config_path, backend).plan("solo/idea")

    assert not config.service.state_dir.exists()
