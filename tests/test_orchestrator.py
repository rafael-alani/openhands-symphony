from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ExistingWorkspace, FakeGitHub, create_worktree, issue, make_config

from symphony.coordinator import Coordinator, IntakeError
from symphony.models import AuthStatus, JobState, ProviderOutcome, QuotaState
from symphony.providers.fake import FakeProvider
from symphony.providers.openhands import OpenHandsProviderError
from symphony.store import Store


def _claim(store: Store, config):
    job = store.claim_next("test-worker", 30, 3, config.scheduler.provider_concurrency)
    assert job
    return job


def test_success_opens_one_draft_pr_with_validation_evidence(tmp_path):
    snapshot = issue()
    config = make_config(tmp_path)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])
    provider = FakeProvider("codex", write_files={"implemented.txt": "ok\n"})
    coordinator = Coordinator(config, store, github, {"codex": provider})
    job, created = coordinator.enqueue(snapshot)
    assert created
    worktree = create_worktree(tmp_path, job.branch)
    coordinator.workspaces = ExistingWorkspace(worktree)
    result = coordinator.run_claimed(_claim(store, config))
    assert result.state == JobState.PR_OPEN
    assert github.pr_creates == 1
    assert github.comment_creates == 1
    assert "Closes #1" in github.pr_bodies[0]
    assert "**PASS**" in github.pr_bodies[0]
    assert "CI: tests=SUCCESS" in result.validation_summary
    assert store.validations(job.id)[0]["exit_code"] == 0
    coalesced, created_again = coordinator.enqueue(snapshot)
    assert not created_again and coalesced.id == job.id
    assert github.pr_creates == 1
    assert github.comment_creates == 1


def test_independent_fresh_reviewer_posts_real_review_and_leaves_pr_open(tmp_path):
    snapshot = issue(labels=("agent:ready", "agent:codex", "review:required", "review:claude"))
    config = make_config(tmp_path, review=True)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])
    implementer = FakeProvider("codex", write_files={"implemented.txt": "ok\n"})
    reviewer = FakeProvider(
        "claude",
        result_data={"review_event": "approve", "substantive_findings": 0},
    )
    coordinator = Coordinator(config, store, github, {"codex": implementer, "claude": reviewer})
    job, _ = coordinator.enqueue(snapshot)
    coordinator.workspaces = ExistingWorkspace(create_worktree(tmp_path, job.branch))
    result = coordinator.run_claimed(_claim(store, config))
    assert result.state == JobState.PR_OPEN
    assert len(implementer.starts) == 1
    assert len(reviewer.starts) == 1
    assert reviewer.starts[0][2].startswith(f"{job.id}-review-")
    assert reviewer.starts[0][3] is True
    assert implementer.starts[0][3] is False
    assert github.reviews[0][3] == "comment"
    for category in ("Blocker", "High", "Medium", "Low", "Validation", "Residual risks"):
        assert f"## {category}" in github.reviews[0][2]
    assert github.pr_creates == 1


def test_required_explicit_reviewer_must_be_authenticated_before_implementation(tmp_path):
    snapshot = issue(labels=("agent:ready", "agent:codex", "review:required", "review:claude"))
    config = make_config(tmp_path, review=True)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])

    class UnauthenticatedReviewer(FakeProvider):
        def auth_status(self):
            return AuthStatus(True, False, "login required")

    implementer = FakeProvider("codex", write_files={"implemented.txt": "ok\n"})
    reviewer = UnauthenticatedReviewer("claude")
    coordinator = Coordinator(config, store, github, {"codex": implementer, "claude": reviewer})

    job, _ = coordinator.enqueue(snapshot)

    assert job.state == JobState.NEEDS_GUIDANCE
    assert job.phase == "review-authentication-required"
    assert implementer.starts == []


def test_unclaimed_issue_refreshes_provider_routing_and_branch(tmp_path):
    snapshot = issue()
    updated = replace(snapshot, title="Use Claude now", labels=("agent:ready", "agent:claude"))
    config = make_config(tmp_path, review=True)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])
    coordinator = Coordinator(
        config,
        store,
        github,
        {"codex": FakeProvider("codex"), "claude": FakeProvider("claude")},
    )
    original, _ = coordinator.enqueue(snapshot)

    refreshed, created = coordinator.enqueue(updated)

    assert not created
    assert refreshed.id == original.id
    assert refreshed.implementation_provider == "claude"
    assert refreshed.branch == "agent/1-use-claude-now"
    assert refreshed.content_hash == updated.content_hash()


