from dataclasses import replace

import pytest
from conftest import issue

from symphony.github import STATUS_MARKER, GhCLIBackend, PullRequest, StaleIssueError
from symphony.intake import branch_name
from symphony.store import Store


def test_contract_labels_skip_matching_remote_labels(monkeypatch):
    backend = GhCLIBackend(("solo/project",))
    calls: list[tuple[list[str], bool]] = []

    def fake_run(args, *, json_output=False):
        calls.append((args, json_output))
        return [{"name": "agent:ready", "color": "0e8a16", "description": "Ready"}]

    monkeypatch.setattr(backend, "_run", fake_run)

    assert backend.ensure_contract_labels("solo/project", {"agent:ready": ("0E8A16", "Ready")}) == 0
    assert calls == [
        (
            [
                "label",
                "list",
                "--repo",
                "solo/project",
                "--limit",
                "1000",
                "--json",
                "name,color,description",
            ],
            True,
        )
    ]


def test_contract_labels_create_only_missing_or_changed_labels(monkeypatch):
    backend = GhCLIBackend(("solo/project",))
    writes: list[list[str]] = []

    def fake_run(args, *, json_output=False):
        if json_output:
            return [{"name": "agent:ready", "color": "FFFFFF", "description": "Old"}]
        writes.append(args)
        return ""

    monkeypatch.setattr(backend, "_run", fake_run)

    changed = backend.ensure_contract_labels(
        "solo/project",
        {
            "agent:ready": ("0E8A16", "Ready"),
            "agent:codex": ("1F6FEB", "Codex"),
        },
    )

    assert changed == 2
    assert [command[2] for command in writes] == ["agent:ready", "agent:codex"]


def test_canonical_status_comment_must_be_owned_by_authenticated_bot(monkeypatch):
    backend = GhCLIBackend(("solo/project",), bot_login="symphony-bot")
    comments = [
        {"id": 1, "body": STATUS_MARKER, "user": {"login": "attacker"}},
        {"id": 2, "body": STATUS_MARKER, "user": {"login": "symphony-bot"}},
    ]

    def fake_run(args, *, json_output=False):
        assert json_output
        return [comments]

    monkeypatch.setattr(backend, "_run", fake_run)
    assert backend._find_status_comment("solo/project", 7) == 2


def test_mutation_guard_rejects_changed_review_routing(tmp_path, monkeypatch):
    snapshot = issue(labels=("agent:ready", "agent:codex", "review:required", "review:claude"))
    store = Store(tmp_path / "state.db")
    job, _ = store.ensure_job(
        snapshot,
        "codex",
        "claude",
        True,
        branch_name(snapshot.number, snapshot.title),
        snapshot.repository,
    )
    backend = GhCLIBackend((snapshot.repository,))
    monkeypatch.setattr(
        backend,
        "get_issue",
        lambda repository, number: replace(snapshot, labels=("agent:ready", "agent:codex")),
    )

    with pytest.raises(StaleIssueError, match="routing changed"):
        backend.guard_code_mutation(job, allow_existing_branch=False)


def test_pr_validation_update_preserves_generated_summary_and_risks(tmp_path, monkeypatch):
    snapshot = issue()
    store = Store(tmp_path / "state.db")
    job, _ = store.ensure_job(
        snapshot,
        "codex",
        None,
        False,
        branch_name(snapshot.number, snapshot.title),
        snapshot.repository,
    )
    job = store.update_job(job.id, pr_number=7, pr_url="https://example.test/pull/7")
    backend = GhCLIBackend((snapshot.repository,))
    original = "## Summary\n\nKeep me.\n\n## Validation\n\n- old\n\n## Unresolved risks\n\n- Keep this too.\n"
    edits: list[list[str]] = []

    def fake_run(args, *, json_output=False):
        if args[:2] == ["pr", "view"]:
            return {"body": original, "headRefName": job.branch, "state": "OPEN"}
        edits.append(args)
        return ""

    monkeypatch.setattr(backend, "_run", fake_run)
    monkeypatch.setattr(backend, "guard_code_mutation", lambda current, allow_existing_branch: snapshot)

    backend.update_pr_validation(job, "- `pytest` — **PASS**")

    updated = edits[0][edits[0].index("--body") + 1]
    assert "Keep me." in updated
    assert "- `pytest` — **PASS**" in updated
    assert "- old" not in updated
    assert "Keep this too." in updated


def test_existing_generated_pr_gets_refreshed_summary_on_rework(tmp_path, monkeypatch):
    snapshot = issue()
    store = Store(tmp_path / "state.db")
    job, _ = store.ensure_job(
        snapshot,
        "codex",
        None,
        False,
        branch_name(snapshot.number, snapshot.title),
        snapshot.repository,
    )
    job = store.update_job(job.id, pr_number=7, pr_url="https://example.test/pull/7")
    backend = GhCLIBackend((snapshot.repository,))
    edits: list[list[str]] = []
    monkeypatch.setattr(backend, "guard_code_mutation", lambda current, allow_existing_branch: snapshot)
    monkeypatch.setattr(
        backend,
        "find_open_pr",
        lambda repository, branch: PullRequest(7, "https://example.test/pull/7", True, branch),
    )
    monkeypatch.setattr(backend, "_run", lambda args, json_output=False: edits.append(args) or "")

    pr = backend.create_draft_pr(job, "Correct title", "Correct structured body", "generated-by-agent")

    assert pr.number == 7
    assert edits == [
        [
            "pr",
            "edit",
            "7",
            "--repo",
            snapshot.repository,
            "--title",
            "Correct title",
            "--body",
            "Correct structured body",
            "--add-label",
            "generated-by-agent",
        ]
    ]
