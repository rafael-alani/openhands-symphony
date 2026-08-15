from __future__ import annotations

import fcntl
import json
import os
import platform
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ideasync.errors import LockBusy
from ideasync.paths import ensure_within


class StructuredLogger:
    def __init__(self, root: Path, path: Path) -> None:
        self.root = root
        self.path = path

    def write(self, event: str, *, level: str = "info", **fields: object) -> None:
        ensure_within(self.root, self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level,
            "event": event,
            **fields,
        }
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
        finally:
            os.close(descriptor)


class Notifier:
    def failure(self, message: str) -> None:
        if platform.system() != "Darwin":
            return
        script = "on run argv\ndisplay notification (item 1 of argv) with title (item 2 of argv)\nend run"
        result = subprocess.run(
            ["osascript", "-e", script, "--", message[:240], "ideasync failed"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise OSError(f"osascript notification failed: {(result.stderr or result.stdout).strip()}")


@contextmanager
def repository_lock(root: Path, path: Path, repository: str) -> Iterator[None]:
    ensure_within(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusy(f"{repository}: another sync holds {path.name}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
