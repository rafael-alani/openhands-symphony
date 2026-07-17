from __future__ import annotations

from datetime import UTC, datetime

from .models import Job
from .workspace import redact


def _elapsed(started_at: str | None, finished_at: str | None = None) -> str:
    if not started_at:
        return "not started"
    start = datetime.fromisoformat(started_at)
    end = datetime.fromisoformat(finished_at) if finished_at else datetime.now(UTC)
    seconds = max(0, int((end - start).total_seconds()))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s"


def _cell(value: str) -> str:
    return redact(value, 4000).replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def render_status(job: Job) -> str:
    pr = f"[#{job.pr_number}]({job.pr_url})" if job.pr_number and job.pr_url else "—"
    question = _cell(job.actionable_message or job.terminal_reason or "—")
    validation = _cell(job.validation_summary or "not run")
    started = job.started_at or "—"
    return "\n".join(
        [
            "### Autonomous implementation status",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| State | `{job.state}` |",
            f"| Provider / attempt | `{job.implementation_provider}` / `{job.attempt}` |",
            f"| Started / elapsed | `{started}` / `{_elapsed(job.started_at, job.finished_at)}` |",
            f"| Current phase | `{job.phase}` |",
            f"| Branch | `{job.branch}` |",
            f"| Pull request | {pr} |",
            f"| Validation | {validation} |",
            f"| Action required / latest failure | {question} |",
            f"| Run ID | `{job.id}` |",
            "",
            "This single bot-owned comment is updated in place.",
        ]
    )
