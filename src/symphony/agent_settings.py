"""Validated task overrides, independent of provider transport and credentials."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
SPEEDS = ("normal", "fast")
SETTING_LABELS = {
    *(f"reasoning:{value}" for value in REASONING_EFFORTS),
    *(f"speed:{value}" for value in SPEEDS),
}


@dataclass(frozen=True)
class AgentSettings:
    reasoning_effort: str | None = None
    speed: str | None = None

    def __post_init__(self) -> None:
        for name, choices in (("reasoning_effort", REASONING_EFFORTS), ("speed", SPEEDS)):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or value not in choices):
                raise ValueError(f"{name} must be one of: {', '.join(choices)}")

    def overlay(self, override: AgentSettings) -> AgentSettings:
        return AgentSettings(override.reasoning_effort or self.reasoning_effort, override.speed or self.speed)

    def for_provider(self, provider: str) -> AgentSettings:
        if provider != "codex" and self != AgentSettings():
            raise ValueError(f"reasoning_effort and speed overrides are currently supported only for Codex, not {provider}")
        return self

    def values(self) -> dict[str, str]:
        return {key: value for key in ("reasoning_effort", "speed") if (value := getattr(self, key)) is not None}

    @classmethod
    def parse(cls, values: dict[str, Any]) -> AgentSettings:
        return cls(**{key: values[key] for key in ("reasoning_effort", "speed") if key in values})

    @classmethod
    def from_labels(cls, labels: tuple[str, ...]) -> AgentSettings:
        values = {}
        for prefix, field in (("reasoning:", "reasoning_effort"), ("speed:", "speed")):
            selected = [label[len(prefix):] for label in labels if label.startswith(prefix)]
            if len(selected) > 1:
                raise ValueError(f"at most one {prefix} label is allowed")
            if selected:
                values[field] = selected[0]
        return cls.parse(values)


INHERIT_SETTINGS = AgentSettings()
