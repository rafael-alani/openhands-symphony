from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from symphony import graduation
from symphony.config import load_config
from symphony.github import GhCLIBackend
from symphony.graduation import (
    GRADUATION_LOCK,
    GhGraduationBackend,
    GraduationError,
    GraduationIssue,
    Graduator,
    graduated_config_bytes,
    render_plan,
)
from symphony.ideas_progress import SectionResult, render_progress, sections
from symphony.models import IdeaRunState, IdeaSnapshot
from symphony.store import Store

RUNTIME = b'''provider = "codex"
[preview]
start = ["python3", "-m", "http.server", "{port}"]
port = 4321
health_path = "/"
startup_timeout_seconds = 10
'''


def _write_config(path: Path, tmp_path: Path) -> Path:
    path.write_text(
        f'''[service]
state_dir = "{tmp_path / "state"}"
workspace_dir = "{tmp_path / "workspaces"}"
report_dir = "{tmp_path / "reports"}"
preview_dir = "{tmp_path / "preview"}"
log_dir = "{tmp_path / "logs"}"

[github]
allowed_repositories = [
  "solo/project", # retain this operator comment
]

[ideas]
repositories = [
  "solo/idea",
]

[scheduler.provider_concurrency]
codex = 1

[providers.codex]
enabled = false
adapter = "openhands-acp"

[repositories."solo/project"]
instruction = "existing tier one"

[repositories."solo/idea"]
instruction = "keep this repository policy"
'''
    )
    return path


def _spec(done_body: str = "Already built.") -> bytes:
    return f'''---
symphony: idea
repo: solo/idea
---

## Complete feature
{done_body}

## Partial feature
Finish the remaining behavior.

## New feature
Build the new behavior.
'''.encode()


def _progress(spec: bytes) -> bytes:
    current = sections(spec)
    return render_progress(
        spec,
        {
            current[0].slug: SectionResult("done", "The completed behavior works."),
            current[1].slug: SectionResult("partial", "The basic path works."),
            current[2].slug: SectionResult("not started", "Not attempted yet."),
        },
    )


class FakeGraduationBackend:
    def __init__(self, snapshot: IdeaSnapshot, *, archive_error: Exception | None = None):
        self.snapshot = snapshot
        self.archive_error = archive_error
        self.issues = ()
        self.archive_arguments = None

    def get_snapshot(self, repository, spec_path, progress_path):
        assert repository == "solo/idea"
        assert spec_path == "idea/SPEC.md"
        assert progress_path == "idea/PROGRESS.md"
        return self.snapshot

    def ensure_issues(self, repository, issues):
        self.issues = issues
        return tuple(f"https://github.test/{repository}/issues/{index}" for index, _ in enumerate(issues, 1))

    def archive_idea_files(self, repository, **kwargs):
        self.archive_arguments = (repository, kwargs)
        if self.archive_error:
            raise self.archive_error
        return "c" * 40


def _snapshot(spec: bytes, progress: bytes) -> IdeaSnapshot:
    return IdeaSnapshot(
        repository="solo/idea",
        spec_hash="a" * 40,
        spec_content=spec,
        runtime_content=RUNTIME,
        previous_progress=progress,
        base_commit="b" * 40,
        default_branch="main",
        private=True,
    )


def test_plan_carries_only_unfinished_current_sections(tmp_path) -> None:
    config_path = _write_config(tmp_path / "config.toml", tmp_path)
    config = load_config(config_path)
    spec = _spec()
    backend = FakeGraduationBackend(_snapshot(spec, _progress(spec)))

    plan = Graduator(config, config_path, backend).plan("solo/idea")

    assert [issue.title for issue in plan.issues] == ["Partial feature", "New feature"]
    assert [issue.status for issue in plan.issues] == ["partial", "not started"]
    assert all("intentionally unrouted" in issue.body for issue in plan.issues)
    assert plan.archive_prefix == f"archive/ideas/{'a' * 12}"
    assert len(plan.approval_id) == 20
    assert "Apply exactly this plan with: sudo agentctl" in render_plan(plan, config_path=config_path)


def test_plan_does_not_trust_done_status_after_spec_body_changes(tmp_path) -> None:
    config_path = _write_config(tmp_path / "config.toml", tmp_path)
    config = load_config(config_path)
    previous = _spec("Old completed behavior.")
    current = _spec("The completed wish changed and needs more work.")
    backend = FakeGraduationBackend(_snapshot(current, _progress(previous)))

    plan = Graduator(config, config_path, backend).plan("solo/idea")

    assert plan.issues[0].title == "Complete feature"
    assert plan.issues[0].status == "not started"


