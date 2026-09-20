from __future__ import annotations

import difflib

from .models import IdeaRun
from .providers.openhands import RESULT_MARKER


def idea_implementation_prompt(
    run: IdeaRun,
    previous_spec: bytes | None,
    global_instruction: str,
    repository_instruction: str,
) -> str:
    current = run.spec_content.decode("utf-8")
    previous = previous_spec.decode("utf-8") if previous_spec is not None else ""
    diff = "".join(
        difflib.unified_diff(
            previous.splitlines(keepends=True),
            current.splitlines(keepends=True),
            fromfile="last-completed/idea/SPEC.md",
            tofile="accepted/idea/SPEC.md",
        )
    ) or "(no textual difference)"
    progress = run.previous_progress.decode("utf-8") if run.previous_progress else "(none yet)"
    instructions = "\n\n".join(value.strip() for value in (global_instruction, repository_instruction) if value.strip())
    return f"""You are the unattended ideas implementation worker for run {run.id}.

Implement the smallest useful solution for the changed wishes and any unresolved pending work identified in a supplied project checklist. Work only inside the isolated worktree. Never edit idea/SPEC.md. Do not use gh, push, create issues or pull requests, add plans, perform a self-review, deploy, or otherwise touch GitHub. The wrapper owns validation, preview screenshots, PROGRESS.md, commits, and publication.

Respect repository-native AGENTS.md and other repository instructions. You have unattended implementation permissions inside the dedicated VM. Install dependencies and run the commands needed to build the project. Maintain .openhands/setup.sh with reproducible, noninteractive dependency installation and build commands for a fresh Git checkout (including ignored dependencies/build output); it runs again before previews. Maintain .openhands/quality-gate.sh as the noninteractive entry point for meaningful automated behavior checks (for example, npm test). Symphony discovers that exact path; a package.json test script alone is not its quality-gate entry point. Run the gate before reporting completion. Update .symphony/idea.toml to launch the actual application, bind it to 127.0.0.1, honor its supplied PORT or {{port}} argument, and keep its declared port stable. Do not leave the bootstrap directory listing as the finished app. Keep secrets and dependency directories out of Git. Ask a focused question only for missing product decisions or credentials. Leave intended code changes in the worktree and emit exactly one final structured line:

{RESULT_MARKER}{{"outcome":"completed|needs-guidance|blocked|failed","summary":"one or two terse factual sentences","question_or_reason":"one focused question or failure"}}

Repository: {run.repository}
Accepted spec blob: {run.spec_hash}
Accepted base commit: {run.base_commit}

<current-spec>
{current}
</current-spec>

<diff-from-last-completed-spec>
{diff}
</diff-from-last-completed-spec>

<previous-progress>
{progress}
</previous-progress>

<configured-repository-instructions>
{instructions or '(none)'}
</configured-repository-instructions>
"""
