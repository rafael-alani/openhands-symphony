from __future__ import annotations

import itertools
from pathlib import Path

from ..models import (
    AuthStatus,
    ProviderCapabilities,
    ProviderOutcome,
    ProviderResult,
    ProviderRun,
    QuotaState,
)
from .base import ProviderAdapter


class FakeProvider(ProviderAdapter):
    """Deterministic provider used only by tests and the local integration harness."""

    def __init__(
        self,
        name: str = "fake",
        outcome: ProviderOutcome = ProviderOutcome.COMPLETED,
        *,
        write_files: dict[str, str] | None = None,
        result_data: dict[str, object] | None = None,
    ):
        self.name = name
        self.outcome = outcome
        self.starts: list[tuple[Path, str, str, bool]] = []
        self.cancels: list[str] = []
        self.resumes: list[tuple[ProviderRun, str | None]] = []
        self.write_files = write_files or {}
        self.result_data = result_data or {}
        self._ids = itertools.count(1)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, True, True, "test-only provider")

    def auth_status(self) -> AuthStatus:
        return AuthStatus(True, True, "deterministic fake")

    def health(self) -> tuple[bool, str]:
        return True, "deterministic fake"

    def quota_or_rate_limit_state(self) -> QuotaState:
        return QuotaState()

    def start(self, workspace: Path, prompt: str, run_id: str, *, read_only: bool = False) -> ProviderRun:
        self.starts.append((workspace, prompt, run_id, read_only))
        if not read_only:
            for relative, content in self.write_files.items():
                target = (workspace / relative).resolve()
                if not target.is_relative_to(workspace.resolve()):
                    raise ValueError("fake provider path escapes workspace")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
        identifier = f"fake-{next(self._ids)}"
        return ProviderRun(self.name, identifier, identifier)

    def resume(self, run: ProviderRun, prompt: str | None = None) -> ProviderRun:
        self.resumes.append((run, prompt))
        return run

    def wait(self, run: ProviderRun, timeout_seconds: int) -> ProviderResult:
        messages = {
            ProviderOutcome.COMPLETED: ("Fake implementation completed.", ""),
            ProviderOutcome.NEEDS_GUIDANCE: ("A product decision is required.", "Choose option A or option B."),
            ProviderOutcome.BLOCKED: ("The environment is blocked.", "Required test service is unavailable."),
            ProviderOutcome.FAILED: ("Fake implementation failed.", "Deterministic failure."),
            ProviderOutcome.CANCELED: ("Fake run canceled.", ""),
        }
        summary, reason = messages[self.outcome]
        return ProviderResult(
            self.outcome, summary, reason, run.conversation_id, run.session_id, dict(self.result_data)
        )

    def cancel(self, run: ProviderRun) -> None:
        self.cancels.append(run.conversation_id)
