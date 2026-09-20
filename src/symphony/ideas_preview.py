from __future__ import annotations

import socket
import subprocess
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
    ) -> PreviewEvidence:
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
        process = subprocess.Popen(
            command,
            cwd=worktree,
            env=preview_environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        health_url = f"http://127.0.0.1:{effective_runtime.port}{effective_runtime.health_path}"
        deadline = time.monotonic() + runtime.startup_timeout_seconds
        log = ""
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log = process.stdout.read() if process.stdout else ""
                    raise PreviewError(f"preview exited before health succeeded: {redact(log, 4000)}")
                try:
                    response = httpx.get(health_url, timeout=2)
                    if 200 <= response.status_code < 400:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
            else:
                raise PreviewError(f"preview health check timed out: {health_url}")

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
                target.parent.mkdir(parents=True, exist_ok=True)
                script = (
                    f'new_tab("http://127.0.0.1:{effective_runtime.port}/")\n'
                    "wait_for_load()\n"
                    f'capture_screenshot(r"{target}", max_dim=1280)\n'
                )
                capture = subprocess.run(
                    ["/opt/browser-use/bin/browser-harness"],
                    input=script,
                    cwd=worktree,
                    env=harness_environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=120,
                    check=False,
                )
                if capture.returncode != 0 or not target.is_file():
                    raise PreviewError(
                        f"browser-harness failed for {section.title}: {redact(capture.stdout, 4000)}"
                    )
                screenshots[section.slug] = relative
            return PreviewEvidence(health_url, screenshots, log)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
