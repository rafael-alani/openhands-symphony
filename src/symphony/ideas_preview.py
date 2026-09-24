from __future__ import annotations

import math
import os
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from .ideas_contract import IdeaRuntime, preview_argv
from .ideas_progress import IdeaSection, screenshot_path
from .validation import redact, validation_argv, validation_environment


class PreviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreviewEvidence:
    health_url: str
    screenshots: dict[str, str]
    log: str


class IdeaPreview:
    def __init__(self, validation_user: str, state_dir: Path):
        self.validation_user = validation_user
        self.harness_home = state_dir / "browser-harness"

    @staticmethod
    def _available_loopback_port(declared_port: int) -> int:
        for _attempt in range(10):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                port = int(probe.getsockname()[1])
            if port != declared_port:
                return port
        raise PreviewError("unable to allocate a temporary preview port")

    def boot_and_capture(
        self,
        worktree: Path,
        runtime: IdeaRuntime,
        affected: tuple[IdeaSection, ...],
        *,
        deadline_seconds: float | None = None,
    ) -> PreviewEvidence:
        started = time.monotonic()
        if deadline_seconds is not None and (not math.isfinite(deadline_seconds) or deadline_seconds <= 0):
            raise PreviewError("preview total deadline must be a finite positive duration")
        total_deadline = started + deadline_seconds if deadline_seconds is not None else None

        def bounded_timeout(limit: float) -> float:
            if limit <= 0:
                raise PreviewError("preview deadline exhausted")
            if total_deadline is None:
                return limit
            remaining = total_deadline - time.monotonic()
            if remaining <= 0:
                raise PreviewError("preview total deadline exhausted")
            return min(limit, remaining)

        # A persistent last-good release may already own runtime.port. Every
        # pre-publication boot therefore uses a separate effective port so the
        # mandatory health check and screenshots can only target this candidate.
        effective_runtime = replace(runtime, port=self._available_loopback_port(runtime.port))
        preview_variables = {
            "HOST": "127.0.0.1",
            "PORT": str(effective_runtime.port),
        }
        command = validation_argv(preview_argv(effective_runtime), self.validation_user, preview_variables)
        preview_environment = validation_environment()
        preview_environment.update(preview_variables)
        bounded_timeout(1)
        # Startup/build output can exceed a pipe buffer before the server
        # listens. Spool it instead of allowing an unread PIPE to stall boot.
        log_handle = tempfile.TemporaryFile()
        try:
            process = subprocess.Popen(
                command,
                cwd=worktree,
                env=preview_environment,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except BaseException:
            log_handle.close()
            raise

        def read_log() -> str:
            size = log_handle.seek(0, os.SEEK_END)
            log_handle.seek(max(0, size - 16_000))
            return redact(log_handle.read().decode("utf-8", errors="replace"), 4000)
        health_url = f"http://127.0.0.1:{effective_runtime.port}{effective_runtime.health_path}"
        deadline = time.monotonic() + runtime.startup_timeout_seconds
        if total_deadline is not None:
            deadline = min(deadline, total_deadline)
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise PreviewError(f"preview exited before health succeeded: {read_log()}")
                try:
                    response = httpx.get(health_url, timeout=bounded_timeout(min(2, deadline - time.monotonic())))
                    if 200 <= response.status_code < 400:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(max(0, min(0.25, deadline - time.monotonic())))
            else:
                bounded_timeout(1)
                raise PreviewError(f"preview health check timed out: {health_url}; {read_log()}")

            bounded_timeout(1)
            screenshots: dict[str, str] = {}
            self.harness_home.mkdir(parents=True, exist_ok=True)
            harness_environment = validation_environment()
            harness_environment.update(
                {
                    "BROWSER_HARNESS_HOME": str(self.harness_home),
                    "BH_AGENT_WORKSPACE": str(self.harness_home / "agent-workspace"),
                    "BU_CDP_URL": "http://127.0.0.1:9222",
                    "BH_TELEMETRY": "0",
                    "BROWSER_USE_CLOUD_SYNC": "false",
                }
            )
            for section in affected:
                relative = screenshot_path(section)
                target = worktree / relative
                if target.is_symlink() or not target.resolve().is_relative_to(worktree.resolve()):
                    raise PreviewError("screenshot target escapes the worktree or is a symlink")
                target.parent.mkdir(parents=True, exist_ok=True)
                script = (
                    f'new_tab("http://127.0.0.1:{effective_runtime.port}/")\n'
                    "wait_for_load()\n"
                    f'capture_screenshot(r"{target}", max_dim=1280)\n'
                )
                try:
                    capture = subprocess.run(
                        ["/opt/browser-use/bin/browser-harness"],
                        input=script,
                        cwd=worktree,
                        env=harness_environment,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        timeout=bounded_timeout(120),
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise PreviewError(f"browser-harness timed out for {section.title}") from exc
                bounded_timeout(1)
                if capture.returncode != 0 or not target.is_file():
                    raise PreviewError(
                        f"browser-harness failed for {section.title}: {redact(capture.stdout, 4000)}"
                    )
                screenshots[section.slug] = relative
            return PreviewEvidence(health_url, screenshots, read_log())
        finally:
            try:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    grace = 10 if total_deadline is None else max(0, min(10, total_deadline - time.monotonic()))
                    process.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    pass
                # A wrapper (npm, shell, or a dev server) can exit before its
                # children. Reap the entire candidate group even in that case.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
            finally:
                log_handle.close()
