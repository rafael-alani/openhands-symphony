from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .models import ValidationResult, utcnow

SECRET_PATTERN = re.compile(
    r"(?i)(authorization:\s*(?:bearer|token)\s+)[^\s]+|((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s]+"
)
SENSITIVE_ENV = {
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "BROWSER_USE_API_KEY",
    "GH_CONFIG_DIR",
}


def redact(value: str, limit: int = 50_000) -> str:
    return SECRET_PATTERN.sub(lambda match: f"{match.group(1) or match.group(2)}[REDACTED]", value)[-limit:]


def validation_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in SENSITIVE_ENV}


def validation_argv(
    command: tuple[str, ...],
    run_as_user: str,
    extra_environment: dict[str, str] | None = None,
) -> list[str]:
    if not run_as_user:
        return list(command)
    environment = []
    for key, value in (extra_environment or {}).items():
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) or "\0" in value:
            raise ValueError("validation environment contains an unsafe assignment")
        environment.append(f"{key}={value}")
    return [
        "sudo",
        "-n",
        "-H",
        "-u",
        run_as_user,
        "--",
        "env",
        "-i",
        f"HOME=/var/lib/{run_as_user}",
        "PATH=/opt/browser-use/bin:/usr/local/bin:/usr/bin:/bin",
        "CI=true",
        *environment,
        "/bin/sh",
        "-c",
        'umask 0007; exec "$@"',
        "symphony-validation",
        *command,
    ]


def run_validation(
    command: tuple[str, ...],
    worktree: Path,
    timeout_seconds: int,
    *,
    run_as_user: str = "",
) -> ValidationResult:
    if not command:
        from .workspace import WorkspaceError

        raise WorkspaceError("empty validation command")
    started = utcnow()
    try:
        process = subprocess.run(
            validation_argv(command, run_as_user),
            cwd=worktree,
            env=validation_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
        return ValidationResult(command, process.returncode, started, utcnow(), redact(process.stdout))
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return ValidationResult(command, None, started, utcnow(), redact(output), timed_out=True)