def test_apply_moves_allowlist_archives_and_retires_claimed_idea_work(tmp_path) -> None:
    config_path = _write_config(tmp_path / "config.toml", tmp_path)
    config = load_config(config_path)
    spec = _spec()
    snapshot = _snapshot(spec, _progress(spec))
    backend = FakeGraduationBackend(snapshot)
    store = Store(config.service.state_dir / "state.db")
    queued, _, _ = store.ensure_idea_run(snapshot, "codex")
    running = store.claim_next_idea("worker", 180, 1, {"codex": 1})
    assert running is not None and running.id == queued.id
    graduator = Graduator(config, config_path, backend, store=store)
    plan = graduator.plan("solo/idea")

    result = graduator.apply("solo/idea", plan.approval_id)

    updated = load_config(config_path)
    assert updated.github.allowed_repositories == ("solo/project", "solo/idea")
    assert updated.ideas.repositories == ()
    assert updated.repository("solo/idea").instruction == "keep this repository policy"
    assert [issue.title for issue in backend.issues] == ["Partial feature", "New feature"]
    assert backend.archive_arguments == (
        "solo/idea",
        {
            "expected_commit": "b" * 40,
            "default_branch": "main",
            "spec_path": "idea/SPEC.md",
            "progress_path": "idea/PROGRESS.md",
            "archive_prefix": f"archive/ideas/{'a' * 12}",
        },
    )
    retired = store.get_idea_run_by_id(queued.id)
    assert retired is not None and retired.state == IdeaRunState.SUPERSEDED
    assert retired.phase == "graduated"
    assert retired.lease_owner is None
    assert store.get_idea_project("solo/idea").preview_state == "stopped"
    assert result.issue_urls == (
        "https://github.test/solo/idea/issues/1",
        "https://github.test/solo/idea/issues/2",
    )
    assert (config.service.preview_dir / "control" / "allowlist.json").read_text().count("solo/idea") == 0


def test_apply_rejects_unreviewed_plan_without_mutation(tmp_path) -> None:
    config_path = _write_config(tmp_path / "config.toml", tmp_path)
    original = config_path.read_bytes()
    config = load_config(config_path)
    spec = _spec()
    backend = FakeGraduationBackend(_snapshot(spec, _progress(spec)))

    with pytest.raises(GraduationError, match="approval does not match"):
        Graduator(config, config_path, backend).apply("solo/idea", "wrong-plan")

    assert config_path.read_bytes() == original
    assert backend.issues == ()
    assert backend.archive_arguments is None


def test_apply_claims_approved_config_before_mutating_issues(tmp_path) -> None:
    config_path = _write_config(tmp_path / "config.toml", tmp_path)
    config = load_config(config_path)
    spec = _spec()

    class OrderingBackend(FakeGraduationBackend):
        def ensure_issues(self, repository, issues):
            transitioned = load_config(config_path)
            assert transitioned.ideas.repositories == ()
            assert repository in transitioned.github.allowed_repositories
            return super().ensure_issues(repository, issues)

    backend = OrderingBackend(_snapshot(spec, _progress(spec)))
    graduator = Graduator(config, config_path, backend)

    graduator.apply("solo/idea", graduator.plan("solo/idea").approval_id)

    assert backend.issues


def test_config_change_after_preflight_aborts_before_issue_mutation(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path / "config.toml", tmp_path)
    config = load_config(config_path)
    spec = _spec()
    backend = FakeGraduationBackend(_snapshot(spec, _progress(spec)))
    graduator = Graduator(config, config_path, backend)
    approval_id = graduator.plan("solo/idea").approval_id
    original_preflight = graduation._preflight_atomic_write

    def concurrent_edit(path):
        original_preflight(path)
        path.write_bytes(path.read_bytes() + b"\n# concurrent operator edit\n")

    monkeypatch.setattr(graduation, "_preflight_atomic_write", concurrent_edit)

    with pytest.raises(GraduationError, match="configuration changed"):
        graduator.apply("solo/idea", approval_id)

    assert b"concurrent operator edit" in config_path.read_bytes()
    assert backend.issues == ()
    assert backend.archive_arguments is None


@pytest.mark.parametrize("changed_input", ["spec", "branch", "config"])
def test_apply_invalidates_approval_when_any_fingerprinted_input_changes(tmp_path, changed_input) -> None:
    config_path = _write_config(tmp_path / "config.toml", tmp_path)
    config = load_config(config_path)
    spec = _spec()
    backend = FakeGraduationBackend(_snapshot(spec, _progress(spec)))
    graduator = Graduator(config, config_path, backend)
    approval_id = graduator.plan("solo/idea").approval_id

    if changed_input == "spec":
        changed_spec = _spec("The accepted wish changed.")
        backend.snapshot = replace(
            backend.snapshot,
            spec_hash="d" * 40,
            spec_content=changed_spec,
            previous_progress=_progress(changed_spec),
            base_commit="e" * 40,
        )
    elif changed_input == "branch":
        backend.snapshot = replace(backend.snapshot, base_commit="e" * 40)
    else:
        config_path.write_bytes(config_path.read_bytes() + b"\n# operator edit\n")

    with pytest.raises(GraduationError, match="approval does not match"):
        graduator.apply("solo/idea", approval_id)

    assert backend.issues == ()
    assert backend.archive_arguments is None


