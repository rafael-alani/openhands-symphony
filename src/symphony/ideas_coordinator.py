from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

from .config import Config
from .execution import ProviderSlots
from .ideas_contract import IdeaContractError, parse_runtime, validate_spec
from .ideas_github import IdeasGitHubBackend
from .ideas_preview import IdeaPreview, PreviewError
from .ideas_progress import (
    SectionResult,
    affected_sections,
    previous_results,
    render_progress,
    sections,
)
from .ideas_prompting import idea_implementation_prompt
from .ideas_reports import IdeaReportWriter
from .models import IdeaRun, IdeaRunState, IdeaSnapshot, ProviderOutcome, ProviderRun
from .preview_queue import PreviewQueue
from .providers.base import ProviderAdapter
from .providers.openhands import OpenHandsProviderError
from .store import Store, StoreError
from .validation import redact, run_validation
from .workspace import NonFastForwardError, WorkspaceError, WorkspaceManager


class IdeasIntakeError(RuntimeError):
    pass


class StaleIdeaError(RuntimeError):
    pass


class IdeasCoordinator:
    def __init__(
        self,
        config: Config,
        store: Store,
        github: IdeasGitHubBackend,
        providers: dict[str, ProviderAdapter],
        provider_slots: ProviderSlots,
    ):
        self.config = config
        self.store = store
        self.github = github
        self.providers = providers
        self.provider_slots = provider_slots
        self.workspaces = WorkspaceManager(config.service.workspace_dir)
        self.preview = IdeaPreview(config.service.validation_user, config.service.state_dir)
        self.preview_deployments = PreviewQueue(config.service.preview_dir, config.service.workspace_dir)
        self.reports = IdeaReportWriter(config.service.report_dir, store)
        self._active_runs: dict[str, tuple[ProviderAdapter, ProviderRun]] = {}
        self._active_lock = threading.Lock()
        self.vault = None

    def observe(self, snapshot: IdeaSnapshot) -> tuple[IdeaRun, bool]:
        if not self.store.vault_allows(snapshot.repository, "idea"):
            raise IdeasIntakeError("Ideas intake is suspended by the project note")
        if snapshot.repository not in self.config.ideas.repositories:
            raise IdeasIntakeError(f"repository is not ideas-allowlisted: {snapshot.repository}")
        if not snapshot.private:
            raise IdeasIntakeError("public ideas repositories are disabled")
        try:
            validate_spec(snapshot.spec_content, snapshot.repository)
            runtime = parse_runtime(snapshot.runtime_content)
        except IdeaContractError as exc:
            raise IdeasIntakeError(str(exc)) from None
        run, created, superseded = self.store.ensure_idea_run(snapshot, runtime.provider)
        for previous in superseded:
            self._cancel_superseded(previous)
        if not created:
            return run, False
        provider = self.providers.get(runtime.provider)
        reason = ""
        if provider is None or not provider.capabilities.autonomous_available:
            reason = provider.capabilities.limitation if provider else "provider is not configured"
        elif self.config.scheduler.provider_concurrency.get(runtime.provider, 1) <= 0:
            reason = f"{runtime.provider} has a configured concurrency limit of zero"
        else:
            try:
                auth = provider.auth_status()
                if not auth.available or not auth.authenticated:
                    reason = f"{runtime.provider} authentication is unavailable: {redact(auth.detail, 2000)}"
            except Exception as exc:
                reason = f"{runtime.provider} authentication check failed: {redact(str(exc), 2000)}"
        if reason:
            run = self.store.transition_idea_run(
                run.id,
                IdeaRunState.FAILED,
                phase="provider-unavailable",
                question=reason,
            )
            self.reports.write(run)
        return run, True

    def observe_repository(self, repository: str) -> tuple[IdeaRun, bool]:
        return self.observe(
            self.github.get_snapshot(repository, self.config.ideas.spec_path, self.config.ideas.progress_path)
        )

    def reconcile(self) -> list[tuple[str, str]]:
        results = self.recover_expired_leases()
        self.preview_deployments.publish_allowlist(self.config.ideas.repositories)
        self.preview_deployments.sync_store(self.store, self.config.ideas.repositories)
        for repository in self.config.ideas.repositories:
            try:
                run, created = self.observe_repository(repository)
                results.append((repository, "created" if created else str(run.state)))
                published = self.store.latest_published_idea_run(repository)
                if published:
                    self._dispatch_preview(published)
            except Exception as exc:
                results.append((repository, f"idea-error: {redact(str(exc), 2000)}"))
        return results

    def _dispatch_preview(self, run: IdeaRun) -> None:
        try:
            queued = self.preview_deployments.enqueue(
                run,
                self.config.repository(run.repository).setup_script,
            )
            if queued:
                self.store.record_idea_event(
                    run.id,
                    "preview-queued",
                    {"commit": run.published_commit},
                )
        except Exception as exc:
            self.store.update_idea_preview(run.repository, "dispatch-failed")
            self.store.record_idea_event(
                run.id,
                "preview-dispatch-failed",
                {"error": redact(f"{type(exc).__name__}: {exc}", 4000)},
            )

    def recover_expired_leases(self) -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []
        for run in self.store.expired_idea_lease_runs():
            if run.state == IdeaRunState.SUPERSEDED:
                self.store.release_idea_lease(run.id)
                results.append((run.repository, "expired superseded lease released"))
                continue
            provider = self.providers.get(run.implementation_provider)
            try:
                if run.conversation_id:
                    if provider is None:
                        raise RuntimeError("provider is unavailable for expired-run cancellation")
                    provider.cancel(
                        ProviderRun(run.implementation_provider, run.conversation_id, run.session_id)
                    )
                recovered = self._retry_or_fail(run, "recovered-expired-lease", "expired idea run was canceled")
                results.append((run.repository, f"recovered: {recovered.state}"))
            except Exception as exc:
                failed = self.store.transition_idea_run(
                    run.id,
                    IdeaRunState.FAILED,
                    phase="recovery-cancel-failed",
                    question=redact(f"{type(exc).__name__}: {exc}", 4000),
                )
                self.reports.write(failed)
                results.append((run.repository, "expired-run cancel failed"))
        return results

    def _cancel_superseded(self, run: IdeaRun) -> None:
        with self._active_lock:
            selected = self._active_runs.get(run.id)
        if selected is None and run.conversation_id:
            provider = self.providers.get(run.implementation_provider)
            if provider:
                selected = (provider, ProviderRun(run.implementation_provider, run.conversation_id, run.session_id))
        try:
            if selected:
                selected[0].cancel(selected[1])
            self.store.release_idea_lease(run.id)
            self.store.record_idea_event(run.id, "superseded-canceled", {})
        except Exception as exc:
            self.store.record_idea_event(
                run.id,
                "superseded-cancel-failed",
                {"error": redact(f"{type(exc).__name__}: {exc}", 2000)},
            )

    def _current(self, run_id: str) -> IdeaRun:
        run = self.store.get_idea_run_by_id(run_id)
        if run is None:
            raise StoreError(f"unknown idea run: {run_id}")
        return run

    def _require_running(self, run: IdeaRun) -> IdeaRun:
        current = self._current(run.id)
        if current.state == IdeaRunState.SUPERSEDED:
            raise StaleIdeaError("a newer spec superseded this run")
        if current.state != IdeaRunState.RUNNING:
            raise StoreError(f"idea run is no longer running: {current.state}")
        if not self.store.renew_idea_lease(
            current.id, current.lease_owner or "", self.config.scheduler.lease_seconds
        ):
            raise StoreError("idea run lease expired")
        return self._current(run.id)

    def _start_heartbeat(self, run: IdeaRun) -> tuple[threading.Event, threading.Thread]:
        stop = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(self.config.scheduler.heartbeat_seconds):
                current = self._current(run.id)
                if current.state == IdeaRunState.SUPERSEDED:
                    self._cancel_superseded(current)
                    return
                if not self.store.renew_idea_lease(
                    run.id, run.lease_owner or "", self.config.scheduler.lease_seconds
                ):
                    return

        thread = threading.Thread(target=heartbeat, name=f"idea-heartbeat-{run.id[:8]}", daemon=True)
        thread.start()
        return stop, thread

    @contextmanager
    def _provider_slot(self, run: IdeaRun, provider: ProviderAdapter):
        def heartbeat() -> None:
            self._require_running(run)

        with self.provider_slots.acquire(provider.name, self.config.scheduler.heartbeat_seconds, heartbeat) as acquired:
            if not acquired:
                raise OpenHandsProviderError(f"{provider.name} has no available provider concurrency slot")
            yield

    def _wait(self, run: IdeaRun, provider: ProviderAdapter, provider_run: ProviderRun):
        with self._active_lock:
            self._active_runs[run.id] = (provider, provider_run)
        try:
            return provider.wait(provider_run, self.config.providers[provider.name].timeout_seconds)
        finally:
            with self._active_lock:
                self._active_runs.pop(run.id, None)

    def _advisory_validations(self, run: IdeaRun, worktree: Path) -> tuple[bool, str]:
        commands = self.config.repository(run.repository).validation_commands
        if not commands and (worktree / ".openhands" / "quality-gate.sh").is_file():
            commands = (("bash", ".openhands/quality-gate.sh"),)
        if not commands:
            return True, "no advisory checks configured"
        results = []
        for command in commands:
            self.workspaces.verify_run_integrity(run.id, run.repository, worktree)
            result = run_validation(
                command,
                worktree,
                self.config.scheduler.validation_timeout_seconds,
                run_as_user=self.config.service.validation_user,
            )
            self.store.record_idea_validation(run.id, run.attempt, result)
            results.append(result)
        passed = sum(result.ok for result in results)
        return passed == len(results), f"{passed}/{len(results)} advisory commands passed"

    def _progress_results(
        self,
        run: IdeaRun,
        affected,
        *,
        status: str,
        summary: str,
        screenshots: dict[str, str],
    ) -> dict[str, SectionResult]:
        prior = previous_results(run.previous_progress)
        affected_slugs = {section.slug for section in affected}
        values: dict[str, SectionResult] = {}
        progress_parent = Path(self.config.ideas.progress_path).parent
        for section in sections(run.spec_content):
            if section.slug not in affected_slugs:
                values[section.slug] = prior.get(
                    section.slug,
                    SectionResult("not started", "No result has been recorded for this wish yet."),
                )
                continue
            screenshot = screenshots.get(section.slug, "")
            if screenshot:
                screenshot = Path(os.path.relpath(screenshot, progress_parent)).as_posix()
            elif section.slug in prior:
                screenshot = prior[section.slug].screenshot
            values[section.slug] = SectionResult(status, summary, screenshot, section.title)
        return values

    def _write_progress(
        self,
        run: IdeaRun,
        worktree: Path,
        affected,
        *,
        status: str,
        summary: str,
        screenshots: dict[str, str],
    ) -> None:
        progress = render_progress(
            run.spec_content,
            self._progress_results(
                run,
                affected,
                status=status,
                summary=summary,
                screenshots=screenshots,
            ),
        )
        target = worktree / self.config.ideas.progress_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(progress)

    def _guard_publication(self, run: IdeaRun, worktree: Path) -> IdeaSnapshot:
        self._require_running(run)
        if self.vault is not None:
            from .vault import VaultError

            try:
                self.vault.guard_spec(run.repository, run.spec_content)
            except (VaultError, OSError) as exc:
                raise StaleIdeaError(str(exc)) from exc
        if run.repository not in self.config.ideas.repositories or run.repository in self.config.github.allowed_repositories:
            raise StaleIdeaError("repository is no longer exclusively ideas-allowlisted")
        live = self.github.get_snapshot(
            run.repository, self.config.ideas.spec_path, self.config.ideas.progress_path
        )
        if not live.private:
            raise StaleIdeaError("ideas repository is no longer private")
        if live.spec_hash != run.spec_hash:
            try:
                self.observe(live)
            except IdeasIntakeError:
                pass
            raise StaleIdeaError("remote idea spec changed before publication")
        spec_path = worktree / self.config.ideas.spec_path
        if not spec_path.is_file() or spec_path.read_bytes() != run.spec_content:
            raise WorkspaceError("the implementation modified the human-owned idea spec")
        return live

    def _publish(self, run: IdeaRun, worktree: Path, *, question_only: bool) -> str:
        live = self._guard_publication(run, worktree)
        all_paths = self.workspaces.changed_paths(worktree, run.default_branch)
        if self.config.ideas.spec_path in all_paths:
            raise WorkspaceError("the implementation modified the human-owned idea spec")
        if question_only:
            allowed_prefix = "idea/assets/"
            paths = tuple(
                path
                for path in all_paths
                if path == self.config.ideas.progress_path or path.startswith(allowed_prefix)
            )
        else:
            paths = tuple(path for path in all_paths if path != self.config.ideas.spec_path)
        if not paths:
            raise WorkspaceError("the idea run produced no committable progress")
        self.workspaces.commit_run(worktree, f"ideas: implement spec {run.spec_hash[:12]}", paths)
        rebased = False
        if live.base_commit != run.base_commit:
            self.workspaces.fetch(worktree, run.repository)
            self.workspaces.rebase_onto_origin(worktree, run.default_branch)
            rebased = True
            self._guard_publication(run, worktree)
        try:
            self.workspaces.push_default(worktree, run.repository, run.default_branch)
        except NonFastForwardError:
            if rebased:
                raise
            self._guard_publication(run, worktree)
            self.workspaces.fetch(worktree, run.repository)
            self.workspaces.rebase_onto_origin(worktree, run.default_branch)
            self._guard_publication(run, worktree)
            self.workspaces.push_default(worktree, run.repository, run.default_branch)
        return self.workspaces.head(worktree)

    def _retry_or_fail(self, run: IdeaRun, phase: str, reason: str) -> IdeaRun:
        # Pre-provider failures never increment attempt. Requeueing them would
        # otherwise retry forever (for example an unreadable setup script).
        if 0 < run.attempt < self.config.scheduler.max_attempts:
            return self.store.transition_idea_run(
                run.id,
                IdeaRunState.QUEUED,
                phase=phase,
                question=redact(reason, 4000),
            )
        return self.store.transition_idea_run(
            run.id,
            IdeaRunState.FAILED,
            phase=phase,
            question=redact(reason, 4000),
        )

    def run_claimed(self, run: IdeaRun) -> IdeaRun:
        heartbeat_stop, heartbeat_thread = self._start_heartbeat(run)
        try:
            run = self._require_running(run)
            runtime = parse_runtime(run.runtime_content)
            worktree = self.workspaces.checkout_run(
                run_id=run.id,
                repository=run.repository,
                branch=f"ideas/{run.id}",
                base_branch=run.default_branch,
                base_revision=run.base_commit,
            )
            run = self.store.update_idea_run(run.id, worktree=str(worktree), phase="checkout")
            spec_path = worktree / self.config.ideas.spec_path
            if not spec_path.is_file() or spec_path.read_bytes() != run.spec_content:
                raise WorkspaceError("accepted idea spec does not match the exact base commit")
            # Setup runs as the credential-free validator, which shares the
            # worker group. A fresh checkout inherits the private service mask.
            self.workspaces.prepare_for_agent(worktree)
            setup = self.workspaces.run_setup(
                worktree,
                self.config.repository(run.repository).setup_script,
                self.config.service.validation_user,
            )
            if setup:
                self.store.record_idea_validation(run.id, run.attempt, setup)
                if not setup.ok:
                    return self._retry_or_fail(run, "setup-failed", "repository setup failed")
            self.workspaces.prepare_for_agent(worktree)
            provider = self.providers.get(run.implementation_provider)
            if provider is None:
                return self._retry_or_fail(run, "provider-unavailable", "provider is not configured")
            previous = self.store.last_completed_idea_run(run.repository)
            previous_spec = previous.spec_content if previous and previous.id != run.id else None
            prompt = idea_implementation_prompt(
                run,
                previous_spec,
                self.config.service.global_agent_instruction,
                self.config.repository(run.repository).instruction,
            )
            if self.vault is not None:
                from .vault import VaultError

                try:
                    prompt += "\n\n" + self.vault.checklist_context(run.repository, run.spec_content)
                except (VaultError, OSError) as exc:
                    raise StaleIdeaError(str(exc)) from exc
            with self._provider_slot(run, provider):
                provider_run = provider.start(worktree, prompt, run.id)
                run = self.store.begin_idea_attempt(
                    run.id,
                    conversation_id=provider_run.conversation_id,
                    session_id=provider_run.session_id,
                )
                result = self._wait(run, provider, provider_run)
            self._require_running(run)
            if spec_path.read_bytes() != run.spec_content:
                return self.store.transition_idea_run(
                    run.id,
                    IdeaRunState.FAILED,
                    phase="spec-modified",
                    question="The implementation modified the human-owned idea spec; nothing was pushed.",
                )
            affected = affected_sections(previous_spec, run.spec_content)
            if result.outcome in {ProviderOutcome.NEEDS_GUIDANCE, ProviderOutcome.BLOCKED}:
                question = result.question_or_reason or result.summary
                self._write_progress(
                    run,
                    worktree,
                    affected,
                    status="question",
                    summary=question,
                    screenshots={},
                )
                commit = self._publish(run, worktree, question_only=True)
                return self.store.transition_idea_run(
                    run.id,
                    IdeaRunState.QUESTION,
                    phase="question",
                    question=question,
                    published_commit=commit,
                )
            if result.outcome != ProviderOutcome.COMPLETED:
                failure_kind = str(result.raw.get("failure_kind") or "")
                if failure_kind in {"quota", "provider-tool"} and run.attempt < self.config.scheduler.max_attempts:
                    seconds = min(
                        self.config.scheduler.provider_backoff_base_seconds * (2 ** max(0, run.attempt - 1)),
                        self.config.scheduler.provider_backoff_max_seconds,
                    )
                    self.store.set_provider_backoff(
                        run.implementation_provider,
                        result.question_or_reason or result.summary,
                        seconds,
                    )
                return self._retry_or_fail(
                    run,
                    "provider-failed",
                    result.question_or_reason or result.summary,
                )
            # The implementation may introduce a new toolchain/setup script.
            setup = self.workspaces.run_setup(
                worktree, self.config.repository(run.repository).setup_script,
                self.config.service.validation_user,
            )
            if setup:
                self.store.record_idea_validation(run.id, run.attempt, setup)
                if not setup.ok:
                    raise WorkspaceError(f"post-implementation setup failed: {redact(setup.output, 4000)}")
            advisory_ok, validation_summary = self._advisory_validations(run, worktree)
            run = self.store.update_idea_run(run.id, validation_summary=validation_summary, phase="preview")
            current_runtime = parse_runtime((worktree / ".symphony/idea.toml").read_bytes())
            if current_runtime.provider != runtime.provider:
                raise IdeaContractError("an idea run cannot change its provider while executing")
            evidence = self.preview.boot_and_capture(worktree, current_runtime, affected)
            status = "done" if advisory_ok else "partial"
            self._write_progress(
                run,
                worktree,
                affected,
                status=status,
                summary=result.summary,
                screenshots=evidence.screenshots,
            )
            commit = self._publish(run, worktree, question_only=False)
            published = self.store.transition_idea_run(
                run.id,
                IdeaRunState.PUBLISHED,
                phase="published",
                validation_summary=validation_summary,
                published_commit=commit,
                question="",
            )
            self._dispatch_preview(published)
            return published
        except StaleIdeaError as exc:
            current = self._current(run.id)
            if current.state == IdeaRunState.SUPERSEDED:
                self.store.release_idea_lease(run.id)
                return self._current(run.id)
            return self.store.transition_idea_run(
                run.id,
                IdeaRunState.SUPERSEDED,
                phase="superseded",
                question=str(exc),
            )
        except (IdeaContractError, PreviewError, WorkspaceError, StoreError) as exc:
            current = self._current(run.id)
            if current.state == IdeaRunState.SUPERSEDED:
                self.store.release_idea_lease(run.id)
                return self._current(run.id)
            return self._retry_or_fail(current, "idea-run-failed", f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            current = self._current(run.id)
            if current.state == IdeaRunState.SUPERSEDED:
                self.store.release_idea_lease(run.id)
                return self._current(run.id)
            return self._retry_or_fail(current, "orchestrator-failure", f"{type(exc).__name__}: {exc}")
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            current = self.store.get_idea_run_by_id(run.id)
            if current:
                self.reports.write(current)