def test_active_issue_edit_is_stopped_by_exact_item_revalidation(tmp_path):
    snapshot = issue()
    config = make_config(tmp_path)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])
    provider = FakeProvider("codex")
    coordinator = Coordinator(config, store, github, {"codex": provider})
    job, _ = coordinator.enqueue(snapshot)
    claimed = _claim(store, config)
    store.update_job(claimed.id, conversation_id="active-conversation", session_id="active-session")

    with pytest.raises(IntakeError, match="changed after claim"):
        coordinator.enqueue(replace(snapshot, body="Edited during the active run."))

    stopped = store.get_job_by_id(job.id)
    assert stopped and stopped.pause_requested
    assert provider.cancels == ["active-conversation"]


def test_reviewer_repair_loop_is_bounded_and_preserves_pr(tmp_path):
    snapshot = issue(labels=("agent:ready", "agent:codex", "review:required", "review:claude"))
    config = make_config(tmp_path, review=True)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])
    implementer = FakeProvider("codex", write_files={"implemented.txt": "ok\n"})
    reviewer = FakeProvider(
        "claude",
        result_data={"review_event": "request-changes", "substantive_findings": 1},
    )
    coordinator = Coordinator(config, store, github, {"codex": implementer, "claude": reviewer})
    job, _ = coordinator.enqueue(snapshot)
    coordinator.workspaces = ExistingWorkspace(create_worktree(tmp_path, job.branch))
    result = coordinator.run_claimed(_claim(store, config))
    assert result.state == JobState.PR_OPEN
    assert result.phase == "review-repair-limit"
    assert len(implementer.starts) == 2
    assert len(reviewer.starts) == 2
    assert len(github.reviews) == 2
    assert github.pr_creates == 1


def test_unstructured_reviewer_result_is_not_treated_as_clean(tmp_path):
    snapshot = issue(labels=("agent:ready", "agent:codex", "review:required", "review:claude"))
    config = make_config(tmp_path, review=True)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])
    implementer = FakeProvider("codex", write_files={"implemented.txt": "ok\n"})
    reviewer = FakeProvider("claude")
    coordinator = Coordinator(config, store, github, {"codex": implementer, "claude": reviewer})
    job, _ = coordinator.enqueue(snapshot)
    coordinator.workspaces = ExistingWorkspace(create_worktree(tmp_path, job.branch))

    result = coordinator.run_claimed(_claim(store, config))

    assert result.state == JobState.PR_OPEN
    assert result.phase == "review-unstructured"
    assert github.reviews == []


def test_explicit_retry_resumes_incomplete_review_on_existing_pr(tmp_path):
    snapshot = issue(labels=("agent:ready", "agent:codex", "review:required", "review:claude"))
    config = make_config(tmp_path, review=True)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])
    implementer = FakeProvider("codex", write_files={"implemented.txt": "ok\n"})
    reviewer = FakeProvider("claude", ProviderOutcome.FAILED)
    coordinator = Coordinator(config, store, github, {"codex": implementer, "claude": reviewer})
    job, _ = coordinator.enqueue(snapshot)
    worktree = create_worktree(tmp_path, job.branch)
    coordinator.workspaces = ExistingWorkspace(worktree)
    first = coordinator.run_claimed(_claim(store, config))
    assert first.state == JobState.PR_OPEN
    assert first.phase == "review-failed"

    reviewer.outcome = ProviderOutcome.COMPLETED
    reviewer.result_data = {"review_event": "approve", "substantive_findings": 0}
    requeued = coordinator.control(snapshot.repository, snapshot.number, "retry")
    assert requeued and requeued.state == JobState.QUEUED

    second = coordinator.run_claimed(_claim(store, config))

    assert second.state == JobState.PR_OPEN
    assert second.phase == "review-complete"
    assert len(reviewer.starts) == 2
    assert github.pr_creates == 1
    assert len(github.reviews) == 1