def test_archive_failure_restores_ideas_allowlist_and_leaves_run_queued(tmp_path) -> None:
    config_path = _write_config(tmp_path / "config.toml", tmp_path)
    original = config_path.read_bytes()
    config = load_config(config_path)
    spec = _spec()
    snapshot = _snapshot(spec, _progress(spec))
    backend = FakeGraduationBackend(snapshot, archive_error=GraduationError("branch moved"))
    store = Store(config.service.state_dir / "state.db")
    queued, _, _ = store.ensure_idea_run(snapshot, "codex")
    graduator = Graduator(config, config_path, backend, store=store)

    with pytest.raises(GraduationError, match="branch moved"):
        graduator.apply("solo/idea", graduator.plan("solo/idea").approval_id)

    assert config_path.read_bytes() == original
    restored = store.get_idea_run_by_id(queued.id)
    assert restored is not None and restored.state == IdeaRunState.QUEUED
    assert restored.phase == "graduation-rollback"
    assert restored.retry_requested
    assert store.get_idea_project("solo/idea").preview_state == "unknown"
    assert "solo/idea" in (config.service.preview_dir / "control" / "allowlist.json").read_text()


def test_archive_failure_does_not_overwrite_a_later_operator_config_edit(tmp_path) -> None:
    config_path = _write_config(tmp_path / "config.toml", tmp_path)
    config = load_config(config_path)
    spec = _spec()

    class ConcurrentEditBackend(FakeGraduationBackend):
        def archive_idea_files(self, repository, **kwargs):
            config_path.write_bytes(config_path.read_bytes() + b"\n# later operator edit\n")
            raise GraduationError("branch moved")

    backend = ConcurrentEditBackend(_snapshot(spec, _progress(spec)))
    graduator = Graduator(config, config_path, backend)

    with pytest.raises(GraduationError, match="rollback was incomplete"):
        graduator.apply("solo/idea", graduator.plan("solo/idea").approval_id)

    content = config_path.read_bytes()
    assert b"later operator edit" in content
    assert b'allowed_repositories = ["solo/project", "solo/idea"]' in content


def test_preview_allowlist_failure_aborts_before_config_or_archive_and_requeues_work(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path / "config.toml", tmp_path)
    original = config_path.read_bytes()
    config = load_config(config_path)
    spec = _spec()
    snapshot = _snapshot(spec, _progress(spec))
    backend = FakeGraduationBackend(snapshot)
    store = Store(config.service.state_dir / "state.db")
    queued, _, _ = store.ensure_idea_run(snapshot, "codex")
    graduator = Graduator(config, config_path, backend, store=store)
    monkeypatch.setattr(
        graduation.PreviewQueue,
        "publish_allowlist",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("preview control is read-only")),
    )

    with pytest.raises(PermissionError, match="read-only"):
        graduator.apply("solo/idea", graduator.plan("solo/idea").approval_id)

    assert config_path.read_bytes() == original
    assert backend.archive_arguments is None
    restored = store.get_idea_run_by_id(queued.id)
    assert restored is not None and restored.state == IdeaRunState.QUEUED
    assert restored.phase == "graduation-rollback"
    assert store.get_idea_project("solo/idea").preview_state == "unknown"


def test_apply_uses_one_global_graduation_lock(tmp_path) -> None:
    config_path = _write_config(tmp_path / "config.toml", tmp_path)
    config = load_config(config_path)
    spec = _spec()
    backend = FakeGraduationBackend(_snapshot(spec, _progress(spec)))
    store = Store(config.service.state_dir / "state.db")
    assert store.acquire_operation_lock(GRADUATION_LOCK, "other-graduation", 60)
    graduator = Graduator(config, config_path, backend, store=store)

    with pytest.raises(GraduationError, match="another graduation operation"):
        graduator.apply("solo/idea", graduator.plan("solo/idea").approval_id)

    assert backend.issues == ()
    assert backend.archive_arguments is None


