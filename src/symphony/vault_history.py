"""Readable audit history projected only into generated vault reports."""
from __future__ import annotations

import json
import re
from urllib.parse import quote

from .store import Store
from .vault_status import markdown


def diagnostic(value: str) -> str:
    # Older adapters stored the tail of the conversation object rather than the
    # actual error. Keep that payload in appdata; it is not a useful note summary.
    if any(key in value for key in ('"acp_command"', '"acp_file_secrets"', '"llm"')):
        return "The provider failed without a readable error summary. Detailed diagnostics remain in the Symphony logs."
    return markdown(value)


def run_history(store: Store, repository: str, progress_path: str) -> str:
    runs = [run for run in store.list_idea_runs() if run.repository == repository]
    if not runs:
        return ""
    lines = ["", "## Run history", "", "Earlier runs and attempts are retained below; the latest result stays above."]
    for run in reversed(runs):
        lines.extend(["", f"### Run {run.id}", "",
                      f"- Status: **{markdown(str(run.state).capitalize())}** · {markdown(run.phase)}",
                      f"- Provider: **{markdown(run.implementation_provider)}**",
                      f"- Created: {run.created_at}", f"- Last update: {run.updated_at}",
                      f"- Provider attempts: {run.attempt}"])
        events = store.idea_events(run.id)
        for event in events:
            if event["kind"] == "checkbox-retry":
                detail = json.loads(event["detail_json"])
                lines.append(f"- Retry of: [previous run](#Run%20{quote(detail['parent'], safe='')})")
                lines.append("- Requested tasks: " + ", ".join(markdown(key) for key in detail["sources"]))
            elif event["kind"] == "retry-unchanged":
                lines.append("- The retry passed without changing the validated repository version.")
        if run.published_commit and re.fullmatch(r"[a-f0-9]{40}", run.published_commit):
            base = f"https://github.com/{repository}"
            lines.append(f"- [Published result]({base}/blob/{run.published_commit}/{quote(progress_path, safe='/')})"
                         f" · [Commit]({base}/commit/{run.published_commit})")
        if run.validation_summary:
            lines.append("- Validation: " + diagnostic(run.validation_summary))
        if run.question:
            lines.append("- Message: " + diagnostic(run.question))
        starts = [event for event in events if event["kind"] == "attempt-started"]
        validations = store.idea_validations(run.id)
        if starts:
            lines.extend(["", "#### Attempts", ""])
        for index, event in enumerate(starts):
            detail = json.loads(event["detail_json"])
            stop = starts[index + 1]["id"] if index + 1 < len(starts) else float("inf")
            transitions = [json.loads(item["detail_json"]) for item in events
                           if event["id"] < item["id"] < stop and item["kind"] == "transition"]
            result = transitions[-1] if transitions else {}
            phase = result.get("phase") or (run.phase if index + 1 == len(starts) else "finished")
            lines.append(f"- **Attempt {detail['attempt']}** · {event['at']} · {markdown(phase)}")
            if detail.get("conversation_id"):
                lines.append(f"  - Conversation: `{markdown(detail['conversation_id'])}`")
        if validations:
            lines.extend(["", "#### Checks", ""])
            for check in validations:
                command = json.loads(check["command_json"])
                label = " ".join(command)
                result = "Timed out" if check["timed_out"] else f"Exit {check['exit_code']}"
                lines.append(f"- Attempt {check['attempt']} · {markdown(result)} · {markdown(label)}")
        elif not starts:
            lines.extend(["", "No provider attempt was recorded for this run."])
    return "\n".join(lines) + "\n"


def append_history(progress: bytes, history: str) -> bytes:
    # Input comes from the Git-owned progress file, never the previously
    # decorated vault copy. No marker or history becomes model input.
    return progress + ("\n" + history).encode() if history else progress
