#!/usr/bin/env python3
"""Minimal ACP bridge for the official Antigravity CLI print interface.

The bridge does not emulate a terminal or scrape the TUI. Each ACP prompt is
forwarded to the documented ``agy --print`` headless command, and the resulting
text is returned as one ACP agent message. OpenHands owns process cancellation
and workspace selection; the official CLI owns authentication and execution.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import uuid
from pathlib import Path
from typing import Any

import acp
from acp import schema

RESULT_MARKER = "OPENHANDS_SYMPHONY_RESULT="
SECRET_PATTERN = re.compile(
    r"(?i)(authorization:\s*(?:bearer|token)\s+)[^\s]+|"
    r"((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s]+"
)


def redact(value: str, limit: int = 50_000) -> str:
    return SECRET_PATTERN.sub(lambda match: f"{match.group(1) or match.group(2)}[REDACTED]", value)[-limit:]


def failure_kind(value: str) -> str:
    lowered = value.lower()
    if any(term in lowered for term in ("rate limit", "quota", "usage limit", "resource exhausted")):
        return "quota"
    if any(term in lowered for term in ("not signed in", "authentication", "unauthorized", "login required")):
        return "authentication"
    return "provider-tool"


class AntigravityBridge:
    def __init__(self) -> None:
        self.connection: Any = None
        self.sessions: dict[str, Path] = {}
        self.session_modes: dict[str, str] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.continue_sessions: set[str] = set()

    @staticmethod
    def _mode_state(current: str = "default") -> schema.SessionModeState:
        return schema.SessionModeState(
            current_mode_id=current,
            available_modes=[
                schema.SessionMode(
                    id="default",
                    name="Accept edits",
                    description="Run agy --sandbox --mode accept-edits for implementation.",
                ),
                schema.SessionMode(
                    id="plan",
                    name="Plan/read-only",
                    description="Run agy --sandbox --mode plan for independent review.",
                ),
            ],
        )

    def on_connect(self, connection: Any) -> None:
        self.connection = connection

    async def initialize(self, protocol_version: int, **kwargs: Any) -> schema.InitializeResponse:
        return schema.InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=schema.AgentCapabilities(
                load_session=True,
                session_capabilities=schema.SessionCapabilities(resume={}),
            ),
            agent_info=schema.Implementation(name="symphony-antigravity-bridge", version="1.0.0"),
        )

    async def new_session(self, cwd: str, **kwargs: Any) -> schema.NewSessionResponse:
        session_id = str(uuid.uuid4())
        self.sessions[session_id] = Path(cwd).resolve()
        self.session_modes[session_id] = "default"
        return schema.NewSessionResponse(session_id=session_id, modes=self._mode_state())

    async def load_session(self, cwd: str, session_id: str, **kwargs: Any) -> schema.LoadSessionResponse:
        self.sessions[session_id] = Path(cwd).resolve()
        self.continue_sessions.add(session_id)
        mode = self.session_modes.setdefault(session_id, "default")
        return schema.LoadSessionResponse(modes=self._mode_state(mode))

    async def resume_session(self, session_id: str, cwd: str, **kwargs: Any) -> schema.ResumeSessionResponse:
        self.sessions[session_id] = Path(cwd).resolve()
        self.continue_sessions.add(session_id)
        mode = self.session_modes.setdefault(session_id, "default")
        return schema.ResumeSessionResponse(modes=self._mode_state(mode))

    async def set_session_mode(self, session_id: str, mode_id: str, **kwargs: Any) -> schema.SetSessionModeResponse:
        if mode_id not in {"default", "plan"}:
            raise ValueError(f"unsupported Antigravity session mode: {mode_id}")
        if session_id not in self.sessions:
            raise ValueError(f"unknown Antigravity session: {session_id}")
        self.session_modes[session_id] = mode_id
        return schema.SetSessionModeResponse()

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> schema.PromptResponse:
        cwd = self.sessions[session_id]
        prompt_text = "\n".join(str(getattr(block, "text", "")) for block in prompt)
        agent_mode = "plan" if self.session_modes.get(session_id) == "plan" else "accept-edits"
        binary = os.environ.get("SYMPHONY_AGY_PATH", "/usr/local/bin/agy")
        print_timeout = os.environ.get("SYMPHONY_AGY_PRINT_TIMEOUT", "120m")
        environment = os.environ.copy()
        environment["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
        for name in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_CONFIG_DIR",
            "BROWSER_USE_API_KEY",
            "LOCAL_BACKEND_API_KEY",
        ):
            environment.pop(name, None)
        command = [
            binary,
            "--sandbox",
            "--mode",
            agent_mode,
            "--print-timeout",
            print_timeout,
            "--print",
            prompt_text,
        ]
        if session_id in self.continue_sessions:
            command.insert(1, "--continue")
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        self.processes[session_id] = process
        try:
            output_bytes, _ = await process.communicate()
        finally:
            self.processes.pop(session_id, None)
        output = redact(output_bytes.decode(errors="replace"))
        if process.returncode == 0:
            self.continue_sessions.add(session_id)
        if process.returncode:
            kind = failure_kind(output)
            payload = {
                "outcome": "failed",
                "summary": "Antigravity headless execution failed.",
                "question_or_reason": output[-4000:] or f"agy exited with status {process.returncode}",
                "failure_kind": kind,
                "exit_code": process.returncode,
            }
            output = f"{RESULT_MARKER}{json.dumps(payload, separators=(',', ':'))}"
        await self.connection.session_update(
            session_id,
            schema.AgentMessageChunk(
                session_update="agent_message_chunk",
                content=schema.TextContentBlock(type="text", text=output),
            ),
        )
        return schema.PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        process = self.processes.get(session_id)
        if process and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return

    async def close_session(self, session_id: str, **kwargs: Any) -> None:
        await self.cancel(session_id)
        self.sessions.pop(session_id, None)
        self.session_modes.pop(session_id, None)
        self.continue_sessions.discard(session_id)

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None


if __name__ == "__main__":
    asyncio.run(acp.run_agent(AntigravityBridge()))