def test_config_rewrite_handles_multiline_arrays_and_remains_valid(tmp_path) -> None:
    config_path = _write_config(tmp_path / "config.toml", tmp_path)
    config = load_config(config_path)

    updated = graduated_config_bytes(config_path, config, "solo/idea")
    candidate = tmp_path / "candidate.toml"
    candidate.write_bytes(updated)
    parsed = load_config(candidate)

    assert parsed.github.allowed_repositories == ("solo/project", "solo/idea")
    assert parsed.ideas.repositories == ()
    assert parsed.repository("solo/project").instruction == "existing tier one"


def test_github_issue_creation_reuses_marker_matches(monkeypatch) -> None:
    first = GraduationIssue("first", "First", "body one", "partial", "<!-- marker:first -->")
    second = GraduationIssue("second", "Second", "body two", "not started", "<!-- marker:second -->")

    def fake_run(args, *, json_output=False):
        if args == ["api", "repos/solo/idea"]:
            return {"private": True}
        if args == ["api", "--paginate", "--slurp", "repos/solo/idea/issues?state=all&per_page=100"]:
            return [
                [
                    {
                        "number": 41,
                        "body": f"old\n{first.marker}",
                        "html_url": "https://github.test/existing",
                        "labels": [
                            {"name": "agent:ready"},
                            {"name": "agent:codex"},
                            {"name": "unrelated"},
                        ],
                    }
                ]
            ]
        raise AssertionError(args)

    created = []
    monkeypatch.setattr(GhCLIBackend, "_run", fake_run)
    monkeypatch.setattr(
        graduation,
        "_api_json",
        lambda method, path, payload: created.append((method, path, payload))
        or {
            "html_url": (
                "https://github.test/existing" if method == "PATCH" else "https://github.test/new"
            )
        },
    )

    urls = GhGraduationBackend(("solo/idea",)).ensure_issues("solo/idea", (first, second))

    assert urls == ("https://github.test/existing", "https://github.test/new")
    assert created == [
        (
            "PATCH",
            "repos/solo/idea/issues/41",
            {"title": "First", "body": "body one", "state": "open", "labels": ["unrelated"]},
        ),
        (
            "POST",
            "repos/solo/idea/issues",
            {"title": "Second", "body": "body two", "labels": []},
        ),
    ]


def test_github_archive_atomically_moves_only_contract_assets(monkeypatch) -> None:
    commit_sha = "b" * 40
    tree_sha = "d" * 40
    rows = [
        {"path": "idea/SPEC.md", "type": "blob", "mode": "100644", "sha": "1" * 40},
        {"path": "idea/PROGRESS.md", "type": "blob", "mode": "100644", "sha": "2" * 40},
        {"path": "idea/assets/demo.png", "type": "blob", "mode": "100644", "sha": "3" * 40},
        {"path": ".symphony/idea.toml", "type": "blob", "mode": "100644", "sha": "4" * 40},
        {"path": "idea/README.md", "type": "blob", "mode": "100644", "sha": "5" * 40},
        {"path": "src/app.py", "type": "blob", "mode": "100644", "sha": "6" * 40},
    ]

    def fake_run(args, *, json_output=False):
        if args == ["api", "repos/solo/idea"]:
            return {"private": True}
        if args == ["api", "repos/solo/idea/commits/main"]:
            return {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}
        assert args == ["api", f"repos/solo/idea/git/trees/{commit_sha}?recursive=1"]
        return {"truncated": False, "tree": rows}

    calls = []

    def fake_api(method, path, payload):
        calls.append((method, path, payload))
        if path.endswith("/git/trees"):
            return {"sha": "e" * 40}
        if path.endswith("/git/commits"):
            return {"sha": "c" * 40}
        return {"object": {"sha": "c" * 40}}

    monkeypatch.setattr(GhCLIBackend, "_run", fake_run)
    monkeypatch.setattr(graduation, "_api_json", fake_api)

    result = GhGraduationBackend(("solo/idea",)).archive_idea_files(
        "solo/idea",
        expected_commit=commit_sha,
        default_branch="main",
        spec_path="idea/SPEC.md",
        progress_path="idea/PROGRESS.md",
        archive_prefix="archive/ideas/plan",
    )

    assert result == "c" * 40
    tree_payload = calls[0][2]
    assert tree_payload["base_tree"] == tree_sha
    paths = {row["path"] for row in tree_payload["tree"]}
    assert "archive/ideas/plan/idea/SPEC.md" in paths
    assert "archive/ideas/plan/idea/assets/demo.png" in paths
    assert "archive/ideas/plan/.symphony/idea.toml" in paths
    assert "idea/SPEC.md" in paths
    assert "idea/README.md" not in paths
    assert "src/app.py" not in paths
    assert calls[-1] == (
        "PATCH",
        "repos/solo/idea/git/refs/heads/main",
        {"sha": "c" * 40, "force": False},
    )