def test_pause_after_restart_targets_durable_reviewer_conversation(tmp_path):
    snapshot = issue(labels=("agent:ready", "agent:codex", "review:required", "review:claude"))
    config = make_config(tmp_path, review=True)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])
    implementer = FakeProvider("codex")
    reviewer = FakeProvider("claude")
    coordinator = Coordinator(config, store, github, {"codex": implementer, "claude": reviewer})
    job, _ = coordinator.enqueue(snapshot)
    job = _claim(store, config)
    job = store.update_job(
        job.id,
        conversation_id="old-implementation",
        review_conversation_id="active-review",
        review_session_id="active-review-session",
        pr_number=7,
    )
    job = store.transition(job.id, JobState.PR_OPEN, release_lease=False)
    store.transition(job.id, JobState.REVIEWING, phase="independent-review", release_lease=False)

    coordinator.control(snapshot.repository, snapshot.number, "pause")

    assert reviewer.cancels == ["active-review"]
    assert implementer.cancels == []


def test_needs_guidance_stops_without_pr(tmp_path):
    snapshot = issue()
    config = make_config(tmp_path)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])
    provider = FakeProvider("codex", ProviderOutcome.NEEDS_GUIDANCE)
    coordinator = Coordinator(config, store, github, {"codex": provider})
    job, _ = coordinator.enqueue(snapshot)
    coordinator.workspaces = ExistingWorkspace(create_worktree(tmp_path, job.branch))
    result = coordinator.run_claimed(_claim(store, config))
    assert result.state == JobState.NEEDS_GUIDANCE
    assert "option A" in result.actionable_message
    assert github.pr_creates == 0


def test_transport_quota_error_is_distinct_from_provider_tool_failure(tmp_path):
    snapshot = issue()
    config = make_config(tmp_path)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])

    class QuotaProvider(FakeProvider):
        def __init__(self):
            super().__init__("codex")
            self.observed = False

        def wait(self, run, timeout_seconds):
            self.observed = True
            raise OpenHandsProviderError("Agent Server HTTP 429")

        def quota_or_rate_limit_state(self):
            if self.observed:
                self.observed = False
                return QuotaState(True, 300, "subscription quota exhausted")
            return QuotaState()

    provider = QuotaProvider()
    coordinator = Coordinator(config, store, github, {"codex": provider})
    job, _ = coordinator.enqueue(snapshot)
    coordinator.workspaces = ExistingWorkspace(create_worktree(tmp_path, job.branch))

    result = coordinator.run_claimed(_claim(store, config))

    assert result.state == JobState.QUEUED
    assert result.phase == "provider-quota-backoff"
    assert "quota" in result.actionable_message


def test_edited_issue_is_revalidated_before_agent_or_code_mutation(tmp_path):
    snapshot = issue()
    config = make_config(tmp_path)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])
    provider = FakeProvider("codex", write_files={"implemented.txt": "ok\n"})
    coordinator = Coordinator(config, store, github, {"codex": provider})
    coordinator.enqueue(snapshot)
    claimed = _claim(store, config)
    github.set_issue(replace(snapshot, body="A materially edited specification."))
    result = coordinator.run_claimed(claimed)
    assert result.state == JobState.NEEDS_GUIDANCE
    assert result.phase == "issue-edited"
    assert provider.starts == []
    assert github.pr_creates == 0


def test_paused_issue_is_revalidated_before_agent_or_code_mutation(tmp_path):
    snapshot = issue()
    config = make_config(tmp_path)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])
    provider = FakeProvider("codex", write_files={"implemented.txt": "ok\n"})
    coordinator = Coordinator(config, store, github, {"codex": provider})
    coordinator.enqueue(snapshot)
    claimed = _claim(store, config)
    github.set_issue(replace(snapshot, labels=(*snapshot.labels, "agent:paused")))
    result = coordinator.run_claimed(claimed)
    assert result.state == JobState.NEEDS_GUIDANCE
    assert provider.starts == []
    assert github.pr_creates == 0


def test_issue_edit_after_implementation_is_guarded_before_push(tmp_path):
    snapshot = issue()
    config = make_config(tmp_path)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])

    class EditingProvider(FakeProvider):
        def wait(self, run, timeout_seconds):
            github.set_issue(replace(snapshot, body="Edited while the agent was running."))
            return super().wait(run, timeout_seconds)

    provider = EditingProvider("codex", write_files={"implemented.txt": "ok\n"})
    coordinator = Coordinator(config, store, github, {"codex": provider})
    job, _ = coordinator.enqueue(snapshot)
    coordinator.workspaces = ExistingWorkspace(create_worktree(tmp_path, job.branch))
    result = coordinator.run_claimed(_claim(store, config))
    assert result.state == JobState.NEEDS_GUIDANCE
    assert result.phase == "mutation-guard"
    assert github.guard_calls == 1
    assert github.pr_creates == 0


