from __future__ import annotations

import base64
import os
from dataclasses import replace

import pytest
from conftest import issue, make_config

from symphony.config import VaultConfig
from symphony.ideas_contract import validate_spec
from symphony.models import IdeaSnapshot, JobState
from symphony.store import Store
from symphony.vault import GhVaultBackend, VaultBridge, VaultError, attach_repository, read_note


class Backend:
    def __init__(self):
        self.repos = set()
        self.specs = {}
        self.labels = []
        self.calls = []
        self.fail = False

    def ensure_repository(self, repository, *, managed):
        if self.fail:
            raise OSError("offline")
        self.repos.add(repository)
        self.calls.append((repository, managed))

    def sync_spec(self, repository, spec, provider, port, spec_path):
        validate_spec(spec, repository)
        self.specs[repository] = spec

    def ensure_labels(self, repository):
        self.labels.append(repository)

    def progress(self, repository, progress_path):
        return {"PROGRESS.md": b"# Implemented\n", "assets/demo.png": b"png"}


def setup(tmp_path, *, body="Build a pantry app."):
    config = make_config(tmp_path)
    vault = tmp_path / "obsidian"
    projects = vault / "1. Projects & Tasks"
    projects.mkdir(parents=True)
    config = replace(config, vault=VaultConfig(True, vault, owner="solo", quiet_seconds=0))
    note = projects / "Dinner planner.md"
    note.write_text("---\nsymphony: idea\ntags: [projects, cooking]\ncreated: 2026-09-18\n---\n" + body)
    store = Store(tmp_path / "state.db")
    backend = Backend()
    return VaultBridge(config, store, backend), note, store, backend


def mode(note, value):
    text = note.read_text()
    for old in ("idea", "github", "paused"):
        text = text.replace(f"symphony: {old}\n", f"symphony: {value}\n")
    note.write_text(text)


def test_plain_note_creates_repository_and_internal_contract_once(tmp_path):
    bridge, note, store, backend = setup(tmp_path)
    assert bridge.reconcile() == []
    project = store.vault_projects()[0]
    repo = project["repository"]
    assert len(backend.repos) == 1
    assert project["mode"] == "idea"
    assert f"repo: {repo}" in note.read_text()
    assert "tags: [projects, cooking]" in note.read_text()
    assert backend.specs[repo].endswith(b"## Project\n\nBuild a pantry app.")
    assert repo in bridge.effective_config().ideas.repositories
    assert repo not in bridge.effective_config().github.allowed_repositories
    assert (bridge.base_config.vault.path / "_symphony" / repo.replace("/", "--") / "PROGRESS.md").is_file()
    assert bridge.reconcile() == []
    assert len(store.vault_projects()) == len(backend.repos) == 1
    moved = note.with_name("A better name.md")
    note.rename(moved)
    bridge.reconcile()
    assert store.vault_projects()[0]["repository"] == repo
    assert store.vault_projects()[0]["note_path"] == str(moved)


def test_generated_progress_is_syncable_under_the_service_private_umask(tmp_path):
    bridge, _, store, _ = setup(tmp_path)
    old_mask = os.umask(0o077)
    try:
        assert bridge.reconcile() == []
    finally:
        os.umask(old_mask)
    repo = store.vault_projects()[0]["repository"].replace("/", "--")
    root = bridge.base_config.vault.path / "_symphony"
    for directory in (root, root / repo, root / repo / "assets"):
        assert directory.stat().st_mode & 0o770 == 0o770
    assert (root / repo / "assets/demo.png").stat().st_mode & 0o660 == 0o660
    # Original-note recovery remains private; the grant applies only to output.
    originals = bridge.base_config.service.state_dir / "vault-note-originals"
    assert originals.stat().st_mode & 0o077 == 0


def test_modes_preserve_issues_and_resume_same_repo(tmp_path):
    bridge, note, store, backend = setup(tmp_path)
    bridge.reconcile()
    repo = store.vault_projects()[0]["repository"]
    original = store.ensure_job(issue(repo), "codex", None, False, "agent/1-demo", repo)[0]
    assert store.claim_next("worker", 60, 2, {"codex": 2}) is None
    mode(note, "github")
    bridge.reconcile()
    assert backend.labels == [repo]
    assert repo in bridge.effective_config().github.allowed_repositories
    assert repo not in bridge.effective_config().ideas.repositories
    claimed = store.claim_next("worker", 60, 2, {"codex": 2})
    assert claimed.id == original.id
    mode(note, "idea")
    bridge.reconcile()
    project = store.vault_projects()[0]
    assert project["mode"] == "github" and project["desired_mode"] == "idea"
    assert not store.vault_allows(repo, "github")
    store.transition(claimed.id, JobState.PR_OPEN)
    bridge.reconcile()
    assert store.vault_projects()[0]["mode"] == "idea"
    assert store.get_job_by_id(original.id).state == JobState.PR_OPEN
    mode(note, "github")
    bridge.reconcile()
    assert store.get_job_by_id(original.id).state == JobState.PR_OPEN
    assert len(backend.repos) == 1


