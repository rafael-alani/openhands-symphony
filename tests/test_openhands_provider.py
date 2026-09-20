from __future__ import annotations

from pathlib import Path

import pytest

from symphony.models import ProviderOutcome, ProviderRun
from symphony.providers.openhands import OpenHandsACPProvider


@pytest.mark.parametrize(
    ("provider_name", "read_only", "expected_mode"),
    (
        ("claude", False, "bypassPermissions"),
        ("codex", False, "agent-full-access"),
        ("antigravity", False, "default"),
        ("claude", True, "plan"),
        ("codex", True, "read-only"),
        ("antigravity", True, "plan"),
    ),
)
def test_start_selects_full_implementation_and_read_only_review(
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
    assert settings["acp_prompt_timeout"] == 600.0


def test_agent_server_response_envelope_yields_structured_summary() -> None:
    run = ProviderRun("codex", "conversation-id", "session-id")
    payload = {
        "response": (
            "Progress update that must not become the PR summary.\n"
            'OPENHANDS_SYMPHONY_RESULT={"outcome":"completed","summary":"Implemented issue scope",'
            '"question_or_reason":""}'
        )
    }

    result = OpenHandsACPProvider._parse_result(OpenHandsACPProvider._final_text(payload), run)

    assert result.outcome == ProviderOutcome.COMPLETED
    assert result.summary == "Implemented issue scope"
    assert result.raw["outcome"] == "completed"


def test_missing_structured_result_fails_closed() -> None:
    run = ProviderRun("codex", "conversation-id", "session-id")

    result = OpenHandsACPProvider._parse_result("Agent says it is done.", run)

    assert result.outcome == ProviderOutcome.FAILED
    assert result.raw["failure_kind"] == "provider-tool"
    assert "OPENHANDS_SYMPHONY_RESULT" in result.question_or_reason


@pytest.mark.parametrize(("name", "expected"), [("codex", "agent"), ("claude", "acceptEdits")])
def test_restricted_mode_remains_explicitly_available(tmp_path, monkeypatch, name, expected):
    provider = OpenHandsACPProvider(name, "http://localhost", ("fake",), ("fake",), permission_mode="restricted")
    captured = {}

    def request(method, path, **kwargs):
        captured.update(kwargs["json"])
        return {"id": "id"}

    monkeypatch.setattr(provider, "_request", request)
    provider.start(tmp_path, "work", "run")
    assert captured["agent_settings"]["acp_session_mode"] == expected