def test_pause_after_implementation_is_guarded_before_push(tmp_path):
    snapshot = issue()
    config = make_config(tmp_path)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])

    class PausingProvider(FakeProvider):
        def wait(self, run, timeout_seconds):
            github.set_issue(replace(snapshot, labels=(*snapshot.labels, "agent:paused")))
            return super().wait(run, timeout_seconds)

    provider = PausingProvider("codex", write_files={"implemented.txt": "ok\n"})
    coordinator = Coordinator(config, store, github, {"codex": provider})
    job, _ = coordinator.enqueue(snapshot)
    coordinator.workspaces = ExistingWorkspace(create_worktree(tmp_path, job.branch))
    result = coordinator.run_claimed(_claim(store, config))
    assert result.state == JobState.NEEDS_GUIDANCE
    assert result.phase == "mutation-guard"
    assert github.pr_creates == 0


def test_expired_lease_restart_resumes_same_job_without_duplicate_artifacts(tmp_path):
    snapshot = issue()
    config = make_config(tmp_path)
    path = config.service.state_dir / "state.db"
    store = Store(path)
    github = FakeGitHub([snapshot])
    provider = FakeProvider("codex")
    coordinator = Coordinator(config, store, github, {"codex": provider})
    job, _ = coordinator.enqueue(snapshot)
    worktree = create_worktree(tmp_path, job.branch)
    (worktree / "implemented.txt").write_text("ok\n")
    first_claim = _claim(store, config)
    store.update_job(
        first_claim.id, conversation_id="preserved-conversation", session_id="preserved-session", worktree=str(worktree)
    )
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with store.transaction() as connection:
        connection.execute("UPDATE leases SET expires_at=?", (expired,))

    restarted_store = Store(path)
    assert restarted_store.reap_expired_leases() == [job.id]
    restarted = Coordinator(config, restarted_store, github, {"codex": provider})
    restarted.workspaces = ExistingWorkspace(worktree)
    second_claim = _claim(restarted_store, config)
    result = restarted.run_claimed(second_claim)
    assert result.id == job.id
    assert result.state == JobState.PR_OPEN
    assert provider.starts == []
    assert len(provider.resumes) == 1
    assert github.pr_creates == 1
    assert github.comment_creates == 1


def test_coordinator_cancels_expired_provider_run_before_requeue(tmp_path):
    snapshot = issue()
    config = make_config(tmp_path)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])
    provider = FakeProvider("codex")
    coordinator = Coordinator(config, store, github, {"codex": provider})
    job, _ = coordinator.enqueue(snapshot)
    claimed = _claim(store, config)
    store.update_job(claimed.id, conversation_id="stale-conversation", session_id="stale-session")
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with store.transaction() as connection:
        connection.execute("UPDATE leases SET expires_at=?", (expired,))

    results = coordinator.recover_expired_leases()

    recovered = store.get_job_by_id(job.id)
    assert provider.cancels == ["stale-conversation"]
    assert recovered and recovered.state == JobState.QUEUED
    assert results == [(snapshot.repository, snapshot.number, "expired lease recovered safely")]


def test_reconciliation_marks_human_merged_pr_done(tmp_path):
    snapshot = issue()
    config = make_config(tmp_path)
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])
    provider = FakeProvider("codex")
    coordinator = Coordinator(config, store, github, {"codex": provider})
    job, _ = coordinator.enqueue(snapshot)
    job = store.update_job(job.id, pr_number=19, pr_url="https://example.test/solo/project/pull/19")
    store.transition(job.id, JobState.PR_OPEN, phase="pr-open")
    github.merged_prs.add((snapshot.repository, 19))

    results = coordinator.reconcile()

    done = store.get_job_by_id(job.id)
    assert done and done.state == JobState.DONE
    assert done.terminal_outcome == "done"
    assert (snapshot.repository, snapshot.number, "done: merged") in results
