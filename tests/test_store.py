from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from conftest import issue

from symphony.intake import branch_name
from symphony.models import IdeaRunState, IdeaSnapshot, JobState
from symphony.store import SCHEMA_VERSION, Store


def _add(store: Store, snapshot, key: str | None = None):
    return store.ensure_job(
        snapshot,
        "codex",
        None,
        False,
        branch_name(snapshot.number, snapshot.title),
        key or snapshot.repository,
    )[0]


def test_duplicate_intake_coalesces_to_one_job(tmp_path):
    store = Store(tmp_path / "state.db")
    snapshot = issue()
    first = _add(store, snapshot)
    second, created = store.ensure_job(snapshot, "codex", None, False, first.branch, snapshot.repository)
    assert not created
    assert first.id == second.id
    assert len(store.list_jobs()) == 1


def test_fresh_database_uses_source_neutral_repository_leases(tmp_path):
    store = Store(tmp_path / "state.db")

    with store.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(leases)")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert {"run_kind", "run_id", "repository", "provider"} <= columns
    assert "job_id" not in columns
    assert version == SCHEMA_VERSION


def test_v3_migration_preserves_active_lease_and_is_idempotent(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    original = _add(store, issue())
    claimed = store.claim_next("worker-a", 60, 2, {"codex": 2})
    assert claimed
    with store.transaction() as connection:
        connection.execute("DROP INDEX leases_provider_idx")
        connection.execute("ALTER TABLE leases RENAME TO leases_v4_current")
        connection.execute(
            """
            CREATE TABLE leases (
                concurrency_key TEXT PRIMARY KEY,
                job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
                owner TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO leases(concurrency_key, job_id, owner, expires_at, heartbeat_at)
            SELECT concurrency_key, run_id, owner, expires_at, heartbeat_at FROM leases_v4_current
            """
        )
        connection.execute("DROP TABLE leases_v4_current")
        connection.execute("PRAGMA user_version=3")

    migrated = Store(path)
    rerun = Store(path)
    with rerun.connect() as connection:
        lease = dict(connection.execute("SELECT * FROM leases").fetchone())

    assert lease["run_kind"] == "github-issue"
    assert lease["run_id"] == original.id
    assert lease["repository"] == original.repository
    assert lease["provider"] == original.implementation_provider
    assert migrated.get_job_by_id(original.id).lease_owner == "worker-a"


def _idea_snapshot(spec_hash: str = "spec-one") -> IdeaSnapshot:
    return IdeaSnapshot(
        repository="solo/idea",
        spec_hash=spec_hash,
        spec_content=b"---\nsymphony: idea\nrepo: solo/idea\n---\n\n## Wish\n\nBuild it.\n",
        runtime_content=(
            b'provider = "codex"\n[preview]\nstart = ["python3", "-m", "app"]\n'
            b'port = 4317\nhealth_path = "/health"\nstartup_timeout_seconds = 20\n'
        ),
        previous_progress=b"",
        base_commit="a" * 40,
        default_branch="main",
    )


def test_duplicate_idea_spec_hash_coalesces_to_one_run(tmp_path):
    store = Store(tmp_path / "state.db")

    first, created, _ = store.ensure_idea_run(_idea_snapshot(), "codex")
    duplicate, duplicate_created, _ = store.ensure_idea_run(_idea_snapshot(), "codex")

    assert created
    assert not duplicate_created
    assert first.id == duplicate.id
    assert len(store.list_idea_runs()) == 1


def test_returning_to_an_older_spec_creates_one_new_run_and_retains_history(tmp_path):
    store = Store(tmp_path / "state.db")
    first, _, _ = store.ensure_idea_run(_idea_snapshot(), "codex")
    claimed = store.claim_next_idea("worker", 60, 2, {"codex": 2})
    store.transition_idea_run(claimed.id, IdeaRunState.PUBLISHED, published_commit="first")
    second, _, _ = store.ensure_idea_run(_idea_snapshot("spec-two"), "codex")
    restored, created, superseded = store.ensure_idea_run(_idea_snapshot(), "codex")
    assert created and restored.id != first.id
    assert [run.id for run in superseded] == [second.id]
    assert store.get_idea_run_by_id(first.id).published_commit == "first"
    assert store.get_idea_run("solo/idea", "spec-one").id == restored.id
    assert store.last_completed_idea_run("solo/idea").id == first.id
    duplicate, created, _ = store.ensure_idea_run(_idea_snapshot(), "codex")
    assert not created and duplicate.id == restored.id
    claimed = store.claim_next_idea("worker", 60, 2, {"codex": 2})
    assert claimed.id == restored.id
    store.transition_idea_run(claimed.id, IdeaRunState.PUBLISHED, published_commit="restored")
    assert store.last_completed_idea_run("solo/idea").id == restored.id


def test_schema7_upgrade_preserves_runs_events_validations_and_active_leases(tmp_path):
    import re

    from symphony.models import ValidationResult

    store = Store(tmp_path / "state.db")
    run, _, _ = store.ensure_idea_run(_idea_snapshot(), "codex")
    claimed = store.claim_next_idea("worker", 60, 2, {"codex": 2})
    store.record_idea_validation(run.id, claimed.attempt, ValidationResult(("true",), 0, "start", "end", "ok"))
    with store.connect() as connection:
        definition = connection.execute("SELECT sql FROM sqlite_master WHERE name='idea_runs'").fetchone()[0]
        indexes = [row[0] for row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='idea_runs' AND sql IS NOT NULL"
        )]
        legacy = re.sub(r"\)\s*$", ", UNIQUE(repository, spec_hash))", definition)
        legacy = legacy.replace("CREATE TABLE idea_runs", "CREATE TABLE legacy_runs", 1)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(legacy)
        connection.execute("INSERT INTO legacy_runs SELECT * FROM idea_runs")
        connection.execute("DROP TABLE idea_runs")
        connection.execute("ALTER TABLE legacy_runs RENAME TO idea_runs")
        for statement in indexes:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=7")
        events = connection.execute("SELECT count(*) FROM idea_run_events").fetchone()[0]
    upgraded = Store(store.path)
    assert upgraded.get_idea_run_by_id(run.id).state == IdeaRunState.RUNNING
    assert upgraded.repository_has_lease("solo/idea")
    assert upgraded.idea_validations(run.id)[0]["output"] == "ok"
    with upgraded.connect() as connection:
        assert connection.execute("SELECT count(*) FROM idea_run_events").fetchone()[0] == events
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    upgraded.ensure_idea_run(_idea_snapshot("spec-two"), "codex")
    restored, created, _ = upgraded.ensure_idea_run(_idea_snapshot(), "codex")
    assert created and restored.id != run.id


