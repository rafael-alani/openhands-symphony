from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from conftest import issue

from symphony.intake import branch_name
from symphony.models import JobState
from symphony.store import Store


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
    started = store.begin_attempt(claimed.id)
    store.transition(
        started.id,
        JobState.FAILED,
        phase="provider-tool-failure",
        terminal_reason="provider launch failed",
    )

    retried = store.request_control(original.repository, original.issue_number, "retry")

    assert retried and retried.attempt == 1
    detail = json.loads(store.events(original.id)[-1]["detail_json"])
    assert detail["reset_pre_provider_attempts"] is False


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
