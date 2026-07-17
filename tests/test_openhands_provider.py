from __future__ import annotations

from pathlib import Path

import pytest

from symphony.providers.openhands import OpenHandsACPProvider


@pytest.mark.parametrize(
    ("provider_name", "read_only", "expected_mode"),
    (
        ("claude", False, "acceptEdits"),
        ("codex", False, "agent"),
        ("antigravity", False, "default"),
        ("claude", True, "plan"),
        ("codex", True, "read-only"),
        ("antigravity", True, "plan"),
    ),
)
def test_start_selects_unattended_non_bypass_mode(
    tmp_path: Path,
    provider_name: str,
    read_only: bool,
    expected_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenHandsACPProvider(
        provider_name,
        "http://127.0.0.1:8000",
        ("fake-acp",),
        ("fake-auth",),
    )
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs: object) -> dict[str, str]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "conversation-id"}

    monkeypatch.setattr(provider, "_request", fake_request)
    provider.start(tmp_path, "implement the issue", "run-id", read_only=read_only)

    payload = captured["json"]
    assert isinstance(payload, dict)
    settings = payload["agent_settings"]
    assert isinstance(settings, dict)
    assert settings["acp_session_mode"] == expected_mode
    assert settings["acp_session_mode"] not in {"bypassPermissions", "agent-full-access"}
    assert settings["acp_prompt_timeout"] == 600.0