def test_published_idea_waits_for_preview_health_before_advancing_last_good(tmp_path):
    store = Store(tmp_path / "state.db")
    store.ensure_idea_run(_idea_snapshot(), "codex")
    run = store.claim_next_idea("worker-a", 60, 2, {"codex": 2})
    assert run

    store.transition_idea_run(
        run.id,
        IdeaRunState.PUBLISHED,
        published_commit="b" * 40,
    )

    pending = store.get_idea_project("solo/idea")
    assert pending.preview_state == "pending"
    assert pending.last_good_preview_commit is None

    healthy = store.update_idea_preview("solo/idea", "healthy", last_good_commit="b" * 40)
    assert healthy.preview_state == "healthy"
    assert healthy.last_good_preview_commit == "b" * 40


def test_new_idea_revision_supersedes_running_run_but_keeps_lease_until_canceled(tmp_path):
    store = Store(tmp_path / "state.db")
    first, _, _ = store.ensure_idea_run(_idea_snapshot(), "codex")
    claimed = store.claim_next_idea("worker-a", 60, 2, {"codex": 2})
    assert claimed and claimed.id == first.id

    second, created, superseded = store.ensure_idea_run(_idea_snapshot("spec-two"), "codex")

    assert created
    assert [run.id for run in superseded] == [first.id]
    assert store.get_idea_run_by_id(first.id).state == IdeaRunState.SUPERSEDED
    assert store.claim_next_idea("worker-b", 60, 2, {"codex": 2}) is None
    store.release_idea_lease(first.id)
    next_run = store.claim_next_idea("worker-b", 60, 2, {"codex": 2})
    assert next_run and next_run.id == second.id


