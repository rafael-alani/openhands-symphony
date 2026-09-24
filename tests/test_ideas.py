from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from conftest import FakeGitHub, issue, make_config

from symphony.config import IdeasConfig
from symphony.coordinator import Coordinator
from symphony.execution import ProviderSlots
from symphony.ideas_contract import (
    IdeaContractError,
    IdeaRuntime,
    git_blob_hash,
    parse_runtime,
    preview_argv,
    validate_spec,
)
from symphony.ideas_coordinator import IdeasCoordinator
from symphony.ideas_preview import IdeaPreview, PreviewError, PreviewEvidence
from symphony.models import IdeaRunState, IdeaSnapshot, ProviderOutcome
from symphony.preview_queue import PreviewQueue
from symphony.providers.fake import FakeProvider
from symphony.store import Store
from symphony.webhook import create_app
from symphony.workspace import NonFastForwardError, WorkspaceError, WorkspaceManager

SPEC = b"---\nsymphony: idea\nrepo: solo/idea\n---\n\n# Test idea\n\n## First wish\n\nBuild the smallest thing.\n"
RUNTIME = (
    b'provider = "codex"\n\n[preview]\nstart = ["python3", "-m", "app"]\n'
    b'port = 4317\nhealth_path = "/health"\nstartup_timeout_seconds = 20\n'
)


def test_preview_argv_renders_effective_port_placeholder():
    runtime = parse_runtime(
        b'provider = "codex"\n[preview]\nstart = ["python3", "-m", "app", "--port", "{port}"]\n'
        b'port = 4317\nhealth_path = "/health"\nstartup_timeout_seconds = 20\n'
    )

    assert preview_argv(runtime) == ("python3", "-m", "app", "--port", "4317")


def test_prepublication_preview_does_not_probe_the_stable_last_good_port(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        stable_port = int(probe.getsockname()[1])

    old_preview = subprocess.Popen(
        ["python3", "-m", "http.server", str(stable_port), "--bind", "127.0.0.1"],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        old_health = f"http://127.0.0.1:{stable_port}/"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if httpx.get(old_health, timeout=0.25).is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail("last-good preview fixture did not become healthy")

        runtime = parse_runtime(
            (
                'provider = "codex"\n[preview]\n'
                'start = ["python3", "-m", "http.server", "{port}", "--bind", "127.0.0.1"]\n'
                f'port = {stable_port}\nhealth_path = "/"\nstartup_timeout_seconds = 5\n'
            ).encode()
        )
        evidence = IdeaPreview("", tmp_path / "state").boot_and_capture(tmp_path, runtime, ())

        assert urlsplit(evidence.health_url).port != stable_port
        assert old_preview.poll() is None
    finally:
        if old_preview.poll() is None:
            old_preview.terminate()
        old_preview.wait(timeout=5)


def test_prepublication_preview_handles_noisy_startup_and_stops_server_children(tmp_path):
    (tmp_path / "app.py").write_text(
        "import http.server, os, signal, time\n"
        "from pathlib import Path\n"
        "print('startup output ' * 100000, flush=True)\n"
        "if os.fork() == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    Path('child.pid').write_text(str(os.getpid()))\n"
        "    server = http.server.HTTPServer(('127.0.0.1', int(os.environ['PORT'])), "
        "http.server.SimpleHTTPRequestHandler)\n"
        "    server.serve_forever()\n"
        "else:\n"
        "    time.sleep(60)\n"
    )
    runtime = IdeaRuntime("codex", (sys.executable, "app.py"), 4317, "/", 5)
    try:
        evidence = IdeaPreview("", tmp_path / "state").boot_and_capture(tmp_path, runtime, ())
        assert "startup output" in evidence.log
        assert len(evidence.log) <= 4000
        port = urlsplit(evidence.health_url).port
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.1)
                if probe.connect_ex(("127.0.0.1", port)) != 0:
                    break
            time.sleep(0.05)
        else:
            pytest.fail("candidate child server survived preview cleanup")
    finally:
        child_pid = tmp_path / "child.pid"
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def _run(command: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _commit(repository: Path, message: str) -> str:
    _run(["git", "add", "--all"], repository)
    _run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            message,
        ],
        repository,
    )
    return _run(["git", "rev-parse", "HEAD"], repository)