def test_idea_queue_is_retained_but_not_claimed_in_github_mode(tmp_path):
    bridge, note, store, backend = setup(tmp_path)
    bridge.reconcile()
    repo = store.vault_projects()[0]["repository"]
    spec = backend.specs[repo]
    snapshot = IdeaSnapshot(repo, "a" * 40, spec, b"", b"", "b" * 40, "main")
    run, _, _ = store.ensure_idea_run(snapshot, "codex")
    mode(note, "github")
    bridge.reconcile()
    assert store.claim_next_idea("worker", 60, 2, {"codex": 2}) is None
    mode(note, "idea")
    bridge.reconcile()
    assert store.claim_next_idea("worker", 60, 2, {"codex": 2}).id == run.id


@pytest.mark.parametrize("change", ["missing", "unmarked", "malformed", "duplicate", "conflict"])
def test_invalid_or_missing_note_pauses_without_deleting_repository(tmp_path, change):
    bridge, note, store, backend = setup(tmp_path)
    bridge.reconcile()
    repo = store.vault_projects()[0]["repository"]
    if change == "missing":
        note.unlink()
    elif change == "unmarked":
        note.write_text("Regular note")
    elif change == "malformed":
        note.write_text("---\nsymphony: idea\ntags: [\n---\nhello")
    elif change == "duplicate":
        note.with_name("duplicate.md").write_bytes(note.read_bytes())
    else:
        note.with_name("Dinner planner.sync-conflict-20260918-120000-device.md").write_bytes(note.read_bytes())
    bridge.reconcile()
    assert store.vault_projects()[0]["mode"] == "paused"
    assert not store.vault_allows(repo, "idea")
    assert repo in backend.repos


def test_note_changes_during_run_are_rejected_before_publication(tmp_path):
    bridge, note, store, backend = setup(tmp_path)
    bridge.reconcile()
    repo = store.vault_projects()[0]["repository"]
    accepted = backend.specs[repo]
    bridge.guard_spec(repo, accepted)
    note.write_text(note.read_text() + "\nActually make it a calendar.")
    with pytest.raises(VaultError, match="changed"):
        bridge.guard_spec(repo, accepted)


def test_mode_change_alone_does_not_invalidate_draining_run(tmp_path):
    bridge, note, store, backend = setup(tmp_path)
    bridge.reconcile()
    repo = store.vault_projects()[0]["repository"]
    mode(note, "github")
    bridge.guard_spec(repo, backend.specs[repo])


def test_offline_preserves_note_and_recovers(tmp_path):
    bridge, note, store, backend = setup(tmp_path)
    original = note.read_bytes()
    backend.fail = True
    assert bridge.reconcile()
    assert note.read_bytes() == original
    backend.fail = False
    assert bridge.reconcile() == []
    assert store.vault_projects()[0]["mode"] == "idea"


def test_registration_will_not_overwrite_concurrent_edit(tmp_path):
    bridge, path, _, _ = setup(tmp_path)
    note = read_note(path)
    path.write_text(path.read_text() + "\nNew edit")
    with pytest.raises(VaultError, match="changed"):
        attach_repository(note, "solo/example", tmp_path / "backups")
    assert path.read_text().endswith("New edit")


def test_duplicate_yaml_keys_and_cross_owner_routes_are_rejected(tmp_path):
    bridge, path, store, backend = setup(tmp_path)
    path.write_text("---\nsymphony: idea\nsymphony: github\n---\nHello")
    assert bridge.reconcile()
    assert not backend.repos
    path.write_text("---\nsymphony: idea\nrepo: someone/else\n---\nHello")
    assert bridge.reconcile()
    assert not backend.repos


