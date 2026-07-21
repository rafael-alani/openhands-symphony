from __future__ import annotations

import hashlib
import json
import random
import shlex
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import Config
from .github import GitHubBackend, GitHubError, StaleIssueError
from .intake import branch_name, route
from .models import IssueSnapshot, Job, JobState, ProviderOutcome, ProviderRun
from .prompting import implementation_prompt, review_prompt
from .providers.base import ProviderAdapter
from .providers.openhands import OpenHandsProviderError
from .reports import ReportWriter
from .status import render_status
from .store import Store, StoreError
from .workspace import WorkspaceError, WorkspaceManager, redact, run_validation


class IntakeError(RuntimeError):
    pass


class Coordinator:
    def __init__(
        self,
        config: Config,
        store: Store,
        github: GitHubBackend,
        providers: dict[str, ProviderAdapter],
    ):
        self.config = config
        self.store = store
        self.github = github
        self.providers = providers
        self.workspaces = WorkspaceManager(config.service.workspace_dir)
        self.reports = ReportWriter(config.service.report_dir, store)
        self._active_runs: dict[str, tuple[ProviderAdapter, ProviderRun]] = {}
        self._active_lock = threading.Lock()
        self._operation_owner = f"coordinator:{uuid.uuid4()}"
        self._provider_slots = {
            name: threading.BoundedSemaphore(config.scheduler.provider_concurrency.get(name, 1))
            for name in providers
            if config.scheduler.provider_concurrency.get(name, 1) > 0
        }

    def _available_reviewers(self) -> set[str]:
        candidates: set[str] = set()
        for name, provider in self.providers.items():
            if (
                provider.capabilities.autonomous_available
                and provider.capabilities.supports_review
                and self.config.scheduler.provider_concurrency.get(name, 1) > 0
            ):
                try:
                    if provider.auth_status().authenticated:
                        candidates.add(name)
                except Exception:
                    pass
        return candidates

    def enqueue(self, snapshot: IssueSnapshot) -> tuple[Job, bool]:
        if snapshot.repository not in self.config.github.allowed_repositories:
            raise IntakeError(f"repository is not allowlisted: {snapshot.repository}")
        if self.config.github.private_only and not snapshot.private:
            raise IntakeError("public repositories are disabled")
        existing = self.store.get_job(snapshot.repository, snapshot.number)
        decision = route(snapshot, self._available_reviewers())
        if not decision.eligible or not decision.implementation_provider:
            if existing and existing.state in {JobState.QUEUED, JobState.RUNNING, JobState.REVIEWING}:
                self._halt_ineligible(existing, snapshot, decision.reason or "live issue is no longer eligible")
            raise IntakeError(decision.reason)

        try:
            concurrency_key = self.config.concurrency_key(snapshot.repository, snapshot.labels)
        except ValueError as exc:
            if existing and existing.state in {JobState.QUEUED, JobState.RUNNING, JobState.REVIEWING}:
                self._halt_ineligible(existing, snapshot, str(exc))
            raise IntakeError(str(exc)) from None

        branch = branch_name(snapshot.number, snapshot.title)
        if existing:
            if existing.state == JobState.QUEUED and existing.attempt == 0:
                self.store.update_snapshot_if_unclaimed(
                    snapshot,
                    implementation_provider=decision.implementation_provider,
                    review_provider=decision.review_provider,
                    review_required=decision.review_required,
                    branch=branch,
                    concurrency_key=concurrency_key,
                )
            elif existing.state in {JobState.QUEUED, JobState.RUNNING, JobState.REVIEWING} and (
                existing.content_hash != snapshot.content_hash()
                or existing.implementation_provider != decision.implementation_provider
                or existing.review_provider != decision.review_provider
                or existing.review_required != decision.review_required
                or existing.concurrency_key != concurrency_key
            ):
                self._halt_ineligible(existing, snapshot, "issue specification or routing changed after claim")
                raise IntakeError("issue specification or routing changed after claim; explicitly resume to accept it")
            return self.store.get_job_by_id(existing.id) or existing, False

        open_pr = self.github.find_open_pr(snapshot.repository, branch)
        remote_branch = self.github.remote_branch_exists(snapshot.repository, branch)
        job, created = self.store.ensure_job(
            snapshot,
            decision.implementation_provider,
            decision.review_provider,
            decision.review_required,
            branch,
            concurrency_key,
        )
        if open_pr:
            job = self.store.update_job(
                job.id, pr_number=open_pr.number, pr_url=open_pr.url, phase="adopted-existing-pr"
            )
            job = self.store.transition(
                job.id, JobState.PR_OPEN, actionable_message="Existing implementation PR adopted safely."
            )
            self._sync_status(job)
            return job, created
        if remote_branch:
            job = self.store.transition(
                job.id,
                JobState.NEEDS_GUIDANCE,
                phase="orphan-branch",
                actionable_message=f"Generated branch `{branch}` already exists without an open PR; inspect it and use /agent retry.",
            )
            self._sync_status(job)
            return job, created

        provider = self.providers.get(decision.implementation_provider)
        if provider is None or not provider.capabilities.autonomous_available:
            limitation = provider.capabilities.limitation if provider else "provider is not configured"
            job = self.store.transition(
                job.id,
                JobState.NEEDS_GUIDANCE,
                phase="provider-unavailable",
                actionable_message=limitation,
            )
            self._sync_status(job)
            return job, created
        if self.config.scheduler.provider_concurrency.get(decision.implementation_provider, 1) <= 0:
            job = self.store.transition(
                job.id,
                JobState.NEEDS_GUIDANCE,
                phase="provider-disabled",
                actionable_message=f"{decision.implementation_provider} has a configured concurrency limit of zero.",
            )
            self._sync_status(job)
            return job, created
        auth = provider.auth_status()
        if not auth.available or not auth.authenticated:
            job = self.store.transition(
                job.id,
                JobState.NEEDS_GUIDANCE,
                phase="authentication-required",
                actionable_message=(
                    f"{decision.implementation_provider} authentication is unavailable: {redact(auth.detail, 2000)}"
                ),
            )
        elif decision.review_required and decision.review_provider:
            reviewer = self.providers.get(decision.review_provider)
            review_auth = reviewer.auth_status() if reviewer else None
            if (
                reviewer is None
                or not reviewer.capabilities.autonomous_available
                or not reviewer.capabilities.supports_review
                or self.config.scheduler.provider_concurrency.get(decision.review_provider, 1) <= 0
                or review_auth is None
                or not review_auth.authenticated
            ):
                detail = redact(review_auth.detail, 2000) if review_auth else "review provider is not configured"
                job = self.store.transition(
                    job.id,
                    JobState.NEEDS_GUIDANCE,
                    phase="review-authentication-required",
                    actionable_message=f"{decision.review_provider} review is unavailable: {detail}",
                )
        self._sync_status(job)
        return job, created

    def _durable_run(self, job: Job) -> tuple[ProviderAdapter, ProviderRun] | None:
        provider_name = job.implementation_provider
        conversation_id = job.conversation_id
        session_id = job.session_id
        if job.state == JobState.REVIEWING and job.phase != "review-repair":
            provider_name = job.review_provider or ""
            conversation_id = job.review_conversation_id
            session_id = job.review_session_id
        provider = self.providers.get(provider_name)
        if provider and conversation_id:
            return provider, ProviderRun(provider_name, conversation_id, session_id)
        return None

    def _cancel_active_run(self, job: Job) -> None:
        with self._active_lock:
            active = self._active_runs.get(job.id)
        selected = active or self._durable_run(job)
        if not selected:
            return
        try:
            selected[0].cancel(selected[1])
        except Exception:
            pass

    def _halt_ineligible(self, job: Job, snapshot: IssueSnapshot, reason: str) -> None:
        command = "cancel" if snapshot.state.lower() != "open" else "pause"
        halted = self.store.request_control(job.repository, job.issue_number, command)
        if not halted:
            return
        halted = self.store.update_job(
            halted.id,
            actionable_message=f"Autonomous work stopped after live issue revalidation: {redact(reason, 2000)}",
        )
        self._cancel_active_run(job)
        if halted.state not in {JobState.RUNNING, JobState.REVIEWING}:
            self._sync_status(halted)
            self.reports.write(self.store.get_job_by_id(halted.id) or halted)

    def reconcile(self) -> list[tuple[str, int, str]]:
        results = self.recover_expired_leases()
        for job in self.store.list_jobs({JobState.PR_OPEN}):
            if not job.pr_number:
                continue
            try:
                pr_state = self.github.pr_state(job.repository, job.pr_number)
                if pr_state.get("mergedAt"):
                    job = self.store.transition(
                        job.id,
                        JobState.DONE,
                        phase="merged",
                        terminal_reason=f"Pull request #{job.pr_number} was merged by a human.",
                    )
                    self._sync_status(job)
                    self.reports.write(self.store.get_job_by_id(job.id) or job)
                    results.append((job.repository, job.issue_number, "done: merged"))
                    continue
                context = self.github.pr_review_context(job.repository, job.pr_number)
                summary = self._validation_with_ci(job.validation_summary, context)
                if summary != job.validation_summary:
                    job = self.store.update_job(job.id, validation_summary=summary)
                    self.store.record_event(job.id, "ci-observed", {"summary": self._ci_summary(context)})
                    self.github.update_pr_validation(job, self._validation_markdown(job))
                    self._sync_status(job)
                    self.reports.write(self.store.get_job_by_id(job.id) or job)
                results.append((job.repository, job.issue_number, f"ci: {self._ci_summary(context)}"))
            except GitHubError as exc:
                results.append((job.repository, job.issue_number, f"pr-status-error: {exc}"))
        for repository in self.config.github.allowed_repositories:
            try:
                for snapshot in self.github.list_ready_issues(repository):
                    try:
                        job, created = self.enqueue(snapshot)
                        results.append((repository, snapshot.number, "created" if created else str(job.state)))
                    except IntakeError as exc:
                        results.append((repository, snapshot.number, f"ineligible: {exc}"))
            except GitHubError as exc:
                results.append((repository, 0, f"github-error: {exc}"))
            try:
                for issue_number, comment_id, command in self.github.list_control_commands(repository):
                    _, applied = self.apply_control_comment(repository, issue_number, comment_id, command)
                    if applied:
                        results.append((repository, issue_number, f"recovered command: {command}"))
            except (GitHubError, StoreError, IntakeError) as exc:
                results.append((repository, 0, f"command-reconcile-error: {exc}"))
        return results

    def apply_control_comment(
        self, repository: str, issue_number: int, comment_id: int, command: str
    ) -> tuple[Job | None, bool]:
        """Apply a trusted command exactly once across webhook and reconciliation processes."""
        delivery_id = f"issue-comment:{repository}:{comment_id}"
        if self.store.delivery_recorded(delivery_id):
            return self.store.get_job(repository, issue_number), False
        lock_name = f"control:{repository}:{comment_id}"
        if not self.store.acquire_operation_lock(lock_name, self._operation_owner):
            return self.store.get_job(repository, issue_number), False
        try:
            if self.store.delivery_recorded(delivery_id):
                return self.store.get_job(repository, issue_number), False
            job = self.control(repository, issue_number, command)
            payload_hash = hashlib.sha256(f"{repository}#{issue_number}:{comment_id}:{command}".encode()).hexdigest()
            self.store.record_delivery(
                delivery_id,
                "issue_comment_command",
                payload_hash,
                repository,
                issue_number,
            )
            return job, True
        finally:
            self.store.release_operation_lock(lock_name, self._operation_owner)

    def recover_expired_leases(self) -> list[tuple[str, int, str]]:
        """Cancel durable provider work before making an expired lease runnable again."""
        results: list[tuple[str, int, str]] = []
        for job in self.store.expired_lease_jobs():
            selected = self._durable_run(job)
            if selected:
                provider, run = selected
                try:
                    provider.cancel(run)
                    self.store.record_event(
                        job.id,
                        "expired-run-canceled",
                        {"provider": run.provider, "conversation_id": run.conversation_id},
                    )
                except Exception as exc:
                    detail = redact(f"{type(exc).__name__}: {exc}", 2000)
                    if job.state == JobState.REVIEWING and job.pr_number:
                        recovered = self.store.transition(
                            job.id,
                            JobState.PR_OPEN,
                            phase="recovery-cancel-failed",
                            actionable_message=(
                                "The expired review/repair conversation could not be canceled safely; "
                                f"the draft PR is preserved. Inspect OpenHands before retrying: {detail}"
                            ),
                            release_lease=True,
                        )
                    else:
                        recovered = self.store.transition(
                            job.id,
                            JobState.NEEDS_GUIDANCE,
                            phase="recovery-cancel-failed",
                            actionable_message=(
                                "The expired implementation conversation could not be canceled safely. "
                                f"Inspect OpenHands, then explicitly retry: {detail}"
                            ),
                            release_lease=True,
                        )
                    self._sync_status(recovered)
                    self.reports.write(self.store.get_job_by_id(job.id) or recovered)
                    results.append((job.repository, job.issue_number, "expired-run cancel failed; manual guidance"))
                    continue
            elif job.conversation_id or job.review_conversation_id:
                provider_name = job.review_provider if job.state == JobState.REVIEWING else job.implementation_provider
                conversation_id = job.review_conversation_id if job.state == JobState.REVIEWING else job.conversation_id
                recovered = self.store.transition(
                    job.id,
                    JobState.PR_OPEN if job.pr_number else JobState.NEEDS_GUIDANCE,
                    phase="recovery-provider-unavailable",
                    actionable_message=(
                        f"Cannot safely cancel expired conversation {conversation_id}: provider {provider_name!r} "
                        "is unavailable. Inspect OpenHands, then explicitly retry."
                    ),
                    release_lease=True,
                )
                self._sync_status(recovered)
                self.reports.write(self.store.get_job_by_id(job.id) or recovered)
                results.append((job.repository, job.issue_number, "expired-run provider unavailable"))
                continue

            reaped = self.store.reap_expired_leases({job.id})
            if not reaped:
                continue
            recovered = self.store.get_job_by_id(job.id)
            if recovered and job.state == JobState.REVIEWING and recovered.state == JobState.PR_OPEN:
                recovered = self.store.request_control(job.repository, job.issue_number, "retry")
            if recovered:
                self._sync_status(recovered)
                self.reports.write(self.store.get_job_by_id(job.id) or recovered)
            results.append((job.repository, job.issue_number, "expired lease recovered safely"))
        return results

    def control(self, repository: str, issue_number: int, command: str) -> Job | None:
        before = self.store.get_job(repository, issue_number)
        job = self.store.request_control(repository, issue_number, command)
        self.github.set_control_state(repository, issue_number, command)
        if job is None:
            if command in {"resume", "retry"}:
                snapshot = self.github.get_issue(repository, issue_number)
                job, _ = self.enqueue(snapshot)
            return job
        if command in {"pause", "cancel"} and before:
            self._cancel_active_run(before)
        if command in {"resume", "retry"} and job.state == JobState.QUEUED:
            snapshot = self.github.get_issue(repository, issue_number)
            decision = route(snapshot, self._available_reviewers())
            if not decision.eligible or not decision.implementation_provider:
                job = self.store.transition(
                    job.id,
                    JobState.NEEDS_GUIDANCE,
                    phase="invalid-explicit-requeue",
                    actionable_message=f"Cannot resume with the current live routing: {redact(decision.reason, 2000)}",
                )
                self._sync_status(job)
                self.reports.write(self.store.get_job_by_id(job.id) or job)
                return job
            job = self.store.accept_snapshot(job.id, snapshot)
            try:
                concurrency_key = self.config.concurrency_key(snapshot.repository, snapshot.labels)
            except ValueError as exc:
                job = self.store.transition(
                    job.id,
                    JobState.NEEDS_GUIDANCE,
                    phase="invalid-concurrency-scope",
                    actionable_message=f"Cannot resume with the current concurrency scope: {redact(str(exc), 2000)}",
                )
                self._sync_status(job)
                self.reports.write(self.store.get_job_by_id(job.id) or job)
                return job
            job = self.store.update_job(
                job.id,
                implementation_provider=decision.implementation_provider,
                review_provider=decision.review_provider,
                review_required=decision.review_required,
                concurrency_key=concurrency_key,
            )
        self._sync_status(job)
        self.reports.write(self.store.get_job_by_id(job.id) or job)
        return job

    def _sync_status(self, job: Job) -> None:
        lock_name = f"status:{job.id}"
        if not self.store.acquire_operation_lock(lock_name, self._operation_owner):
            return
        try:
            self.github.set_state_labels(job, job.state)
            refreshed = self.store.get_job_by_id(job.id) or job
            comment_id = self.github.update_status_comment(refreshed, render_status(refreshed))
            if refreshed.status_comment_id != comment_id:
                self.store.update_job(job.id, status_comment_id=comment_id)
        except (GitHubError, StaleIssueError):
            # Durable state/reporting remains authoritative while external control-plane state is unavailable.
            pass
        finally:
            self.store.release_operation_lock(lock_name, self._operation_owner)

    def _finish(self, job: Job) -> Job:
        current = self.store.get_job_by_id(job.id) or job
        self._sync_status(current)
        current = self.store.get_job_by_id(job.id) or current
        self.reports.write(current)
        return current

    def _stop_heartbeat(self, event: threading.Event, thread: threading.Thread) -> None:
        event.set()
        thread.join(timeout=5)

    def _start_heartbeat(self, job: Job) -> tuple[threading.Event, threading.Thread]:
        stop = threading.Event()

        def heartbeat() -> None:
            canceled = False
            while not stop.wait(self.config.scheduler.heartbeat_seconds):
                if not self.store.renew_lease(job.id, job.lease_owner or "", self.config.scheduler.lease_seconds):
                    self._cancel_active_run(self.store.get_job_by_id(job.id) or job)
                    return
                current = self.store.get_job_by_id(job.id)
                if current and (current.cancel_requested or current.pause_requested) and not canceled:
                    canceled = True
                    self._cancel_active_run(current)

        thread = threading.Thread(target=heartbeat, name=f"heartbeat-{job.id[:8]}", daemon=True)
        thread.start()
        return stop, thread

    def _wait(self, job: Job, provider: ProviderAdapter, run: ProviderRun):
        with self._active_lock:
            self._active_runs[job.id] = (provider, run)
        try:
            return provider.wait(run, self.config.providers[provider.name].timeout_seconds)
        finally:
            with self._active_lock:
                self._active_runs.pop(job.id, None)

    def _require_live_lease(self, job: Job) -> None:
        if not self.store.renew_lease(job.id, job.lease_owner or "", self.config.scheduler.lease_seconds):
            raise StoreError("worker lease expired before external mutation")
        live = self.github.get_issue(job.repository, job.issue_number)
        try:
            live_key = self.config.concurrency_key(job.repository, live.labels)
        except ValueError as exc:
            raise StaleIssueError(f"concurrency scope changed after claim: {exc}") from None
        if live_key != job.concurrency_key:
            raise StaleIssueError("concurrency scope changed after claim")

    @staticmethod
    def _with_jitter(seconds: int) -> int:
        return seconds + random.SystemRandom().randint(0, max(1, min(seconds // 4, 60)))

    @contextmanager
    def _provider_slot(self, job: Job, provider: ProviderAdapter):
        semaphore = self._provider_slots.get(provider.name)
        if semaphore is None:
            raise OpenHandsProviderError(f"{provider.name} has no available provider concurrency slot")
        while not semaphore.acquire(timeout=self.config.scheduler.heartbeat_seconds):
            if not self.store.renew_lease(job.id, job.lease_owner or "", self.config.scheduler.lease_seconds):
                raise OpenHandsProviderError(f"lease expired while waiting for a {provider.name} provider slot")
        try:
            yield
        finally:
            semaphore.release()

    def _validation_commands(self, job: Job, worktree: Path) -> tuple[tuple[str, ...], ...]:
        commands = self.config.repository(job.repository).validation_commands
        if commands:
            return commands
        quality_gate = worktree / ".openhands" / "quality-gate.sh"
        if quality_gate.is_file():
            return (("bash", ".openhands/quality-gate.sh"),)
        return ()

    def _run_validations(self, job: Job, worktree: Path) -> tuple[bool, str, list[object]]:
        commands = self._validation_commands(job, worktree)
        if not commands:
            return (
                False,
                "No validation commands or .openhands/quality-gate.sh are configured; proof is required before push.",
                [],
            )
        results = []
        for command in commands:
            self.workspaces.verify_integrity(job, worktree)
            result = run_validation(
                command,
                worktree,
                self.config.scheduler.validation_timeout_seconds,
                run_as_user=self.config.service.validation_user,
            )
            self.store.record_validation(job.id, job.attempt, result)
            results.append(result)
            self.workspaces.verify_integrity(job, worktree)
        passed = sum(1 for result in results if result.ok)
        summary = f"{passed}/{len(results)} configured commands passed"
        return passed == len(results), summary, results

    @staticmethod
    def _ci_summary(context: dict[str, object]) -> str:
        rollup = context.get("statusCheckRollup")
        if not isinstance(rollup, list) or not rollup:
            return "not reported"
        checks = []
        for item in rollup[:20]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("context") or "check")
            state = str(item.get("conclusion") or item.get("state") or item.get("status") or "UNKNOWN")
            checks.append(f"{name}={state}")
        return ", ".join(checks) or "not reported"

    @classmethod
    def _validation_with_ci(cls, validation_summary: str, context: dict[str, object]) -> str:
        local = validation_summary.split("; CI:", 1)[0]
        return f"{local}; CI: {cls._ci_summary(context)}"

    @staticmethod
    def _validation_failure_prompt(results: list[object], validation_summary: str) -> str:
        sections = []
        for result in results:
            if not getattr(result, "ok", False):
                command = " ".join(result.command)
                output = result.output[-6000:]
                sections.append(f"Command: {command}\nObserved output:\n{output}")
        if not sections:
            sections.append(f"Wrapper validation could not start:\n{validation_summary}")
        return (
            "The wrapper could not obtain passing validation evidence. Correct only the observed problem, create a "
            "truthful `.openhands/quality-gate.sh` if the repository has no configured gate, rerun relevant checks, "
            "and leave the workspace ready. Do not push or use GitHub.\n\n" + "\n\n".join(sections)
        )

    def _transition_from_provider_result(self, job: Job, result) -> Job | None:
        current = self.store.get_job_by_id(job.id) or job
        question = redact(result.question_or_reason or "", 4000)
        summary = redact(result.summary or "", 4000)
        if current.cancel_requested:
            return self.store.transition(
                job.id, JobState.CANCELED, phase="canceled", terminal_reason="Canceled by operator."
            )
        if current.pause_requested:
            return self.store.transition(
                job.id,
                JobState.NEEDS_GUIDANCE,
                phase="paused",
                actionable_message=(
                    current.actionable_message
                    or "Paused by operator; workspace and session were preserved. Use /agent resume."
                ),
            )
        if result.outcome == ProviderOutcome.NEEDS_GUIDANCE:
            return self.store.transition(
                job.id,
                JobState.NEEDS_GUIDANCE,
                phase="needs-guidance",
                actionable_message=question or "The agent requested additional guidance.",
            )
        if result.outcome == ProviderOutcome.BLOCKED:
            return self.store.transition(
                job.id,
                JobState.BLOCKED,
                phase="blocked",
                actionable_message=question,
                terminal_reason=question or summary,
            )
        if result.outcome == ProviderOutcome.FAILED:
            failure_kind = str(result.raw.get("failure_kind") or "implementation")
            if failure_kind == "quota":
                backoff = self.config.scheduler.provider_backoff_base_seconds
                detail = question or summary
                if job.attempt >= self.config.scheduler.max_attempts:
                    return self.store.transition(
                        job.id,
                        JobState.FAILED,
                        phase="provider-quota-failure",
                        terminal_reason=f"Provider quota/rate limit exhausted {job.attempt} attempts: {detail}",
                    )
                self.store.set_provider_backoff(job.implementation_provider, detail, self._with_jitter(backoff))
                return self.store.transition(
                    job.id,
                    JobState.QUEUED,
                    phase="provider-quota-backoff",
                    actionable_message=f"Provider quota/rate limit observed; retrying after backoff: {detail}",
                )
            if failure_kind == "authentication":
                return self.store.transition(
                    job.id,
                    JobState.FAILED,
                    phase="authentication-failure",
                    actionable_message=question,
                    terminal_reason=question or summary,
                )
            if failure_kind == "provider-tool":
                if job.attempt < self.config.scheduler.max_attempts:
                    backoff = min(
                        self.config.scheduler.provider_backoff_base_seconds * (2 ** max(0, job.attempt - 1)),
                        self.config.scheduler.provider_backoff_max_seconds,
                    )
                    detail = question or summary
                    self.store.set_provider_backoff(job.implementation_provider, detail, self._with_jitter(backoff))
                    return self.store.transition(
                        job.id,
                        JobState.QUEUED,
                        phase="provider-tool-retry",
                        actionable_message=f"Provider/tool failure; retrying after backoff: {detail}",
                    )
                return self.store.transition(
                    job.id,
                    JobState.FAILED,
                    phase="provider-tool-failure",
                    terminal_reason=question or summary,
                )
            return self.store.transition(
                job.id,
                JobState.FAILED,
                phase="implementation-failed",
                actionable_message=question,
                terminal_reason=question or summary,
            )
        if result.outcome == ProviderOutcome.CANCELED:
            return self.store.transition(
                job.id, JobState.CANCELED, phase="canceled", terminal_reason="Agent process was canceled."
            )
        return None

    def run_claimed(self, claimed: Job) -> Job:
        stop, thread = self._start_heartbeat(claimed)
        try:
            return self._run_claimed(claimed)
        finally:
            self._stop_heartbeat(stop, thread)

    def _run_claimed(self, claimed: Job) -> Job:
        job = claimed
        try:
            live = self.github.get_issue(job.repository, job.issue_number)
            decision = route(live, {job.review_provider} if job.review_provider else set())
            if live.content_hash() != job.content_hash:
                job = self.store.transition(
                    job.id,
                    JobState.NEEDS_GUIDANCE,
                    phase="issue-edited",
                    actionable_message="Issue title or body changed after claim. Review the edit, then use /agent resume.",
                )
                return self._finish(job)
            if (
                not decision.eligible
                or decision.implementation_provider != job.implementation_provider
                or decision.review_required != job.review_required
                or decision.review_provider != job.review_provider
            ):
                job = self.store.transition(
                    job.id,
                    JobState.NEEDS_GUIDANCE,
                    phase="routing-changed",
                    actionable_message=f"Live routing no longer matches the claim: {decision.reason or 'provider changed'}.",
                )
                return self._finish(job)
            try:
                live_concurrency_key = self.config.concurrency_key(job.repository, live.labels)
            except ValueError as exc:
                job = self.store.transition(
                    job.id,
                    JobState.NEEDS_GUIDANCE,
                    phase="concurrency-scope-changed",
                    actionable_message=f"Live concurrency scope no longer matches the claim: {redact(str(exc), 2000)}",
                )
                return self._finish(job)
            if live_concurrency_key != job.concurrency_key:
                job = self.store.transition(
                    job.id,
                    JobState.NEEDS_GUIDANCE,
                    phase="concurrency-scope-changed",
                    actionable_message="Live concurrency scope no longer matches the claim.",
                )
                return self._finish(job)
            provider = self.providers.get(job.implementation_provider)
            if provider is None:
                job = self.store.transition(
                    job.id,
                    JobState.NEEDS_GUIDANCE,
                    phase="provider-configuration-changed",
                    actionable_message=(
                        f"Provider {job.implementation_provider!r} is no longer enabled; inspect configuration and retry."
                    ),
                )
                return self._finish(job)
            self._sync_status(job)
            if job.attempt > self.config.scheduler.max_attempts:
                job = self.store.transition(
                    job.id,
                    JobState.FAILED,
                    phase="retry-limit",
                    terminal_reason=f"Maximum of {self.config.scheduler.max_attempts} attempts reached.",
                )
                return self._finish(job)

            existing_pr = self.github.find_open_pr(job.repository, job.branch)
            if existing_pr:
                job = self.store.update_job(job.id, pr_number=existing_pr.number, pr_url=existing_pr.url)
                if not job.review_required:
                    job = self.store.transition(job.id, JobState.PR_OPEN, phase="recovered-existing-pr")
                    return self._finish(job)
                worktree = self.workspaces.checkout(job, live)
                job = self.store.update_job(job.id, worktree=str(worktree), phase="recovered-existing-pr-review")
                self.workspaces.prepare_for_agent(worktree)
                self.workspaces.verify_integrity(job, worktree)
                job = self.store.transition(
                    job.id,
                    JobState.PR_OPEN,
                    phase="recovered-existing-pr-review",
                    release_lease=False,
                )
                self._sync_status(job)
                return self._finish(self._review(job, worktree))

            auth = provider.auth_status()
            healthy, health_detail = provider.health()
            quota = provider.quota_or_rate_limit_state()
            if not auth.authenticated:
                job = self.store.transition(
                    job.id,
                    JobState.FAILED,
                    phase="authentication-failure",
                    terminal_reason=(
                        f"{job.implementation_provider} authentication failed: {redact(auth.detail, 2000)}"
                    ),
                )
                return self._finish(job)
            if quota.limited:
                backoff = quota.retry_after_seconds or self.config.scheduler.provider_backoff_base_seconds
                quota_detail = redact(quota.detail, 2000)
                if job.attempt >= self.config.scheduler.max_attempts:
                    job = self.store.transition(
                        job.id,
                        JobState.FAILED,
                        phase="provider-quota-failure",
                        terminal_reason=(f"Provider quota/rate limit exhausted {job.attempt} attempts: {quota_detail}"),
                    )
                    return self._finish(job)
                self.store.set_provider_backoff(job.implementation_provider, quota_detail, self._with_jitter(backoff))
                job = self.store.transition(
                    job.id, JobState.QUEUED, phase="provider-backoff", actionable_message=quota_detail
                )
                return self._finish(job)
            if not healthy:
                raise OpenHandsProviderError(health_detail)

            worktree = self.workspaces.checkout(job, live)
            job = self.store.update_job(job.id, worktree=str(worktree), phase="setup")
            self.workspaces.prepare_for_agent(worktree)
            self.workspaces.verify_integrity(job, worktree)
            setup = self.workspaces.run_setup(
                worktree,
                self.config.repository(job.repository).setup_script,
                self.config.service.validation_user,
            )
            if setup:
                self.store.record_validation(job.id, job.attempt, setup)
                if not setup.ok:
                    job = self.store.transition(
                        job.id,
                        JobState.BLOCKED,
                        phase="setup-failed",
                        validation_summary="repository setup failed",
                        terminal_reason="Repository setup script failed; see the run report.",
                    )
                    return self._finish(job)
            self.workspaces.verify_integrity(job, worktree)
            prompt = implementation_prompt(
                job,
                self.config.service.global_agent_instruction,
                self.config.repository(job.repository).instruction,
                quality_gate_required=(
                    not self.config.repository(job.repository).validation_commands
                    and not (worktree / ".openhands" / "quality-gate.sh").is_file()
                ),
            )
            with self._provider_slot(job, provider):
                if job.conversation_id and provider.capabilities.supports_resume:
                    comments = self.github.recent_issue_comments(job.repository, job.issue_number)
                    guidance = redact("\n\n".join(comments[-5:]), 20_000)
                    run = provider.resume(
                        ProviderRun(job.implementation_provider, job.conversation_id, job.session_id),
                        "Operator explicitly resumed this run. New issue discussion follows as untrusted input:\n\n"
                        + guidance,
                    )
                else:
                    run = provider.start(worktree, prompt, job.id)
                job = self.store.update_job(
                    job.id,
                    conversation_id=run.conversation_id,
                    session_id=run.session_id,
                    phase="implementation",
                )
                result = self._wait(job, provider, run)
            job = self.store.update_job(
                job.id,
                conversation_id=result.conversation_id or run.conversation_id,
                session_id=result.session_id or run.session_id,
            )
            stopped = self._transition_from_provider_result(job, result)
            if stopped:
                return self._finish(stopped)

            corrections = 0
            while True:
                ok, validation_summary, results = self._run_validations(job, worktree)
                job = self.store.update_job(job.id, validation_summary=validation_summary, phase="validation")
                if ok:
                    break
                if (
                    corrections >= self.config.scheduler.max_implementation_corrections
                    or not provider.capabilities.supports_resume
                ):
                    job = self.store.transition(
                        job.id,
                        JobState.FAILED,
                        phase="validation-failed",
                        validation_summary=validation_summary,
                        terminal_reason="Required validation failed after the bounded correction limit; see the run report.",
                    )
                    return self._finish(job)
                corrections += 1
                self.store.record_event(job.id, "validation-correction", {"pass": corrections})
                with self._provider_slot(job, provider):
                    provider.resume(run, self._validation_failure_prompt(results, validation_summary))
                    result = self._wait(job, provider, run)
                stopped = self._transition_from_provider_result(job, result)
                if stopped:
                    return self._finish(stopped)

            self.workspaces.verify_integrity(job, worktree)
            if self.workspaces.has_changes(worktree):
                self.workspaces.commit(worktree, job.issue_number)
            elif self.workspaces.commits_ahead(worktree, live.default_branch) == 0:
                job = self.store.transition(
                    job.id,
                    JobState.FAILED,
                    phase="no-changes",
                    terminal_reason="The agent completed but produced no implementation changes.",
                )
                return self._finish(job)

            branch_exists = self.github.remote_branch_exists(job.repository, job.branch)
            branch_matches = branch_exists and self.workspaces.remote_matches(worktree, job.repository, job.branch)
            self._require_live_lease(job)
            self.github.guard_code_mutation(job, allow_existing_branch=branch_matches)
            if not branch_matches:
                self.workspaces.push(worktree, job.repository, job.branch)

            job = self.store.update_job(job.id, phase="pull-request")
            self._require_live_lease(job)
            pr = self.github.create_draft_pr(
                job,
                f"[agent] {live.title}",
                self._pr_body(job, result.summary),
                self.config.github.generated_pr_label,
            )
            job = self.store.update_job(job.id, pr_number=pr.number, pr_url=pr.url)
            ci_context = self.github.pr_review_context(job.repository, pr.number)
            job = self.store.update_job(
                job.id,
                validation_summary=self._validation_with_ci(job.validation_summary, ci_context),
            )
            self.store.record_event(job.id, "ci-observed", {"summary": self._ci_summary(ci_context)})
            self._require_live_lease(job)
            self.github.update_pr_validation(job, self._validation_markdown(job))
            job = self.store.transition(job.id, JobState.PR_OPEN, phase="pr-open", release_lease=False)
            self._sync_status(job)

            if job.review_required:
                job = self._review(job, worktree)
            else:
                job = self.store.transition(job.id, JobState.PR_OPEN, phase="pr-open", release_lease=True)
            return self._finish(job)
        except StaleIssueError as exc:
            current = self.store.get_job_by_id(job.id) or job
            if current.pr_number:
                job = self.store.transition(
                    job.id,
                    JobState.PR_OPEN,
                    phase="mutation-guard",
                    actionable_message=f"The draft PR is preserved; autonomous mutation stopped: {redact(str(exc), 2000)}",
                    release_lease=True,
                )
            else:
                job = self.store.transition(
                    job.id,
                    JobState.NEEDS_GUIDANCE,
                    phase="mutation-guard",
                    actionable_message=redact(str(exc), 2000),
                )
            return self._finish(job)
        except OpenHandsProviderError as exc:
            detail = redact(str(exc), 4000)
            current = self.store.get_job_by_id(job.id) or job
            limited_provider = ""
            retry_after = 0
            for name, candidate in self.providers.items():
                try:
                    quota_state = candidate.quota_or_rate_limit_state()
                except Exception:
                    continue
                if quota_state.limited:
                    limited_provider = name
                    retry_after = quota_state.retry_after_seconds or self.config.scheduler.provider_backoff_base_seconds
                    detail = redact(quota_state.detail or detail, 4000)
                    break
            if limited_provider:
                self.store.set_provider_backoff(limited_provider, detail, self._with_jitter(retry_after))
                if current.pr_number:
                    job = self.store.transition(
                        job.id,
                        JobState.PR_OPEN,
                        phase="review-provider-quota-backoff",
                        actionable_message=(
                            f"Provider {limited_provider} reached a quota/rate limit; the draft PR is preserved: {detail}"
                        ),
                        release_lease=True,
                    )
                else:
                    job = self.store.transition(
                        job.id,
                        JobState.QUEUED,
                        phase="provider-quota-backoff",
                        actionable_message=f"Provider quota/rate limit observed; retrying after backoff: {detail}",
                    )
                return self._finish(job)
            if current.pr_number and current.state in {JobState.PR_OPEN, JobState.REVIEWING}:
                job = self.store.transition(
                    job.id,
                    JobState.PR_OPEN,
                    phase="review-provider-failure",
                    actionable_message=f"Review provider/tool failed; the draft PR is preserved: {detail}",
                    release_lease=True,
                )
                return self._finish(job)
            if job.attempt < self.config.scheduler.max_attempts:
                backoff = min(
                    self.config.scheduler.provider_backoff_base_seconds * (2 ** max(0, job.attempt - 1)),
                    self.config.scheduler.provider_backoff_max_seconds,
                )
                self.store.set_provider_backoff(job.implementation_provider, detail, self._with_jitter(backoff))
                job = self.store.transition(
                    job.id,
                    JobState.QUEUED,
                    phase="provider-tool-retry",
                    actionable_message=f"Provider/tool failure; retrying after backoff: {detail}",
                )
            else:
                job = self.store.transition(
                    job.id,
                    JobState.FAILED,
                    phase="provider-tool-failure",
                    terminal_reason=f"Provider/tool failure exhausted {job.attempt} attempts: {detail}",
                )
            return self._finish(job)
        except (GitHubError, WorkspaceError, StoreError, OSError) as exc:
            detail = redact(f"{type(exc).__name__}: {exc}", 4000)
            current = self.store.get_job_by_id(job.id) or job
            if current.pr_number and current.state in {JobState.PR_OPEN, JobState.REVIEWING}:
                job = self.store.transition(
                    job.id,
                    JobState.PR_OPEN,
                    phase="review-orchestrator-failure",
                    actionable_message=f"Review failed; the draft PR is preserved: {detail}",
                    release_lease=True,
                )
            elif current.state == JobState.RUNNING:
                job = self.store.transition(
                    job.id,
                    JobState.FAILED,
                    phase="orchestrator-failure",
                    terminal_reason=detail,
                )
            else:
                job = current
            return self._finish(job)
        except Exception as exc:
            # A programming/runtime exception must not leave a claimed lease
            # silently running forever. Preserve any already-created PR.
            current = self.store.get_job_by_id(job.id) or job
            detail = redact(f"{type(exc).__name__}: {exc}", 4000)
            if current.pr_number and current.state in {JobState.PR_OPEN, JobState.REVIEWING}:
                job = self.store.transition(
                    job.id,
                    JobState.PR_OPEN,
                    phase="unexpected-review-failure",
                    actionable_message=f"Unexpected review/orchestrator failure; draft PR preserved: {detail}",
                    release_lease=True,
                )
            else:
                job = self.store.transition(
                    job.id,
                    JobState.FAILED,
                    phase="unexpected-orchestrator-failure",
                    terminal_reason=detail,
                )
            return self._finish(job)

    def _pr_body(self, job: Job, summary: str) -> str:
        validation = self._validation_markdown(job)
        return f"""Closes #{job.issue_number}

## Summary

{redact(summary, 20_000)}

## Provenance

- Implementation provider: `{job.implementation_provider}`
- Run ID: `{job.id}`
- Attempt: `{job.attempt}`

## Validation

{validation}

## Unresolved risks

- Human review and CI are still required before merge.
- No production deployment or automatic merge was performed.
"""

    def _validation_markdown(self, job: Job) -> str:
        validations = self.store.validations(job.id)
        lines = []
        for result in validations:
            command = shlex.join(json.loads(str(result["command_json"])))
            status = "PASS" if result["exit_code"] == 0 and not result["timed_out"] else "FAIL"
            lines.append(f"- `{command}` — **{status}** (exit `{result['exit_code']}`)")
        if "; CI:" in job.validation_summary:
            lines.append(f"- CI at last observation: `{job.validation_summary.split('; CI:', 1)[1].strip()}`")
        evidence = "\n".join(lines) or "- No validation ran (PR creation should have been blocked)."
        return (
            evidence + "\n\nFull command output is retained in the VM run report. "
            "No command is reported as passing unless the wrapper observed exit code zero."
        )

    @staticmethod
    def _review_body(summary: str, provider: str, run_id: str, recommendation: str) -> str:
        narrative = redact(summary, 20_000)
        required = ("Blocker", "High", "Medium", "Low", "Validation", "Residual risks")
        missing = [
            heading for heading in required if f"## {heading}" not in narrative and f"### {heading}" not in narrative
        ]
        supplement = ""
        if missing:
            supplement = "\n\n" + "\n\n".join(
                f"## {heading}\n\nNo separately categorized entry was returned; inspect the reviewer narrative above."
                for heading in missing
            )
        return (
            f"Independent provider: `{provider}`  \n"
            f"Run ID: `{run_id}`  \n"
            f"Recommendation: **{recommendation}**\n\n"
            f"{narrative}{supplement}"
        )

    def _review(self, job: Job, worktree: Path) -> Job:
        review_name = job.review_provider
        reviewer = self.providers.get(review_name or "")
        if not review_name or reviewer is None or not reviewer.capabilities.supports_review:
            return self.store.transition(
                job.id,
                JobState.PR_OPEN,
                phase="review-unavailable",
                actionable_message="Implementation PR is available, but the configured independent reviewer is unavailable.",
                release_lease=True,
            )
        review_auth = reviewer.auth_status()
        review_healthy, review_health_detail = reviewer.health()
        review_quota = reviewer.quota_or_rate_limit_state()
        if not review_auth.authenticated or not review_healthy or review_quota.limited:
            detail = (
                review_auth.detail
                if not review_auth.authenticated
                else review_quota.detail
                if review_quota.limited
                else review_health_detail
            )
            detail = redact(detail, 2000)
            return self.store.transition(
                job.id,
                JobState.PR_OPEN,
                phase="review-unavailable",
                actionable_message=f"Implementation PR is preserved; independent review is unavailable: {detail}",
                release_lease=True,
            )
        repairs = 0
        while True:
            self._require_live_lease(job)
            self.github.guard_code_mutation(job, allow_existing_branch=True)
            job = self.store.transition(job.id, JobState.REVIEWING, phase="independent-review", release_lease=False)
            github_context = json.dumps(
                self.github.pr_review_context(job.repository, job.pr_number or 0),
                sort_keys=True,
                default=str,
            )[-20_000:]
            with self._provider_slot(job, reviewer):
                review_run = reviewer.start(
                    worktree,
                    review_prompt(
                        job,
                        github_context,
                        self.config.service.global_agent_instruction,
                        self.config.repository(job.repository).instruction,
                    ),
                    f"{job.id}-review-{repairs}",
                    read_only=True,
                )
                job = self.store.update_job(
                    job.id,
                    review_conversation_id=review_run.conversation_id,
                    review_session_id=review_run.session_id,
                )
                review_result = self._wait(job, reviewer, review_run)
            if review_result.outcome != ProviderOutcome.COMPLETED:
                review_failure = redact(review_result.question_or_reason or review_result.summary, 4000)
                return self.store.transition(
                    job.id,
                    JobState.PR_OPEN,
                    phase="review-failed",
                    actionable_message=f"Independent review failed without hiding the PR: {review_failure}",
                    release_lease=True,
                )
            if "review_event" not in review_result.raw or "substantive_findings" not in review_result.raw:
                return self.store.transition(
                    job.id,
                    JobState.PR_OPEN,
                    phase="review-unstructured",
                    actionable_message=(
                        "Independent review did not return the required structured recommendation/findings; "
                        "the draft PR is preserved for retry or human review."
                    ),
                    release_lease=True,
                )
            recommendation = str(review_result.raw.get("review_event") or "comment")
            if recommendation not in {"approve", "request-changes", "comment"}:
                recommendation = "comment"
            # GitHub rejects approving/requesting changes on a PR created by the
            # same bot identity. A COMMENT review is still a real PR review and
            # keeps the independent recommendation in the review body/event log.
            event = "comment"
            self._require_live_lease(job)
            self.github.guard_code_mutation(job, allow_existing_branch=True)
            review_body = self._review_body(
                review_result.summary, review_name, review_run.conversation_id, recommendation
            )
            self.github.post_review(job.repository, job.pr_number or 0, review_body, event)
            self.store.record_event(
                job.id,
                "github-review",
                {
                    "provider": review_name,
                    "event": event,
                    "recommendation": recommendation,
                    "conversation_id": review_run.conversation_id,
                },
            )
            findings = int(
                review_result.raw.get("substantive_findings") or (1 if recommendation == "request-changes" else 0)
            )
            if not findings:
                return self.store.transition(job.id, JobState.PR_OPEN, phase="review-complete", release_lease=True)
            if repairs >= self.config.scheduler.max_review_repairs:
                return self.store.transition(
                    job.id,
                    JobState.PR_OPEN,
                    phase="review-repair-limit",
                    actionable_message="Review findings remain after the bounded repair limit; the draft PR is preserved for human action.",
                    release_lease=True,
                )
            repairs += 1
            implementer = self.providers[job.implementation_provider]
            self.github.guard_code_mutation(job, allow_existing_branch=True)
            implementer_auth = implementer.auth_status()
            implementer_healthy, implementer_health_detail = implementer.health()
            implementer_quota = implementer.quota_or_rate_limit_state()
            if not implementer_auth.authenticated or not implementer_healthy or implementer_quota.limited:
                detail = (
                    implementer_auth.detail
                    if not implementer_auth.authenticated
                    else implementer_quota.detail
                    if implementer_quota.limited
                    else implementer_health_detail
                )
                return self.store.transition(
                    job.id,
                    JobState.PR_OPEN,
                    phase="review-repair-provider-unavailable",
                    actionable_message=f"Review findings remain, but the implementer is unavailable: {redact(detail, 2000)}",
                    release_lease=True,
                )
            repair_prompt = (
                "A fresh independent review requested changes. Apply only well-supported fixes in the existing workspace, "
                "run relevant tests, and do not use GitHub. Review body:\n\n" + review_body
            )
            with self._provider_slot(job, implementer):
                repair_run = implementer.start(worktree, repair_prompt, f"{job.id}-repair-{repairs}")
                job = self.store.update_job(
                    job.id,
                    conversation_id=repair_run.conversation_id,
                    session_id=repair_run.session_id,
                    phase="review-repair",
                )
                repair_result = self._wait(job, implementer, repair_run)
            if repair_result.outcome != ProviderOutcome.COMPLETED:
                return self.store.transition(
                    job.id,
                    JobState.PR_OPEN,
                    phase="review-repair-failed",
                    actionable_message="A reviewer repair pass failed; the original PR remains available.",
                    release_lease=True,
                )
            ok, summary, _ = self._run_validations(job, worktree)
            if not ok:
                return self.store.transition(
                    job.id,
                    JobState.PR_OPEN,
                    phase="review-repair-validation-failed",
                    validation_summary=summary,
                    actionable_message="Review repairs failed required validation; the PR remains open.",
                    release_lease=True,
                )
            if self.workspaces.has_changes(worktree):
                self.workspaces.verify_integrity(job, worktree)
                self.workspaces.commit(worktree, job.issue_number)
                self._require_live_lease(job)
                self.github.guard_code_mutation(job, allow_existing_branch=True)
                self.workspaces.push(worktree, job.repository, job.branch)
            ci_context = self.github.pr_review_context(job.repository, job.pr_number or 0)
            job = self.store.update_job(
                job.id,
                validation_summary=self._validation_with_ci(summary, ci_context),
            )
            self.store.record_event(job.id, "ci-observed", {"summary": self._ci_summary(ci_context)})
            self._require_live_lease(job)
            self.github.update_pr_validation(job, self._validation_markdown(job))
            self.store.record_event(job.id, "review-repair", {"pass": repairs})
