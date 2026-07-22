from __future__ import annotations

import json
import os
import shlex
from dataclasses import asdict
from pathlib import Path

from .models import Job
from .store import Store
from .workspace import redact


class ReportWriter:
    def __init__(self, root: Path, store: Store):
        self.root = root
        self.store = store
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def write(self, job: Job) -> tuple[Path, Path]:
        directory = self.root / job.id
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        validations = self.store.validations(job.id)
        events = self.store.events(job.id)
        payload = {"job": asdict(job), "validations": validations, "events": events}
        payload["job"]["state"] = str(job.state)
        json_path = directory / "run.json"
        markdown_path = directory / "run.md"
        json_path.write_text(redact(json.dumps(payload, indent=2, sort_keys=True, default=str), 5_000_000) + "\n")
        markdown_path.write_text(self._markdown(job, validations, events))
        os.chmod(json_path, 0o600)
        os.chmod(markdown_path, 0o600)
        return markdown_path, json_path

    @staticmethod
    def _markdown(
        job: Job,
        validations: list[dict[str, object]],
        events: list[dict[str, object]],
    ) -> str:
        lines = [
            f"# Run {job.id}",
            "",
            f"- Repository: `{job.repository}`",
            f"- Issue: `#{job.issue_number}`",
            f"- State: `{job.state}`",
            f"- Provider: `{job.implementation_provider}`",
            f"- Attempt: `{job.attempt}`",
            f"- Branch: `{job.branch}`",
            f"- Conversation: `{job.conversation_id or 'none'}`",
            f"- PR: `{job.pr_url or 'none'}`",
            f"- Outcome: `{job.terminal_outcome or 'in progress'}`",
            f"- Reason: {redact(job.terminal_reason or job.actionable_message or 'none', 20_000)}",
            "",
            "## Validation",
            "",
        ]
        if not validations:
            lines.append("No configured validation command ran.")
        for result in validations:
            command = shlex.join(json.loads(str(result["command_json"])))
            status = "PASS" if result["exit_code"] == 0 and not result["timed_out"] else "FAIL"
            lines.extend(
                [
                    f"### `{command}` — {status}",
                    "",
                    f"Exit: `{result['exit_code']}`; timeout: `{bool(result['timed_out'])}`",
                    "",
                    "```text",
                    str(result["output"])[-10000:],
                    "```",
                    "",
                ]
            )
        lines.extend(["", "## Event history", ""])
        for event in events:
            try:
                detail = json.loads(str(event["detail_json"]))
            except (KeyError, TypeError, ValueError):
                detail = {}
            transition = ""
            if detail.get("from") or detail.get("to"):
                transition = f" {detail.get('from', '?')} -> {detail.get('to', '?')}"
            phase = f" phase={detail['phase']}" if detail.get("phase") else ""
            message = detail.get("terminal_reason") or detail.get("actionable_message") or ""
            suffix = f" — {redact(str(message), 10_000)}" if message else ""
            lines.append(
                f"- `{event.get('at', 'unknown')}` `{event.get('kind', 'event')}`{transition}{phase}{suffix}"
            )
        return "\n".join(lines) + "\n"