def test_debounce_prevents_partial_edit_from_starting_work(tmp_path):
    bridge, _, store, backend = setup(tmp_path)
    bridge.base_config = replace(bridge.base_config, vault=replace(bridge.base_config.vault, quiet_seconds=600))
    bridge.reconcile()
    project = store.vault_projects()[0]
    assert not backend.repos
    assert not store.vault_allows(project["repository"], "idea")


def test_github_sync_uses_tree_sha_and_never_overwrites_runtime(tmp_path, monkeypatch):
    backend = GhVaultBackend()
    calls = []
    existing = {".symphony/idea.toml": {"sha": "runtime"}, ".openhands/setup.sh": {"sha": "setup"}}
    monkeypatch.setattr(backend, "_tree", lambda repo: ("main", "commit-sha", "tree-sha", existing))

    def api(method, path, payload):
        calls.append((method, path, payload))
        return {"sha": "new-sha"}

    monkeypatch.setattr(backend, "api", api)
    backend.sync_spec("solo/demo", b"spec", "codex", 10001, "idea/SPEC.md")
    tree = next(payload for _, path, payload in calls if path.endswith("/git/trees"))
    assert tree["base_tree"] == "tree-sha"
    assert ".symphony/idea.toml" not in [entry["path"] for entry in tree["tree"]]
    assert ".openhands/setup.sh" not in [entry["path"] for entry in tree["tree"]]
    assert calls[-1][2] == {"sha": "new-sha", "force": False}
    assert any(base64.b64decode(payload["content"]) == b"spec" for _, _, payload in calls if "content" in payload)


def test_bom_crlf_note_preserves_header_and_prose(tmp_path):
    path = tmp_path / "note.md"
    raw = b"\xef\xbb\xbf---\r\nsymphony: idea\r\ntags: [test]\r\n---\r\nA normal note.\r\n"
    path.write_bytes(raw)
    note = attach_repository(read_note(path), "solo/project", tmp_path / "backups")
    assert note.repository == "solo/project"
    assert path.read_bytes() == raw.replace(b"---\r\n", b"---\r\nrepo: solo/project\r\n", 1)


def test_service_restart_reloads_routing_without_github_mutation(tmp_path):
    bridge, note, store, backend = setup(tmp_path)
    bridge.reconcile()
    mode(note, "github")
    bridge.reconcile()
    calls = len(backend.calls)
    reloaded = VaultBridge(bridge.base_config, Store(store.path), backend)
    repo = store.vault_projects()[0]["repository"]
    assert repo in reloaded.effective_config().github.allowed_repositories
    assert repo not in reloaded.effective_config().ideas.repositories
    assert len(backend.calls) == calls


def test_backend_failure_does_not_kill_future_reconciliation(tmp_path, monkeypatch):
    bridge, note, store, backend = setup(tmp_path)
    bridge.reconcile()
    original = bridge._safe_output
    monkeypatch.setattr(bridge, "_safe_output", lambda *args: (_ for _ in ()).throw(PermissionError("vault is read-only")))
    assert bridge.reconcile()
    assert store.vault_projects()[0]["mode"] == "paused"
    monkeypatch.setattr(bridge, "_safe_output", original)
    assert bridge.reconcile() == []
    assert store.vault_projects()[0]["mode"] == "idea"


