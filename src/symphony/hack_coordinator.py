"""Time-bounded lane execution and a single, gated campaign integrator.

Only the orchestrator commits and pushes. Workers have read-only Git metadata,
and their complete diff is checked against the frozen lane assignment.
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from .github import GhCLIBackend, GitHubError
from .hack_contract import parse_board, parse_lanes, validate_footprint
from .hack_store import HackStore
from .ideas_contract import parse_runtime
from .ideas_preview import IdeaPreview
from .ideas_progress import IdeaSection
from .intake import validate_repository_name
from .models import ProviderOutcome, ProviderRun
from .preview_queue import PreviewQueue, preview_allowlist
from .providers.openhands import RESULT_MARKER
from .validation import redact, run_validation, validation_environment
from .workspace import WorkspaceError, WorkspaceManager


class HackError(RuntimeError):
    pass


class GhHackBackend:
    """Read a board at one exact commit, and publish one final campaign PR."""

    def __init__(self, repositories: tuple[str, ...]):
        self.repositories = set(repositories)

    def get_snapshot(self, repository: str, ref: str | None = None) -> dict:
        validate_repository_name(repository)
        if repository not in self.repositories:
            raise GitHubError("repository is not hack-allowlisted")
        repo = GhCLIBackend._run(["api", f"repos/{repository}"], json_output=True)
        if not repo.get("private"):
            raise GitHubError("hack campaigns require a private repository")
        default_branch = repo.get("default_branch") or "main"
        commit = GhCLIBackend._run(
            ["api", f"repos/{repository}/commits/{ref or default_branch}"], json_output=True
        )["sha"]
        tree = GhCLIBackend._run(
            ["api", f"repos/{repository}/git/trees/{commit}?recursive=1"], json_output=True
        )
        if tree.get("truncated"):
            raise GitHubError("repository tree is too large to inspect safely")
        entries = {row["path"]: row["sha"] for row in tree.get("tree", []) if row.get("type") == "blob"}

        def read(path: str) -> bytes:
            if path not in entries:
                return b""
            blob = GhCLIBackend._run(["api", f"repos/{repository}/git/blobs/{entries[path]}"], json_output=True)
            return base64.b64decode(blob["content"].replace("\n", ""), validate=True)

        return {
            "repository": repository, "private": True, "default_branch": default_branch,
            "base_commit": commit, "board_content": read("hack/BOARD.md"),
            "lanes_content": read("hack/LANES.toml") or read(".symphony/hack.toml"),
        }

    def publish_pr(self, campaign: dict, body: str) -> str:
        self.get_snapshot(campaign["repository"])
        # Include closed/merged PRs: an interrupted response must never create a
        # second publication after a human has already handled the first one.
        existing = GhCLIBackend._run(
            ["pr", "list", "--repo", campaign["repository"], "--head", campaign["branch"],
             "--state", "all", "--json", "url", "--limit", "1"], json_output=True,
        )
        if existing:
            return existing[0]["url"]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md") as handle:
            handle.write(body)
            handle.flush()
            return GhCLIBackend._run(
                ["pr", "create", "--repo", campaign["repository"], "--base", campaign["default_branch"],
                 "--head", campaign["branch"], "--draft", "--title", f"Hack campaign {campaign['id'][:12]}",
                 "--body-file", handle.name]
            )


class HackCoordinator:
    def __init__(self, config, store, github, providers, provider_slots):
        self.config = config
        self.store = store if isinstance(store, HackStore) else HackStore(store)
        self.state = self.store
        self.github = github
        self.providers = providers
        self.provider_slots = provider_slots
        self.workspaces = WorkspaceManager(config.service.workspace_dir)
        self.preview = IdeaPreview(config.service.validation_user, config.service.state_dir)
        self.preview_deployments = PreviewQueue(config.service.preview_dir, config.service.workspace_dir)
        self._active: dict[str, tuple[object, ProviderRun, str, int]] = {}
        self._active_lock = threading.Lock()
        self._operation = threading.local()
        self.vault = None

    @staticmethod
    def _remaining(campaign: dict) -> float:
        return (datetime.fromisoformat(campaign["expires_at"]) - datetime.now(UTC)).total_seconds()

    def start(self, repository: str, hours: float = 24, **overrides) -> dict:
        if not self.config.hack.enabled or repository not in self.config.hack.repositories:
            raise HackError("hack mode must be enabled and the repository explicitly allowlisted")
        if not 0 < hours <= 168:
            raise HackError("campaign hours must be greater than zero and at most 168")
        existing = self.store.active_campaign(repository)
        if existing:
            return existing
        home = "github" if repository in self.config.github.allowed_repositories else "idea"
        if home == "idea" and repository not in self.config.ideas.repositories:
            raise HackError("a hack repository must have a configured GitHub or Ideas home tier")
        provider_name = overrides.get("provider", self.config.hack.provider)
        provider = self.providers.get(provider_name)
        if provider is None or not provider.capabilities.autonomous_available:
            raise HackError("the campaign provider is not available for autonomous execution")
        auth = provider.auth_status()
        if not auth.available or not auth.authenticated:
            raise HackError(f"campaign provider authentication is unavailable: {redact(auth.detail)}")
        owner = f"hack-start-{uuid.uuid4()}"
        vault_lock = self.config.vault.enabled
        if vault_lock and not self.store.store.acquire_operation_lock("vault-intake", owner, 900):
            raise HackError("vault synchronization is in progress; campaign cannot start until it finishes")
        try:
            snapshot = self.github.get_snapshot(repository)
            if not snapshot["private"]:
                raise HackError("hack campaigns require a private repository")
            return self.store.start_campaign(
                repository=repository, home_tier=home, default_branch=snapshot["default_branch"],
                base_commit=snapshot["base_commit"], provider=provider_name, hours=hours,
                max_parallel=self.config.hack.max_parallel, max_tasks=self.config.hack.max_tasks,
                publish_ideas=self.config.hack.publish_ideas,
            )
        finally:
            if vault_lock:
                self.store.store.release_operation_lock("vault-intake", owner)

    def stop(self, repository: str) -> dict | None:
        return self.store.request_stop(repository)

    @staticmethod
    def _git(worktree: Path, *args: str, check: bool = True, timeout: int = 120):
        process = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=OpenHands Symphony",
             "-c", "user.email=openhands-symphony@localhost", *args],
            cwd=worktree, env=validation_environment(), text=True, capture_output=True,
            timeout=timeout, check=False,
        )
        if check and process.returncode:
            raise WorkspaceError(redact(process.stderr or process.stdout, 4000))
        return process

    def _paths(self, worktree: Path, base: str) -> tuple[str, ...]:
        paths = set()
        for args in (("diff", "--name-only", "-z", base), ("ls-files", "--others", "--exclude-standard", "-z")):
            paths.update(path for path in self._git(worktree, *args).stdout.split("\0") if path)
        return tuple(sorted(paths))

    def _campaign(self, task: dict) -> dict:
        campaign = self.store.get_campaign(task["campaign_id"])
        if not campaign:
            raise HackError("campaign no longer exists")
        return campaign

    def _require_running(self, task: dict) -> dict:
        campaign = self._campaign(task)
        current = self.store.get_task(task["id"])
        if not current or current["state"] != "running" or current["attempt"] != task["attempt"] or current["lease_owner"] != task["lease_owner"]:
            raise HackError("hack task no longer belongs to this execution attempt")
        if self._remaining(campaign) <= 0 or campaign["state"] not in {"starting", "active", "draining", "polishing"}:
            raise HackError("campaign stopped or reached its hard deadline")
        if not self.store.renew_lease(task["id"], task.get("lease_owner") or "", self.config.scheduler.lease_seconds):
            raise HackError("hack task lease expired")
        return campaign

    def _cancel(self, task: dict) -> None:
        with self._active_lock:
            active = self._active.get(task["id"])
        if active and active[2:] == (task.get("lease_owner"), task["attempt"]):
            active[0].cancel(active[1])
        elif task.get("conversation_id"):
            provider = self.providers.get(task["provider"])
            if provider is None:
                raise HackError("cannot cancel expired task: provider is unavailable")
            provider.cancel(ProviderRun(task["provider"], task["conversation_id"], task.get("session_id")))

    def recover_expired_leases(self) -> list[tuple[str, str]]:
        results = []
        for task in self.store.expired_tasks():
            try:
                self._cancel(task)
                self.store.recover_expired_tasks([task["id"]])
                results.append((task["repository"], "expired hack task canceled and blocked"))
            except Exception as exc:
                self.store.update_task(task["id"], error=redact(str(exc), 4000))
                results.append((task["repository"], f"hack cancellation failed: {redact(str(exc), 2000)}"))
        return results

    def _prompt(self, task: dict, campaign: dict) -> str:
        kind = task["kind"]
        instructions = task["prompt"]
        if kind == "scaffold":
            instructions += (
                "\nCreate a bootable shared skeleton and explicit disjoint lane boundaries in hack/LANES.toml "
                "using [lanes.NAME] paths = ['directory/**']. Create shared interface stubs before fan-out. "
            "Provide .openhands/hack-gate.sh (fast build/smoke checks) and .symphony/idea.toml "
                "with provider and [preview] start argv, port, health_path, startup_timeout_seconds. "
                "The preview must bind 127.0.0.1 and respect PORT or a {port} argv placeholder."
            )
        elif kind == "polish":
            instructions += "\nMake one final solo pass for cross-feature bugs, visual consistency, dead code and demo flow."
        elif kind == "dispatcher":
            source = json.loads(task["prompt"])
            instructions = (
                "You are the board dispatcher. Do not implement features. Read the director's board below and "
                "write only hack/dispatch.json containing {\"board\": \"normalized Markdown board\"}. "
                "The normalized board must use ## configured-lane headers and ordered '- [ ] [stable-id] task' lines. "
                "Copy explicit task IDs unchanged. Preserve checked tasks and priorities. Split each cross-lane "
                "request into an interface/stub contract task in its owning lane, followed by implementation tasks "
                "in individual lanes with '<!-- depends: contract-id -->'. Use stable derived IDs for splits. "
                "Never broaden a lane footprint. A task may modify only its lane's files. Dependencies must be "
                "acyclic and name tasks present in the normalized board. Preserve existing task assignments by ID.\n"
                f"Configured lanes:\n{source['lanes']}\nDirector board:\n{source['board']}\n"
            )
        return (
            f"{self.config.service.global_agent_instruction}\n"
            f"{self.config.repository(campaign['repository']).instruction}\n"
            f"Hack campaign {campaign['id']}; deadline {campaign['expires_at']}. Task: {kind}.\n"
            "Edit files only. The orchestrator owns Git commits, pushes, the campaign branch and publication. "
            f"Never edit hack/BOARD.md, hack/STATUS.md or {self.config.ideas.spec_path}; those have other owners. "
            "Never change Git metadata or invoke git push. No per-task PRs, labels, comments or reviews.\n"
            f"Frozen lane: {task['lane']}; permitted paths: {', '.join(task['footprint'])}.\n"
            "Stay strictly inside that footprint. If another lane needs a change, report the contract or blocker.\n"
            f"{instructions}\n"
            "Scaffold and polish jobs maintain .openhands/setup.sh for noninteractive dependency setup; "
            "other tasks must never edit it unless their lane explicitly owns it. "
            "Follow repository AGENTS.md. Finish with exactly one structured line:\n"
            f'{RESULT_MARKER}{{"outcome":"completed|needs-guidance|blocked|failed",'
            '"summary":"concise result","question_or_reason":"one focused question or failure"}\n'
        )

    def run_claimed(self, task: dict) -> dict:
        stop = threading.Event()
        cancellation_failed = threading.Event()
        original = task.copy()
        provider_run = None
        provider_finished = False

        def update(**fields):
            return self.store.update_task(original["id"], expected_owner=original["lease_owner"],
                                          expected_attempt=original["attempt"], **fields)

        def finish(state, **fields):
            return self.store.finish_task(original["id"], state, expected_owner=original["lease_owner"],
                                          expected_attempt=original["attempt"], **fields)

        def heartbeat():
            while not stop.wait(min(self.config.scheduler.heartbeat_seconds, 1)):
                try:
                    self._require_running(task)
                except Exception:
                    try:
                        self._cancel(task)
                    except Exception:
                        cancellation_failed.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True, name=f"hack-{task['id'][:8]}")
        thread.start()
        try:
            campaign = self._require_running(task)
            base = task.get("base_commit") or campaign.get("result_commit") or campaign["base_commit"]
            worktree = self.workspaces.checkout_run(
                run_id=task["id"], repository=campaign["repository"], branch=task["branch"],
                base_branch=campaign["default_branch"], base_revision=base,
            )
            self._require_running(task)
            if task["attempt"] > 1:
                # A director-authorized retry starts from its new frozen base.
                # The previous attempt's isolated checkout is disposable; stale
                # files/status must not be misidentified as this lane's changes.
                self._git(worktree, "reset", "--hard", base)
                self._git(worktree, "clean", "-ffdx")
            task = update(worktree=str(worktree), base_commit=base)
            protected_paths = ["hack/BOARD.md", "hack/STATUS.md", self.config.ideas.spec_path]
            if task["kind"] in {"lane", "dispatcher"}:
                protected_paths += ["hack/LANES.toml", ".symphony/hack.toml", ".openhands/hack-gate.sh", ".symphony/idea.toml"]
            if task["kind"] == "lane":
                protected_paths += ["hack/dispatch.json"]
            protected = {path: (worktree / path).read_bytes() if (worktree / path).exists() else None
                         for path in protected_paths}
            self.workspaces.prepare_for_agent(worktree)
            setup = self._setup(campaign, worktree)
            if setup and not setup.ok:
                raise HackError(f"repository setup failed: {redact(setup.output, 4000)}")
            provider = self.providers[task["provider"]]
            with self.provider_slots.acquire(provider.name, 1, lambda: self._require_running(task)) as acquired:
                if not acquired:
                    raise HackError("provider concurrency is disabled")
                campaign = self._require_running(task)
                provider_run = provider.start(worktree, self._prompt(task, campaign), task["id"])
                with self._active_lock:
                    self._active[task["id"]] = (provider, provider_run, original["lease_owner"], original["attempt"])
                task = update(conversation_id=provider_run.conversation_id, session_id=provider_run.session_id)
                try:
                    timeout = max(1, min(self.config.hack.task_timeout_seconds, int(self._remaining(campaign))))
                    result = provider.wait(provider_run, timeout)
                    provider_finished = True
                except Exception:
                    try:
                        provider.cancel(provider_run)
                        provider_finished = True
                    except Exception:
                        cancellation_failed.set()
                    raise
            self._require_running(task)
            self.workspaces.verify_run_integrity(task["id"], campaign["repository"], worktree)
            for path, content in protected.items():
                target = worktree / path
                actual = target.read_bytes() if target.exists() else None
                if actual != content:
                    raise HackError(f"worker modified protected file: {path}")
            if result.outcome != ProviderOutcome.COMPLETED:
                if result.raw.get("failure_kind") in {"quota", "provider-tool"}:
                    self.store.store.set_provider_backoff(
                        task["provider"], redact(result.question_or_reason or result.summary, 2000),
                        min(self.config.scheduler.provider_backoff_base_seconds,
                            self.config.scheduler.provider_backoff_max_seconds),
                    )
                state = "question" if result.outcome == ProviderOutcome.NEEDS_GUIDANCE else "blocked"
                return finish(state, note=redact(result.question_or_reason or result.summary))
            paths = self._paths(worktree, base)
            if task["kind"] in {"lane", "dispatcher"}:
                validate_footprint(paths, task["footprint"])
            if task["kind"] == "dispatcher":
                source = json.loads(task["prompt"])
                target = worktree / "hack/dispatch.json"
                if target.is_symlink() or not target.resolve().is_relative_to(worktree.resolve()):
                    raise HackError("dispatcher output escapes its worktree")
                plan = json.loads(target.read_text())
                if not isinstance(plan, dict) or not isinstance(plan.get("board"), str):
                    raise HackError("dispatcher must return a JSON object with a Markdown board string")
                parse_board(plan["board"], parse_lanes(source["lanes"]))
                self._require_running(task)
                live_source = self._board_source(self._campaign(task))
                if live_source["hash"] != source["hash"]:
                    return finish("canceled", note="Director edited the board while it was being dispatched.")
                self.store.sync_board(
                    campaign["id"], plan["board"].encode(), source["lanes"].encode(),
                    expected_task_id=original["id"], expected_owner=original["lease_owner"],
                    expected_attempt=original["attempt"], source_hash=source["hash"],
                )
                return finish("merged", note="Board dispatched; cross-lane contracts precede implementation.")
            self._require_running(task)
            if self.workspaces.has_changes(worktree):
                self.workspaces.commit_run(worktree, f"hack: {task['lane']} {task['id'][:12]}")
            elif task["kind"] != "polish":
                raise HackError("task produced no changes")
            return finish("ready", result_commit=self.workspaces.head(worktree), note=redact(result.summary, 4000))
        except Exception as exc:
            if provider_run is not None and not provider_finished:
                try:
                    provider.cancel(provider_run)
                except Exception:
                    cancellation_failed.set()
            try:
                if cancellation_failed.is_set():
                    # Keep the lease/active campaign fenced until recovery can
                    # positively cancel the original provider conversation.
                    return update(error=f"cancellation unconfirmed: {redact(str(exc), 4000)}")
                return finish("blocked", error=redact(str(exc), 4000), note=redact(str(exc), 4000))
            except Exception:
                return self.store.get_task(original["id"])
        finally:
            stop.set()
            thread.join(timeout=2)
            with self._active_lock:
                selected = self._active.get(original["id"])
                if selected and selected[1] == provider_run:
                    self._active.pop(original["id"], None)

    def _fast_gate(self, campaign: dict, worktree: Path, *, milestone: bool = False) -> None:
        remaining = self._remaining(campaign)
        if remaining <= 0:
            raise HackError("campaign reached its hard deadline before integration")
        gate = self.workspaces._inside_script(worktree, ".openhands/hack-gate.sh")
        if not gate.is_file():
            raise HackError("mandatory fast gate .openhands/hack-gate.sh is missing")
        self.workspaces.prepare_for_agent(worktree)
        setup = self._setup(campaign, worktree)
        if setup and not setup.ok:
            raise HackError(f"post-implementation setup failed: {redact(setup.output, 4000)}")
        remaining = self._remaining(campaign)
        if remaining <= 0:
            raise HackError("campaign reached its hard deadline during dependency setup")
        result = run_validation(("bash", str(gate)), worktree,
                                max(1, min(int(remaining), self.config.hack.fast_gate_timeout_seconds)),
                                run_as_user=self.config.service.validation_user)
        if not result.ok:
            raise HackError(f"fast build gate failed: {redact(result.output, 4000)}")
        runtime = parse_runtime((worktree / ".symphony/idea.toml").read_bytes())
        remaining = self._remaining(campaign)
        if remaining <= 0:
            raise HackError("campaign reached its hard deadline during the fast gate")
        runtime = replace(runtime, startup_timeout_seconds=max(1, min(runtime.startup_timeout_seconds, int(remaining))))
        # No per-task screenshots. A milestone capture is moved to agent-owned
        # hack/assets so it cannot contaminate the Ideas progress contract.
        section = IdeaSection(0, "Campaign milestone", f"hack-{campaign['id'][:8]}", 0, b"")
        evidence = self.preview.boot_and_capture(
            worktree, runtime, (section,) if milestone else (), deadline_seconds=max(0.01, self._remaining(campaign)),
        )
        for relative in evidence.screenshots.values():
            source = worktree / relative
            target = worktree / "hack/assets" / f"milestone-{campaign.get('merged_count', 0)}.png"
            if not source.resolve().is_relative_to(worktree.resolve()) or not target.resolve().is_relative_to(worktree.resolve()):
                raise HackError("milestone screenshot path escapes the worktree")
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)

    def _setup(self, campaign: dict, worktree: Path):
        script = self.config.repository(campaign["repository"]).setup_script or ".openhands/setup.sh"
        target = self.workspaces._inside_script(worktree, script)
        if not target.exists():
            return None
        if not target.is_file():
            raise HackError("repository setup script is not a regular file")
        remaining = self._remaining(campaign)
        if remaining <= 0:
            raise HackError("campaign deadline reached before setup")
        return run_validation(("bash", str(target)), worktree,
                              max(1, min(int(remaining), self.config.hack.fast_gate_timeout_seconds)),
                              run_as_user=self.config.service.validation_user)

    def _assert_operation(self, campaign: dict) -> None:
        owner = getattr(self._operation, "owner", None)
        if owner and not self.store.renew_operation(campaign["id"], owner, 900):
            raise HackError("integrator operation lease expired")

    def _status(self, campaign: dict, *, merged: str | None = None, closing: bool = False) -> str:
        lines = ["# Hack campaign status", "", f"Campaign: `{campaign['id']}`", "",
                 f"Deadline: {campaign['expires_at']}",
                 f"State: {'completed' if closing and campaign['state'] == 'publishing' else campaign['state']}", "",
                 "| Task | Lane | State | Notes |", "| --- | --- | --- | --- |"]
        for task in self.store.list_tasks(campaign["id"]):
            state = "merged" if task["id"] == merged else task["state"]
            title = task.get("title") or task.get("prompt", "")
            def clean(value):
                return str(value).replace("|", "\\|").replace("\n", " ")[:1000]
            lines.append(f"| {clean(title)} | {clean(task['lane'])} | {state} | {clean(task.get('note') or task.get('error') or '')} |")
        if closing:
            lines += ["", "The campaign ended. Merged work is on the campaign branch; unfinished tasks remain recorded above."]
        return "\n".join(lines) + "\n"

    def _write_status(self, campaign: dict, worktree: Path, **kwargs) -> None:
        target = worktree / "hack/STATUS.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or not target.resolve().is_relative_to(worktree.resolve()):
            raise HackError("hack status path escapes the worktree")
        target.write_text(self._status(campaign, **kwargs))

    def _dispatch_preview(self, campaign: dict, worktree: Path) -> None:
        run = SimpleNamespace(repository=campaign["repository"], published_commit=campaign["result_commit"],
                              worktree=str(worktree))
        self.preview_deployments.publish_allowlist(preview_allowlist(self.config, self.store.store))
        self.preview_deployments.enqueue(run, self.config.repository(campaign["repository"]).setup_script or ".openhands/setup.sh")

    def _report_status(self, campaign: dict) -> None:
        content = self._status(campaign, closing=campaign["state"] in {"completed", "failed", "expired"}).encode()
        directory = self.config.service.report_dir / "hack" / campaign["id"]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "STATUS.md").write_bytes(content)
        if self.vault is not None:
            self.vault._safe_output(Path(campaign["repository"].replace("/", "--")) / "hack/STATUS.md", content)

    def _sync_status(self, campaign: dict) -> None:
        self._report_status(campaign)
        if not campaign.get("result_commit") or self._remaining(campaign) <= 0:
            return
        self._assert_operation(campaign)
        worktree = self.workspaces.checkout_run(
            run_id=f"{campaign['id']}-campaign", repository=campaign["repository"], branch=campaign["branch"],
            base_branch=campaign["default_branch"], base_revision=campaign["result_commit"],
        )
        self._git(worktree, "reset", "--hard", campaign["result_commit"])
        self._write_status(campaign, worktree)
        if self.workspaces.has_changes(worktree):
            self.workspaces.commit_run(worktree, "hack: update board execution status", ("hack/STATUS.md",))
        commit = self.workspaces.head(worktree)
        # Recording an orchestrator-only status commit before its push makes a
        # restart retry the same immutable commit, even after a lost response.
        self.store.update_campaign(campaign["id"], result_commit=commit, campaign_commit=commit, worktree=str(worktree))
        if self.workspaces.remote_matches(worktree, campaign["repository"], campaign["branch"]):
            return
        live = self.github.get_snapshot(campaign["repository"])
        if not live["private"] or campaign["repository"] not in self.config.hack.repositories:
            raise HackError("status publication requires a private, allowlisted repository")
        self._assert_operation(campaign)
        self.workspaces.push(worktree, campaign["repository"], campaign["branch"])

    def _integrate(self, campaign: dict, task: dict) -> None:
        self._assert_operation(campaign)
        worktree = Path(task["worktree"])
        self.workspaces.verify_run_integrity(task["id"], campaign["repository"], worktree)
        base = campaign.get("result_commit") or campaign["base_commit"]
        try:
            remote = self.github.get_snapshot(campaign["repository"], campaign["branch"])
        except GitHubError:
            remote = None
        if remote and task.get("prepared_commit") and remote["base_commit"] == task["prepared_commit"]:
            self._accept_candidate(campaign, task, worktree, task["result_commit"])
            return
        if task.get("prepared_commit"):
            if self.workspaces.head(worktree) != task["prepared_commit"]:
                raise HackError("prepared integration worktree changed after verification")
            if task.get("prepared_base_commit") == base:
                self._push_candidate(campaign, worktree)
                self._accept_candidate(campaign, task, worktree, task["prepared_commit"])
                return
            # Only the orchestrator's status/evidence commit is discarded. The
            # frozen worker output is rebased and fully verified against the new
            # campaign tip before publication.
            self._git(worktree, "reset", "--hard", task["implementation_commit"])
            task = self.store.update_task(task["id"], prepared_commit=None, prepared_base_commit=None,
                                          result_commit=task["implementation_commit"])
        # A pushed candidate survives crashes between the external write and
        # the SQLite update. Inspect exact ancestry before replaying a rebase.
        if task["base_commit"] != base:
            process = self._git(worktree, "rebase", "--onto", base, task["base_commit"], check=False)
            if process.returncode:
                self._git(worktree, "rebase", "--abort", check=False)
                raise HackError(f"integration conflict; edit the board to requeue: {redact(process.stderr, 3000)}")
            task = self.store.update_task(task["id"], base_commit=base, result_commit=self.workspaces.head(worktree))
        if task["kind"] == "lane":
            validate_footprint(self._paths(worktree, base), task["footprint"])
        if task["kind"] == "scaffold":
            lanes_path = worktree / "hack/LANES.toml"
            if not lanes_path.is_file():
                lanes_path = worktree / ".symphony/hack.toml"
            parse_lanes(lanes_path.read_bytes())
        task = self.store.update_task(task["id"], implementation_commit=self.workspaces.head(worktree))
        merged_count = sum(item["state"] == "merged" and item["kind"] != "dispatcher"
                           for item in self.store.list_tasks(campaign["id"]))
        campaign["merged_count"] = merged_count + 1
        milestone = task["kind"] == "polish" or (merged_count + 1) % self.config.hack.milestone_every == 0
        self._fast_gate(campaign, worktree, milestone=milestone)
        self.workspaces.verify_run_integrity(task["id"], campaign["repository"], worktree)
        generated_paths = self._paths(worktree, self.workspaces.head(worktree))
        if any(not path.startswith("hack/assets/") for path in generated_paths):
            raise HackError("fast gate modified source files; candidate was not integrated")
        self._write_status(campaign, worktree, merged=task["id"])
        if self.workspaces.has_changes(worktree):
            self.workspaces.commit_run(worktree, "hack: record gated integration")
        commit = self.workspaces.head(worktree)
        task = self.store.update_task(task["id"], result_commit=commit, prepared_commit=commit, prepared_base_commit=base)
        self._push_candidate(campaign, worktree)
        self._accept_candidate(campaign, task, worktree, commit)

    def _push_candidate(self, campaign: dict, worktree: Path) -> None:
        if self._remaining(campaign) <= 0:
            raise HackError("campaign reached its deadline before the campaign push")
        live = self.github.get_snapshot(campaign["repository"])
        if not live["private"] or campaign["repository"] not in self.config.hack.repositories:
            raise HackError("campaign no longer satisfies private repository allowlisting")
        self._assert_operation(campaign)
        self.workspaces.push(worktree, campaign["repository"], campaign["branch"])

    def _accept_candidate(self, campaign: dict, task: dict, worktree: Path, commit: str) -> None:
        # No worker checks out the campaign branch. This ref is solely the
        # integrator's and is needed for offline continuation and final checkout.
        self._git(worktree, "update-ref", f"refs/heads/{campaign['branch']}", commit)
        campaign = self.store.update_campaign(campaign["id"], result_commit=commit, campaign_commit=commit,
                                              worktree=str(worktree), note="fast build and boot gate passed")
        self.store.finish_task(task["id"], "merged", result_commit=commit)
        if task["kind"] == "scaffold" and campaign["state"] == "starting":
            campaign = self.store.update_campaign(campaign["id"], state="active")
        elif task["kind"] == "polish":
            campaign = self.store.update_campaign(campaign["id"], state="publishing")
        try:
            self._dispatch_preview(campaign, worktree)
        except Exception as exc:
            self.store.update_campaign(campaign["id"], note=f"preview queue error: {redact(str(exc), 2000)}")
        # Keep the immutable commits and reports, but retire the merged lane
        # branch/worktree. Campaign and other active lane branches are untouched.
        cache = self.workspaces.root / "repositories" / self.workspaces._repo_key(campaign["repository"])
        if task["branch"].startswith(f"hack/{campaign['id'][:8]}/task-") and task["branch"] != campaign["branch"]:
            self.store.update_campaign(campaign["id"], worktree=str(cache))
            removed = self._git(cache, "worktree", "remove", "--force", str(worktree), check=False)
            if removed.returncode == 0:
                self._git(cache, "branch", "-D", task["branch"], check=False)

    def _board_source(self, campaign: dict) -> dict:
        snapshot = self.github.get_snapshot(campaign["repository"])
        lanes = b""
        if campaign.get("worktree"):
            worktree = Path(campaign["worktree"])
            for path in ("hack/LANES.toml", ".symphony/hack.toml"):
                result = self._git(worktree, "show", f"{campaign['result_commit']}:{path}", check=False)
                if result.returncode == 0:
                    lanes = result.stdout.encode()
                    break
        lanes = lanes or snapshot["lanes_content"]
        parse_lanes(lanes)
        fingerprint = hashlib.sha256(snapshot["board_content"] + b"\0" + lanes).hexdigest()
        return {"hash": fingerprint, "board": snapshot["board_content"].decode(), "lanes": lanes.decode()}

    def _dispatch_board(self, campaign: dict) -> None:
        source = self._board_source(campaign)
        fingerprint = source["hash"]
        if campaign.get("observed_board_hash") != fingerprint:
            campaign = self.store.update_campaign(campaign["id"], observed_board_hash=fingerprint,
                                                  board_revision=campaign.get("board_revision", 0) + 1)
        key = f"dispatcher-{campaign['board_revision']}-{fingerprint[:20]}"
        for task in self.store.list_tasks(campaign["id"]):
            if task["kind"] == "dispatcher" and task["state"] == "queued" and task["key"] != key:
                self.store.finish_task(task["id"], "canceled", note="Superseded by a newer director board.")
        if campaign.get("board_hash") == fingerprint:
            return
        if not source["board"].strip() or not any("[ ]" in line or "[x]" in line.lower() for line in source["board"].splitlines()):
            self.store.sync_board(campaign["id"], b"", source["lanes"].encode())
            self.store.update_campaign(campaign["id"], board_hash=fingerprint)
            return
        self.store.enqueue_task(campaign["id"], key, "dispatcher", json.dumps(source),
                                ["hack/dispatch.json"], kind="dispatcher")

    def _publish(self, campaign: dict) -> None:
        self._assert_operation(campaign)
        if not campaign.get("result_commit"):
            self.store.update_campaign(campaign["id"], state="failed", note="No task passed the fast gate.")
            return
        snapshot = self.github.get_snapshot(campaign["repository"])
        if not snapshot["private"] or campaign["repository"] not in self.config.hack.repositories:
            raise HackError("campaign publication requires a private, allowlisted repository")
        worktree = self.workspaces.checkout_run(
            run_id=f"{campaign['id']}-campaign", repository=campaign["repository"], branch=campaign["branch"],
            base_branch=campaign["default_branch"], base_revision=campaign["result_commit"],
        )
        self._git(worktree, "reset", "--hard", campaign["result_commit"])
        self._write_status(campaign, worktree, closing=True)
        if self.workspaces.has_changes(worktree):
            self.workspaces.commit_run(worktree, "hack: close campaign and record unfinished tasks")
        final_commit = self.workspaces.head(worktree)
        campaign = self.store.update_campaign(campaign["id"], result_commit=final_commit, campaign_commit=final_commit,
                                              worktree=str(worktree), state="publishing")
        self._assert_operation(campaign)
        self.workspaces.push(worktree, campaign["repository"], campaign["branch"])
        snapshot = self.github.get_snapshot(campaign["repository"])
        if not snapshot["private"] or campaign["repository"] not in self.config.hack.repositories:
            raise HackError("campaign publication requires a private, allowlisted repository")
        direct = (campaign["home_tier"] in {"idea", "ideas"} and campaign["publish_ideas"]
                  and self.config.hack.publish_ideas and campaign["repository"] in self.config.ideas.repositories
                  and campaign["repository"] not in self.config.github.allowed_repositories)
        if direct and snapshot["base_commit"] in {campaign["base_commit"], final_commit}:
            if snapshot["base_commit"] != final_commit:
                self._assert_operation(campaign)
                self.workspaces.push_default(worktree, campaign["repository"], campaign["default_branch"])
            url = ""
        else:
            # A concurrently edited home branch is never rewritten. A single
            # final PR preserves those edits and makes conflicts reviewable.
            url = self.github.publish_pr(campaign, self._status(campaign, closing=True))
        self.store.update_campaign(campaign["id"], state="completed", pr_url=url, publication_url=url,
                                   note="Campaign published; normal home-tier intake resumed.")
        try:
            self._dispatch_preview(self.store.get_campaign(campaign["id"]), worktree)
        except Exception as exc:
            self.store.update_campaign(campaign["id"], note=f"Published; preview queue error: {redact(str(exc), 2000)}")

    def _revoke(self, campaign: dict, reason: str) -> None:
        for task in self.store.list_tasks(campaign["id"]):
            if task["state"] == "running":
                self._cancel(task)
            if task["state"] in {"queued", "running", "ready"}:
                self.store.finish_task(task["id"], "canceled", note=reason)
        campaign = self.store.update_campaign(campaign["id"], state="failed", note=reason, error=reason)
        self._report_status(campaign)

    def reconcile(self) -> list[tuple[str, str]]:
        results = self.recover_expired_leases()
        for campaign in self.store.list_campaigns(active_only=True):
            owner = f"hack-integrator-{uuid.uuid4()}"
            if not self.store.acquire_operation(campaign["id"], owner, max(900, self.config.hack.fast_gate_timeout_seconds + 300)):
                continue
            self._operation.owner = owner
            operation_stop = threading.Event()

            def keep_operation(campaign_id=campaign["id"], operation_owner=owner, stop_event=operation_stop):
                while not stop_event.wait(30):
                    if not self.store.renew_operation(campaign_id, operation_owner, 900):
                        return

            operation_thread = threading.Thread(target=keep_operation, daemon=True)
            operation_thread.start()
            try:
                campaign = self.store.get_campaign(campaign["id"])
                if not self.config.hack.enabled or campaign["repository"] not in self.config.hack.repositories:
                    self._revoke(campaign, "Hack opt-in was revoked; campaign stopped without further Git mutations.")
                    results.append((campaign["repository"], "revoked"))
                    continue
                try:
                    private = self.github.get_snapshot(campaign["repository"])["private"]
                except GitHubError as exc:
                    if "private repository" not in str(exc):
                        raise
                    private = False
                if not private:
                    self._revoke(campaign, "Repository is no longer private; campaign stopped without further Git mutations.")
                    results.append((campaign["repository"], "repository no longer private"))
                    continue
                tasks = self.store.list_tasks(campaign["id"])
                remaining = self._remaining(campaign)
                used = sum(task.get("attempt", 0) for task in tasks if task["kind"] in {"lane", "dispatcher"})
                if (remaining <= self.config.hack.polish_seconds or campaign["stop_requested"]
                        or used >= campaign["max_tasks"]):
                    if campaign["state"] in {"starting", "active"}:
                        campaign = self.store.update_campaign(campaign["id"], state="draining")
                    for task in tasks:
                        if task["state"] == "queued" and task["kind"] != "polish":
                            self.store.finish_task(task["id"], "canceled", note="Campaign fan-out stopped.")
                        elif remaining <= 0 and task["state"] == "running":
                            self._cancel(task)
                            self.store.finish_task(task["id"], "canceled", note="Hard campaign deadline reached.")
                for task in sorted(self.store.list_tasks(campaign["id"]), key=lambda task: task.get("finished_at") or task["created_at"]):
                    if task["state"] != "ready":
                        continue
                    if self._remaining(campaign) <= 0:
                        if task.get("prepared_commit"):
                            # Recover a push that happened before the deadline
                            # but whose acknowledgement was lost. This adopts
                            # only the exact previously gated, already-published
                            # commit and never starts a new integration push.
                            try:
                                remote = self.github.get_snapshot(campaign["repository"], campaign["branch"])
                            except GitHubError:
                                remote = None
                            if remote and remote["base_commit"] == task["prepared_commit"]:
                                self._integrate(campaign, task)
                                campaign = self.store.get_campaign(campaign["id"])
                                continue
                        self.store.finish_task(task["id"], "canceled", note="Deadline reached before integration.")
                        continue
                    try:
                        self._integrate(campaign, task)
                    except Exception as exc:
                        current = self.store.get_task(task["id"])
                        if current.get("prepared_commit"):
                            self.store.update_task(task["id"], error=redact(str(exc), 4000), note="Verified candidate awaits campaign publication.")
                        else:
                            self.store.finish_task(task["id"], "blocked", error=redact(str(exc), 4000), note=redact(str(exc), 4000))
                    campaign = self.store.get_campaign(campaign["id"])
                tasks = self.store.list_tasks(campaign["id"])
                if campaign["state"] == "starting" and any(task["kind"] == "scaffold" and task["state"] == "merged" for task in tasks):
                    campaign = self.store.update_campaign(campaign["id"], state="active")
                running = any(task["state"] in {"running", "ready"} for task in tasks)
                if campaign["state"] == "starting" and any(
                    task["kind"] == "scaffold" and task["state"] in {"blocked", "question", "canceled"}
                    for task in tasks
                ):
                    self.store.update_campaign(
                        campaign["id"], state="failed",
                        note="Mandatory scaffold did not complete. Resolve its reported blocker, then start a new campaign.",
                    )
                elif campaign["state"] == "active":
                    self._dispatch_board(campaign)
                elif campaign["state"] == "draining" and not running:
                    if self._remaining(campaign) > 0 and campaign.get("result_commit"):
                        self.store.enqueue_task(campaign["id"], "__polish__", "__solo__",
                                                "Polish the complete demo and fix cross-lane integration issues.",
                                                ["**"], kind="polish")
                        campaign = self.store.update_campaign(campaign["id"], state="polishing")
                    else:
                        campaign = self.store.update_campaign(campaign["id"], state="publishing")
                if campaign["state"] == "polishing" and not running:
                    polish = [task for task in self.store.list_tasks(campaign["id"]) if task["kind"] == "polish"]
                    if self._remaining(campaign) <= 0 or (polish and all(task["state"] in {"blocked", "question", "canceled", "merged"} for task in polish)):
                        campaign = self.store.update_campaign(campaign["id"], state="publishing")
                if campaign["state"] == "publishing" and not running:
                    self._publish(campaign)
                latest = self.store.get_campaign(campaign["id"])
                if latest["state"] in {"completed", "failed", "expired"}:
                    self._report_status(latest)
                elif any(task["state"] == "ready" and task.get("prepared_commit") for task in self.store.list_tasks(campaign["id"])):
                    self._report_status(latest)
                else:
                    self._sync_status(latest)
                results.append((campaign["repository"], self.store.get_campaign(campaign["id"])["state"]))
            except Exception as exc:
                self.store.update_campaign(campaign["id"], error=redact(str(exc), 4000), note=redact(str(exc), 4000))
                results.append((campaign["repository"], f"hack-error: {redact(str(exc), 2000)}"))
            finally:
                operation_stop.set()
                operation_thread.join(timeout=2)
                self._operation.owner = None
                self.store.release_operation(campaign["id"], owner)
        return results
