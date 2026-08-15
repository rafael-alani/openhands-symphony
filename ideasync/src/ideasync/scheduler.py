from __future__ import annotations

import os
import platform
import plistlib
import subprocess
import sys
from typing import Protocol

from ideasync.errors import IdeasyncError
from ideasync.fs import atomic_write
from ideasync.paths import AppPaths

LABEL = "io.openai.ideasync"


class Scheduler(Protocol):
    def install(self, *, dry_run: bool) -> str: ...

    def uninstall(self, *, dry_run: bool) -> str: ...


class LaunchdScheduler:
    """A launchd scheduler whose only file lives inside the ideasync data root."""

    def __init__(self, paths: AppPaths, *, interval_seconds: int = 120) -> None:
        self.paths = paths
        self.interval_seconds = interval_seconds
        self.plist_path = paths.launchd_dir / f"{LABEL}.plist"

    @property
    def domain(self) -> str:
        return f"gui/{os.getuid()}"

    def render(self) -> bytes:
        definition = {
            "Label": LABEL,
            "ProgramArguments": [
                sys.executable,
                "-m",
                "ideasync",
                "--data-dir",
                str(self.paths.data_dir),
                "sync",
            ],
            "RunAtLoad": True,
            "StartInterval": self.interval_seconds,
            "ProcessType": "Background",
            "EnvironmentVariables": {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            },
            "StandardOutPath": str(self.paths.data_dir / "logs" / "launchd.stdout.log"),
            "StandardErrorPath": str(self.paths.data_dir / "logs" / "launchd.stderr.log"),
        }
        return plistlib.dumps(definition, fmt=plistlib.FMT_XML, sort_keys=True)

    def _require_macos(self) -> None:
        if platform.system() != "Darwin":
            raise IdeasyncError("launchd schedule installation is supported only on macOS")

    def install(self, *, dry_run: bool) -> str:
        payload = self.render()
        if dry_run:
            return payload.decode("utf-8")
        self._require_macos()
        atomic_write(self.paths.data_dir, self.plist_path, payload)
        subprocess.run(
            ["launchctl", "bootout", f"{self.domain}/{LABEL}"],
            check=False,
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            ["launchctl", "bootstrap", self.domain, str(self.plist_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise IdeasyncError(f"launchctl bootstrap failed: {(result.stderr or result.stdout).strip()}")
        return f"installed {LABEL} every {self.interval_seconds} seconds from {self.plist_path}"

    def uninstall(self, *, dry_run: bool) -> str:
        if dry_run:
            return f"would boot out {self.domain}/{LABEL} and remove {self.plist_path}"
        self._require_macos()
        result = subprocess.run(
            ["launchctl", "bootout", f"{self.domain}/{LABEL}"],
            check=False,
            capture_output=True,
            text=True,
        )
        error_text = result.stderr or result.stdout
        not_loaded = any(text in error_text for text in ("Could not find service", "No such process", "service not found"))
        missing_service = result.returncode != 0 and not not_loaded
        if missing_service:
            raise IdeasyncError(f"launchctl bootout failed: {(result.stderr or result.stdout).strip()}")
        self.plist_path.unlink(missing_ok=True)
        return f"uninstalled {LABEL}"


def scheduler_for(paths: AppPaths) -> Scheduler:
    return LaunchdScheduler(paths)
