from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models import AuthStatus, ProviderCapabilities, ProviderResult, ProviderRun, QuotaState


class ProviderAdapter(ABC):
    name: str

    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    @abstractmethod
    def auth_status(self) -> AuthStatus: ...

    @abstractmethod
    def health(self) -> tuple[bool, str]: ...

    @abstractmethod
    def quota_or_rate_limit_state(self) -> QuotaState: ...

    @abstractmethod
    def start(self, workspace: Path, prompt: str, run_id: str, *, read_only: bool = False) -> ProviderRun: ...

    @abstractmethod
    def resume(self, run: ProviderRun, prompt: str | None = None) -> ProviderRun: ...

    @abstractmethod
    def wait(self, run: ProviderRun, timeout_seconds: int) -> ProviderResult: ...

    @abstractmethod
    def cancel(self, run: ProviderRun) -> None: ...