def _idea_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "idea.git"
    source = tmp_path / "idea-source"
    _run(["git", "init", "--bare", str(remote)])
    _run(["git", "init", "-b", "main", str(source)])
    (source / "idea").mkdir()
    (source / ".symphony").mkdir()
    (source / "idea" / "SPEC.md").write_bytes(SPEC)
    (source / ".symphony" / "idea.toml").write_bytes(RUNTIME)
    _commit(source, "initial idea")
    _run(["git", "remote", "add", "origin", str(remote)], source)
    _run(["git", "push", "-u", "origin", "main"], source)
    _run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], remote)
    return remote, source


class LocalIdeasGitHub:
    def __init__(self, remote: Path):
        self.remote = remote
        self.private = True

    def _show(self, revision: str, path: str) -> bytes:
        process = subprocess.run(
            ["git", "--git-dir", str(self.remote), "show", f"{revision}:{path}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return process.stdout if process.returncode == 0 else b""

    def get_snapshot(self, repository: str, spec_path: str, progress_path: str) -> IdeaSnapshot:
        assert repository == "solo/idea"
        commit = _run(["git", "--git-dir", str(self.remote), "rev-parse", "refs/heads/main"])
        spec = self._show(commit, spec_path)
        runtime = self._show(commit, ".symphony/idea.toml")
        if not spec or not runtime:
            raise RuntimeError("idea contract missing")
        return IdeaSnapshot(
            repository,
            git_blob_hash(spec),
            spec,
            runtime,
            self._show(commit, progress_path),
            commit,
            "main",
            self.private,
        )


class LocalIdeaWorkspaces:
    def __init__(self, tmp_path: Path, remote: Path):
        self.root = tmp_path / "idea-worktrees"
        self.root.mkdir()
        self.remote = remote
        self.race = None
        self.push_attempts = 0

    def checkout_run(self, *, run_id, repository, branch, base_branch, base_revision=None):
        worktree = self.root / run_id
        if not worktree.exists():
            _run(["git", "clone", str(self.remote), str(worktree)])
            _run(["git", "checkout", "-b", branch, base_revision or f"origin/{base_branch}"], worktree)
        return worktree

    def run_setup(self, worktree, setup_script, validation_user):
        return None

    def prepare_for_agent(self, worktree):
        return None

    def verify_run_integrity(self, run_id, repository, worktree):
        return None

    @staticmethod
    def changed_paths(worktree, default_branch):
        return WorkspaceManager.changed_paths(worktree, default_branch)

    @staticmethod
    def commit_run(worktree, message, paths=()):
        return WorkspaceManager.commit_run(worktree, message, paths)

    @staticmethod
    def head(worktree):
        return WorkspaceManager.head(worktree)

    @staticmethod
    def fetch(worktree, repository):
        _run(["git", "fetch", "--prune", "origin"], worktree)

    @staticmethod
    def rebase_onto_origin(worktree, default_branch):
        _run(["git", "-c", "core.hooksPath=/dev/null", "rebase", f"origin/{default_branch}"], worktree)

    def push_default(self, worktree, repository, default_branch):
        self.push_attempts += 1
        if self.race is not None:
            race, self.race = self.race, None
            race()
        process = subprocess.run(
            ["git", "push", "origin", f"HEAD:refs/heads/{default_branch}"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode:
            raise NonFastForwardError(process.stderr)


class FakePreview:
    def boot_and_capture(self, worktree, runtime, affected):
        screenshots = {}
        for section in affected:
            relative = f"idea/assets/{section.slug}.png"
            target = worktree / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            screenshots[section.slug] = relative
        return PreviewEvidence(f"http://127.0.0.1:{runtime.port}{runtime.health_path}", screenshots, "healthy")


class DummyScheduler:
    def start(self):
        return None

    def stop(self, wait=True):
        return None

    def tick(self):
        return 0


def _config(tmp_path: Path):
    config = make_config(tmp_path)
    return replace(config, ideas=IdeasConfig(("solo/idea",)))


def _coordinator(tmp_path: Path, provider: FakeProvider, remote: Path):
    config = _config(tmp_path)
    store = Store(config.service.state_dir / "state.db")
    slots = ProviderSlots(config.scheduler.provider_concurrency, {"codex"})
    coordinator = IdeasCoordinator(config, store, LocalIdeasGitHub(remote), {"codex": provider}, slots)
    workspaces = LocalIdeaWorkspaces(tmp_path, remote)
    coordinator.workspaces = workspaces
    coordinator.preview_deployments = PreviewQueue(config.service.preview_dir, workspaces.root)
    coordinator.preview = FakePreview()
    return config, store, coordinator, workspaces


def _claim(store: Store, config):
    run = store.claim_next_idea(
        "idea-worker",
        config.scheduler.lease_seconds,
        config.scheduler.global_concurrency,
        config.scheduler.provider_concurrency,
    )
    assert run
    return run


def _edit_remote(tmp_path: Path, remote: Path, path: str, content: bytes, message: str) -> None:
    editor = tmp_path / f"editor-{message.replace(' ', '-')}"
    _run(["git", "clone", str(remote), str(editor)])
    target = editor / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    _commit(editor, message)
    _run(["git", "push", "origin", "main"], editor)


def test_idea_contract_is_strict_and_runtime_uses_argument_array():
    validate_spec(SPEC, "solo/idea")
    assert parse_runtime(RUNTIME).start == ("python3", "-m", "app")
    with pytest.raises(IdeaContractError, match="exactly one"):
        validate_spec(SPEC.replace(b"repo: solo/idea", b"repo: solo/idea\nextra: no"), "solo/idea")
    with pytest.raises(IdeaContractError, match="argument array"):
        parse_runtime(RUNTIME.replace(b'["python3", "-m", "app"]', b'"python3 -m app"'))
    with pytest.raises(IdeaContractError, match="safe non-empty name"):
        parse_runtime(RUNTIME.replace(b'provider = "codex"', b'provider = "bad provider"'))
    with pytest.raises(IdeaContractError, match="without a query or fragment"):
        parse_runtime(RUNTIME.replace(b'health_path = "/health"', b'health_path = "//other-host/health"'))
    with pytest.raises(IdeaContractError, match="positive integer"):
        parse_runtime(RUNTIME.replace(b"startup_timeout_seconds = 20", b"startup_timeout_seconds = 1.5"))


def test_duplicate_push_webhooks_create_one_idea_run(tmp_path):
    config = _config(tmp_path)
    config.service.webhook_secret_file.write_text("test-secret\n")
    store = Store(config.service.state_dir / "state.db")
    provider = FakeProvider("codex")
    slots = ProviderSlots(config.scheduler.provider_concurrency, {"codex"})
    issue_coordinator = Coordinator(config, store, FakeGitHub([issue()]), {"codex": provider}, slots)

    class StaticIdeas:
        def get_snapshot(self, repository, spec_path, progress_path):
            return IdeaSnapshot("solo/idea", git_blob_hash(SPEC), SPEC, RUNTIME, b"", "a" * 40, "main", True)

    ideas = IdeasCoordinator(config, store, StaticIdeas(), {"codex": provider}, slots)
    app = create_app(
        store,
        issue_coordinator,
        DummyScheduler(),
        config.service.webhook_secret_file,
        ideas,
    )
    payload = json.dumps(
        {
            "ref": "refs/heads/main",
            "repository": {"full_name": "solo/idea", "private": True, "default_branch": "main"},
            "commits": [{"added": [], "modified": ["idea/SPEC.md"], "removed": []}],
        }
    ).encode()
    headers = {
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "idea-delivery-1",
        "X-Hub-Signature-256": "sha256=" + hmac.new(b"test-secret", payload, hashlib.sha256).hexdigest(),
        "Content-Type": "application/json",
    }

    async def deliver():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return (
                await client.post("/webhooks/github", content=payload, headers=headers),
                await client.post("/webhooks/github", content=payload, headers=headers),
            )

    first, second = asyncio.run(deliver())
    assert first.json()["created"] is True
    assert second.json()["duplicate"] is True
    assert len(store.list_idea_runs()) == 1


def test_newer_spec_prevents_superseded_run_from_publishing(tmp_path):
    remote, _ = _idea_remote(tmp_path)
    config, store, coordinator, workspaces = _coordinator(
        tmp_path, FakeProvider("codex", write_files={"implemented.txt": "old\n"}), remote
    )
    first, _ = coordinator.observe_repository("solo/idea")
    claimed = _claim(store, config)
    _edit_remote(tmp_path, remote, "idea/SPEC.md", SPEC + b"\nNewer revision.\n", "new spec")
    second, created = coordinator.observe_repository("solo/idea")

    result = coordinator.run_claimed(claimed)

    assert created and second.spec_hash != first.spec_hash
    assert result.state == IdeaRunState.SUPERSEDED
    assert workspaces.push_attempts == 0


def test_agent_spec_mutation_fails_without_push(tmp_path):
    remote, _ = _idea_remote(tmp_path)
    config, store, coordinator, workspaces = _coordinator(
        tmp_path,
        FakeProvider("codex", write_files={"idea/SPEC.md": "agent changed this\n"}),
        remote,
    )
    coordinator.observe_repository("solo/idea")

    result = coordinator.run_claimed(_claim(store, config))

    assert result.state == IdeaRunState.FAILED
    assert result.phase == "spec-modified"
    assert workspaces.push_attempts == 0
    assert LocalIdeasGitHub(remote).get_snapshot("solo/idea", "idea/SPEC.md", "idea/PROGRESS.md").spec_content == SPEC


def test_fresh_idea_worktree_is_shared_before_validator_setup(tmp_path, monkeypatch):
    remote, _ = _idea_remote(tmp_path)
    config, store, coordinator, workspaces = _coordinator(
        tmp_path, FakeProvider("codex", write_files={"implemented.txt": "ok\n"}), remote
    )
    shared = set()
    monkeypatch.setattr(workspaces, "prepare_for_agent", lambda path: shared.add(path))

    def setup(path, *_args):
        assert path in shared, "validator cannot read a fresh private checkout"

    monkeypatch.setattr(workspaces, "run_setup", setup)
    run, _ = coordinator.observe_repository("solo/idea")
    store.update_idea_run(run.id, question="A previous setup attempt failed")
    result = coordinator.run_claimed(_claim(store, config))
    assert result.state == IdeaRunState.PUBLISHED
    assert result.question == ""


def test_pre_provider_failure_does_not_requeue_forever(tmp_path, monkeypatch):
    remote, _ = _idea_remote(tmp_path)
    config, store, coordinator, workspaces = _coordinator(tmp_path, FakeProvider("codex"), remote)

    def setup(*_args):
        raise OSError("setup unavailable")

    monkeypatch.setattr(workspaces, "run_setup", setup)
    coordinator.observe_repository("solo/idea")
    run = coordinator.run_claimed(_claim(store, config))
    assert run.attempt == 0
    assert run.state == IdeaRunState.FAILED
    assert not store.claim_next_idea("another-worker", 60, 2, {"codex": 2})


def test_non_fast_forward_race_rebases_once_and_keeps_implementation(tmp_path):
    remote, _ = _idea_remote(tmp_path)
    provider = FakeProvider("codex", write_files={"implemented.txt": "useful\n"})
    config, store, coordinator, workspaces = _coordinator(tmp_path, provider, remote)
    coordinator.observe_repository("solo/idea")

    def race():
        _edit_remote(tmp_path, remote, "race.txt", b"concurrent\n", "concurrent push")

    workspaces.race = race
    result = coordinator.run_claimed(_claim(store, config))

    assert result.state == IdeaRunState.PUBLISHED
    assert workspaces.push_attempts == 2
    assert LocalIdeasGitHub(remote)._show("main", "implemented.txt") == b"useful\n"
    assert LocalIdeasGitHub(remote)._show("main", "race.txt") == b"concurrent\n"
    assert b"assets/first-wish.png" in LocalIdeasGitHub(remote)._show("main", "idea/PROGRESS.md")
    deployment = coordinator.preview_deployments.queued_request("solo/idea")
    assert deployment is not None
    assert deployment.commit == result.published_commit
    project = store.get_idea_project("solo/idea")
    assert project.preview_state == "pending"
    assert project.last_good_preview_commit is None


def test_non_fast_forward_race_rechecks_rebased_code_before_publication(tmp_path):
    remote, _ = _idea_remote(tmp_path)
    config, store, coordinator, workspaces = _coordinator(
        tmp_path, FakeProvider("codex", write_files={"implemented.txt": "useful\n"}), remote
    )
    coordinator.observe_repository("solo/idea")
    checked = []

    class DetectBrokenRebase(FakePreview):
        def boot_and_capture(self, worktree, runtime, affected):
            checked.append(worktree)
            if (worktree / "incompatible.txt").exists():
                raise PreviewError("concurrent change breaks preview startup")
            return super().boot_and_capture(worktree, runtime, affected)

    coordinator.preview = DetectBrokenRebase()
    workspaces.race = lambda: _edit_remote(
        tmp_path, remote, "incompatible.txt", b"breaks startup\n", "incompatible push"
    )

    result = coordinator.run_claimed(_claim(store, config))

    assert result.state != IdeaRunState.PUBLISHED
    assert len(checked) == 2
    assert workspaces.push_attempts == 1
    assert LocalIdeasGitHub(remote)._show("main", "implemented.txt") == b""
    assert LocalIdeasGitHub(remote)._show("main", "incompatible.txt") == b"breaks startup\n"


def test_question_publishes_progress_and_next_spec_edit_requeues(tmp_path):
    remote, _ = _idea_remote(tmp_path)
    provider = FakeProvider("codex", ProviderOutcome.NEEDS_GUIDANCE)
    config, store, coordinator, _ = _coordinator(tmp_path, provider, remote)
    first, _ = coordinator.observe_repository("solo/idea")

    question = coordinator.run_claimed(_claim(store, config))

    progress = LocalIdeasGitHub(remote)._show("main", "idea/PROGRESS.md")
    assert question.state == IdeaRunState.QUESTION
    assert b"> **question**" in progress
    assert b"Choose option A or option B." in progress
    answered = SPEC.replace(b"Build the smallest thing.", b"Build option A.")
    _edit_remote(tmp_path, remote, "idea/SPEC.md", answered, "answer question")

    next_run, created = coordinator.observe_repository("solo/idea")

    assert created
    assert next_run.state == IdeaRunState.QUEUED
    assert next_run.spec_hash != first.spec_hash
    assert store.get_idea_project("solo/idea").latest_completed_spec_hash == first.spec_hash


def test_question_publication_checks_worktree_integrity(tmp_path, monkeypatch):
    remote, _ = _idea_remote(tmp_path)
    config, store, coordinator, workspaces = _coordinator(
        tmp_path, FakeProvider("codex", ProviderOutcome.NEEDS_GUIDANCE), remote
    )
    coordinator.observe_repository("solo/idea")

    def replaced_metadata(*_args):
        raise WorkspaceError("worktree .git pointer was replaced by the agent user")

    monkeypatch.setattr(workspaces, "verify_run_integrity", replaced_metadata)
    result = coordinator.run_claimed(_claim(store, config))

    assert result.state != IdeaRunState.QUESTION
    assert workspaces.push_attempts == 0
    assert LocalIdeasGitHub(remote)._show("main", "idea/PROGRESS.md") == b""


def test_next_spec_edit_revisits_unresolved_unchanged_wishes(tmp_path):
    remote, _ = _idea_remote(tmp_path)
    provider = FakeProvider("codex", ProviderOutcome.NEEDS_GUIDANCE)
    config, store, coordinator, _ = _coordinator(tmp_path, provider, remote)
    coordinator.observe_repository("solo/idea")
    assert coordinator.run_claimed(_claim(store, config)).state == IdeaRunState.QUESTION
    # The first section's bytes remain identical: guidance can be supplied in
    # another section, and pending work must still receive fresh results.
    answered = SPEC + b"## Decisions\nUse option A for every feature.\n"
    _edit_remote(tmp_path, remote, "idea/SPEC.md", answered, "add decision")
    coordinator.providers["codex"] = FakeProvider("codex", write_files={"implemented.txt": "option A\n"})
    coordinator.observe_repository("solo/idea")

    result = coordinator.run_claimed(_claim(store, config))

    assert result.state == IdeaRunState.PUBLISHED
    progress = LocalIdeasGitHub(remote)._show("main", "idea/PROGRESS.md")
    assert progress.count(b"> **done**") == 2
    assert b"> **question**" not in progress
    assert b"assets/first-wish.png" in progress