@pytest.mark.parametrize("folder_checklist", [False, True])
def test_note_to_scheduler_to_git_publication_and_back_to_vault(tmp_path, folder_checklist):
    from conftest import FakeGitHub
    from test_ideas import FakePreview, LocalIdeasGitHub, LocalIdeaWorkspaces, _edit_remote, _idea_remote, _run

    from symphony.coordinator import Coordinator
    from symphony.execution import ProviderSlots
    from symphony.ideas_coordinator import IdeasCoordinator
    from symphony.models import IdeaRunState
    from symphony.preview_queue import PreviewQueue
    from symphony.providers.fake import FakeProvider
    from symphony.scheduler import Scheduler

    remote, _ = _idea_remote(tmp_path)
    config = make_config(tmp_path)
    root = tmp_path / "obsidian"
    projects = root / "1. Projects & Tasks"
    projects.mkdir(parents=True)
    note = projects / "My app.md"
    note.write_text("---\nsymphony: idea\nrepo: solo/idea\n---\n## Greeting\nBuild a greeting app.\n")
    child = projects / "Greeting.md"
    if folder_checklist:
        note.write_text(note.read_text() + "\n- [ ] [[Greeting]]\n\n%% Begin Waypoint %%\n- [[Greeting]]\n%% End Waypoint %%\n")
        child.write_text("Show a friendly greeting.\n")
    config = replace(config, vault=VaultConfig(True, root, owner="solo", quiet_seconds=0))
    store = Store(config.service.state_dir / "state.db")
    github = LocalIdeasGitHub(remote)

    class LocalBridgeBackend(Backend):
        def sync_spec(self, repository, spec, provider, port, spec_path):
            if github.get_snapshot(repository, spec_path, "idea/PROGRESS.md").spec_content != spec:
                import uuid

                _edit_remote(tmp_path, remote, spec_path, spec, "note sync " + uuid.uuid4().hex[:12])

        def progress(self, repository, progress_path):
            progress = github.get_snapshot(repository, "idea/SPEC.md", progress_path).previous_progress
            return {"PROGRESS.md": progress} if progress else {}

    provider = FakeProvider("codex", write_files={"implemented.txt": "ok\n"})
    slots = ProviderSlots(config.scheduler.provider_concurrency, {"codex"})
    coordinator = Coordinator(config, store, FakeGitHub([]), {"codex": provider}, slots)
    ideas = IdeasCoordinator(config, store, github, {"codex": provider}, slots)
    ideas.workspaces = LocalIdeaWorkspaces(tmp_path, remote)
    ideas.preview_deployments = PreviewQueue(config.service.preview_dir, ideas.workspaces.root)
    ideas.preview = FakePreview()
    bridge = VaultBridge(config, store, LocalBridgeBackend())
    coordinator.ideas = ideas
    coordinator.vault = ideas.vault = bridge
    scheduler = Scheduler(config, store, coordinator, ideas)
    try:
        assert scheduler.tick(reconcile=True) == 1
    finally:
        scheduler.stop(wait=True)
    assert store.list_idea_runs()[-1].state == IdeaRunState.PUBLISHED
    assert _run(["git", "--git-dir", str(remote), "show", "main:implemented.txt"]) == "ok"
    bridge.reconcile()
    progress = root / "_symphony/solo--idea/PROGRESS.md"
    assert b"**done**" in progress.read_bytes()
    if not folder_checklist:
        assert note.read_text().endswith("Build a greeting app.\n")
    else:
        assert "- [x] [[Greeting]]" in note.read_text()
        assert "<project-checklist>" in provider.starts[0][1]
        assert "- [ ] Greeting.md" in provider.starts[0][1]
    assert len(provider.starts) == 1
    if folder_checklist:
        scheduler = Scheduler(config, store, coordinator, ideas)
        try:
            assert scheduler.tick(reconcile=True) == 0  # ticking a box must not create another run
            child.write_text("Show a friendly greeting and a reset button.\n")
            assert scheduler.tick(reconcile=True) == 1
        finally:
            scheduler.stop(wait=True)
        assert len(provider.starts) == 2
        assert "- [ ] [[Greeting]]" in note.read_text()
        assert store.list_idea_runs()[-1].state == IdeaRunState.PUBLISHED
        bridge.reconcile()
        assert "- [x] [[Greeting]]" in note.read_text()
        assert b"reset button" in progress.read_bytes()
        # Restoring an exact older note version is a new request against the current code.
        child.write_text("Show a friendly greeting.\n")
        scheduler = Scheduler(config, store, coordinator, ideas)
        try:
            assert scheduler.tick(reconcile=True) == 1
        finally:
            scheduler.stop(wait=True)
        assert len(provider.starts) == 3
        assert store.list_idea_runs()[-1].state == IdeaRunState.PUBLISHED
        bridge.reconcile()
        assert "- [x] [[Greeting]]" in note.read_text()


def test_registration_preserves_edit_racing_atomic_exchange(tmp_path, monkeypatch):
    from symphony import graduation

    _, path, _, _ = setup(tmp_path)
    note = read_note(path)
    edited = note.raw + b"\nConcurrent Syncthing edit"
    exchange = graduation._atomic_exchange
    first = True

    def racing_exchange(left, right):
        nonlocal first
        if first:
            first = False
            path.write_bytes(edited)
        exchange(left, right)

    monkeypatch.setattr(graduation, "_atomic_exchange", racing_exchange)
    with pytest.raises(VaultError, match="concurrent bytes preserved"):
        attach_repository(note, "solo/demo", tmp_path / "backups")
    assert path.read_bytes() == edited
    assert any(p.read_bytes() == edited for p in (tmp_path / "backups").glob("*.md"))