def test_issue_and_idea_claims_share_global_and_provider_capacity(tmp_path):
    store = Store(tmp_path / "state.db")
    issue_job = _add(store, issue())
    store.ensure_idea_run(_idea_snapshot(), "codex")

    claimed_issue = store.claim_next("issue-worker", 60, 2, {"codex": 1})

    assert claimed_issue and claimed_issue.id == issue_job.id
    assert store.claim_next_idea("idea-worker", 60, 2, {"codex": 1}) is None
    store.transition(issue_job.id, JobState.PR_OPEN)
    assert store.claim_next_idea("idea-worker", 60, 2, {"codex": 1}) is not None


def test_restart_recovers_expired_lease_without_duplicate(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    original = _add(store, issue())
    claimed = store.claim_next("worker-a", 60, 2, {"codex": 2})
    assert claimed and claimed.id == original.id
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with store.transaction() as connection:
        connection.execute("UPDATE leases SET expires_at=?", (expired,))
    restarted = Store(path)
    recovered = restarted.reap_expired_leases()
    assert recovered == [original.id]
    job = restarted.get_job_by_id(original.id)
    assert job and job.state == JobState.QUEUED
    assert len(restarted.list_jobs()) == 1


def test_expired_paused_lease_stays_stopped_until_explicit_resume(tmp_path):
    store = Store(tmp_path / "state.db")
    original = _add(store, issue())
    claimed = store.claim_next("worker-a", 60, 2, {"codex": 2})
    assert claimed
    store.request_control(original.repository, original.issue_number, "pause")
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with store.transaction() as connection:
        connection.execute("UPDATE leases SET expires_at=?", (expired,))

    store.reap_expired_leases()

    paused = store.get_job_by_id(original.id)
    assert paused and paused.state == JobState.NEEDS_GUIDANCE
    assert paused.pause_requested
    assert store.claim_next("worker-b", 60, 2, {"codex": 2}) is None


def test_expired_lease_cannot_be_reclaimed_before_reconciliation(tmp_path):
    store = Store(tmp_path / "state.db")
    original = _add(store, issue())
    claimed = store.claim_next("worker-a", 60, 2, {"codex": 2})
    assert claimed
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with store.transaction() as connection:
        connection.execute("UPDATE leases SET expires_at=?", (expired,))
        connection.execute("UPDATE jobs SET state=? WHERE id=?", (JobState.QUEUED, original.id))

    assert store.claim_next("worker-b", 60, 2, {"codex": 2}) is None


def test_repository_concurrency_lease_blocks_second_issue_but_not_other_repository(tmp_path):
    store = Store(tmp_path / "state.db")
    first = _add(store, issue(number=1))
    _add(store, issue(number=2, title="Second task"))
    other = _add(store, issue("solo/other", 1, title="Other repository"))
    claimed_one = store.claim_next("worker-a", 60, 3, {"codex": 3})
    claimed_two = store.claim_next("worker-b", 60, 3, {"codex": 3})
    assert claimed_one and claimed_one.id == first.id
    assert claimed_two and claimed_two.id == other.id
    assert store.get_job("solo/project", 2).state == JobState.QUEUED


def test_sequential_claims_rotate_repositories_before_draining_one_backlog(tmp_path):
    store = Store(tmp_path / "state.db")
    first = _add(store, issue(number=1))
    second = _add(store, issue(number=2, title="Second same-repository task"))
    other = _add(store, issue("solo/other", 1, title="Other repository"))

    claimed = store.claim_next("worker", 60, 1, {"codex": 1})
    assert claimed and claimed.id == first.id
    store.transition(claimed.id, JobState.PR_OPEN)

    claimed = store.claim_next("worker", 60, 1, {"codex": 1})
    assert claimed and claimed.id == other.id
    store.transition(claimed.id, JobState.PR_OPEN)

    claimed = store.claim_next("worker", 60, 1, {"codex": 1})
    assert claimed and claimed.id == second.id


def test_operation_lock_is_durable_and_exclusive(tmp_path):
    path = tmp_path / "state.db"
    first = Store(path)
    second = Store(path)

    assert first.acquire_operation_lock("status:job", "worker-a", 60)
    assert not second.acquire_operation_lock("status:job", "worker-b", 60)
    first.release_operation_lock("status:job", "worker-a")
    assert second.acquire_operation_lock("status:job", "worker-b", 60)


def test_claim_does_not_count_an_attempt_until_provider_work_begins(tmp_path):
    store = Store(tmp_path / "state.db")
    original = _add(store, issue())

    claimed = store.claim_next("worker-a", 60, 2, {"codex": 2})

    assert claimed and claimed.id == original.id
    assert claimed.attempt == 0
    started = store.begin_attempt(claimed.id)
    assert started.attempt == 1
    assert [event["kind"] for event in store.events(original.id)][-2:] == ["claimed", "attempt-started"]


def test_retry_resets_attempts_polluted_by_legacy_setup_failure(tmp_path):
    store = Store(tmp_path / "state.db")
    original = _add(store, issue())
    claimed = store.claim_next("worker-a", 60, 2, {"codex": 2})
    assert claimed
    store.transition(
        claimed.id,
        JobState.BLOCKED,
        phase="setup-failed",
        terminal_reason="legacy setup wrapper failed",
    )
    with store.transaction() as connection:
        connection.execute("UPDATE jobs SET attempt=4 WHERE id=?", (original.id,))

    retried = store.request_control(original.repository, original.issue_number, "retry")

    assert retried and retried.state == JobState.QUEUED
    assert retried.attempt == 0
    detail = json.loads(store.events(original.id)[-1]["detail_json"])
    assert detail["reset_pre_provider_attempts"] is True


def test_retry_preserves_real_provider_attempts(tmp_path):
    store = Store(tmp_path / "state.db")
    original = _add(store, issue())
    claimed = store.claim_next("worker-a", 60, 2, {"codex": 2})
    assert claimed
    started = store.begin_attempt(
        claimed.id,
        conversation_id="conversation-1",
        session_id="session-1",
    )
    store.transition(
        started.id,
        JobState.FAILED,
        phase="provider-tool-failure",
        terminal_reason="provider launch failed",
    )

    retried = store.request_control(original.repository, original.issue_number, "retry")

    assert retried and retried.attempt == 1
    assert retried.conversation_id == "conversation-1"
    detail = json.loads(store.events(original.id)[-1]["detail_json"])
    assert detail["reset_pre_provider_attempts"] is False


def test_retry_resets_legacy_attempts_when_provider_never_created_a_conversation(tmp_path):
    store = Store(tmp_path / "state.db")
    original = _add(store, issue())
    claimed = store.claim_next("worker-a", 60, 2, {"codex": 2})
    assert claimed
    started = store.begin_attempt(claimed.id)
    assert started.attempt == 1
    failed = store.transition(
        started.id,
        JobState.FAILED,
        phase="provider-tool-failure",
        terminal_reason="Agent Server rejected the workspace before creating a conversation",
    )
    assert failed.conversation_id is None

    retried = store.request_control(original.repository, original.issue_number, "retry")

    assert retried and retried.state == JobState.QUEUED
    assert retried.attempt == 0
    detail = json.loads(store.events(original.id)[-1]["detail_json"])
    assert detail["reset_pre_provider_attempts"] is True


def test_retry_can_reset_preconversation_attempts_while_job_is_already_queued(tmp_path):
    store = Store(tmp_path / "state.db")
    original = _add(store, issue())
    claimed = store.claim_next("worker-a", 60, 2, {"codex": 2})
    assert claimed
    started = store.begin_attempt(claimed.id)
    queued = store.transition(
        started.id,
        JobState.QUEUED,
        phase="provider-tool-retry",
        actionable_message="Agent Server rejected the workspace",
    )
    assert queued.attempt == 1

    retried = store.request_control(original.repository, original.issue_number, "retry")

    assert retried and retried.state == JobState.QUEUED
    assert retried.attempt == 0
    assert retried.retry_requested


def test_transition_event_preserves_failure_detail(tmp_path):
    store = Store(tmp_path / "state.db")
    original = _add(store, issue())

    failed = store.transition(
        original.id,
        JobState.FAILED,
        phase="orchestrator-failure",
        actionable_message="inspect the service",
        terminal_reason="exact root cause",
        validation_summary="validation did not run",
    )

    assert failed.state == JobState.FAILED
    detail = json.loads(store.events(original.id)[-1]["detail_json"])
    assert detail["actionable_message"] == "inspect the service"
    assert detail["terminal_reason"] == "exact root cause"
    assert detail["validation_summary"] == "validation did not run"
