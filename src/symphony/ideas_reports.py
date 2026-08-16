from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import IdeaRun
from .reporting import RunReportArtifacts
from .store import Store
from .validation import redact


class IdeaReportWriter:
    def __init__(self, root: Path, store: Store):
        self.store = store
        self.artifacts = RunReportArtifacts(root)

    def write(self, run: IdeaRun) -> tuple[Path, Path]:
        validations = self.store.idea_validations(run.id)
        events = self.store.idea_events(run.id)
        payload = {"idea_run": asdict(run), "validations": validations, "events": events}
        payload["idea_run"]["state"] = str(run.state)
        payload["idea_run"]["spec_content"] = run.spec_content.decode("utf-8", errors="replace")
        payload["idea_run"]["runtime_content"] = run.runtime_content.decode("utf-8", errors="replace")
        payload["idea_run"]["previous_progress"] = run.previous_progress.decode("utf-8", errors="replace")
        lines = [
            f"# Idea run {run.id}",
            "",
            f"- Repository: `{run.repository}`",
            f"- Spec hash: `{run.spec_hash}`",
            f"- Base commit: `{run.base_commit}`",
            f"- State: `{run.state}`",
            f"- Provider: `{run.implementation_provider}`",
            f"- Attempt: `{run.attempt}`",
            f"- Phase: `{run.phase}`",
            f"- Published commit: `{run.published_commit or 'none'}`",
            f"- Validation: {redact(run.validation_summary or 'not run', 20_000)}",
            f"- Question: {redact(run.question or 'none', 20_000)}",
            "",
            "## Event history",
            "",
        ]
        for event in events:
            lines.append(f"- `{event.get('at', 'unknown')}` `{event.get('kind', 'event')}`")
        return self.artifacts.write(
            run.id,
            "\n".join(lines) + "\n",
            json.dumps(payload, indent=2, sort_keys=True, default=str),
        )
