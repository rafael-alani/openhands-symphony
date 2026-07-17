#!/usr/bin/env python3
"""Minimal deterministic ACP server for the pinned OpenHands capability spike."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path
from typing import Any

import acp
from acp import schema


class FakeAgent:
    def __init__(self) -> None:
        self.connection: Any = None
        self.sessions: dict[str, str] = {}
        self.canceled: set[str] = set()

    def on_connect(self, conn: Any) -> None:
        self.connection = conn

    async def initialize(self, protocol_version: int, **kwargs: Any) -> schema.InitializeResponse:
        return schema.InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=schema.AgentCapabilities(
                load_session=True,
                session_capabilities=schema.SessionCapabilities(resume={}),
            ),
            agent_info=schema.Implementation(name="symphony-fake-acp", version="1.0.0"),
        )

    async def new_session(self, cwd: str, **kwargs: Any) -> schema.NewSessionResponse:
        session_id = str(uuid.uuid4())
        self.sessions[session_id] = cwd
        return schema.NewSessionResponse(session_id=session_id)

    async def load_session(self, cwd: str, session_id: str, **kwargs: Any) -> schema.LoadSessionResponse:
        self.sessions[session_id] = cwd
        return schema.LoadSessionResponse()

    async def resume_session(self, session_id: str, cwd: str, **kwargs: Any) -> schema.ResumeSessionResponse:
        self.sessions[session_id] = cwd
        self.canceled.discard(session_id)
        return schema.ResumeSessionResponse()

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> schema.PromptResponse:
        prompt_text = "\n".join(str(getattr(block, "text", "")) for block in prompt)
        match = re.search(r"delay=(\d+(?:\.\d+)?)", prompt_text)
        delay = float(match.group(1)) if match else float(os.environ.get("FAKE_ACP_DELAY_SECONDS", "0"))
        for _ in range(max(1, int(delay * 10))):
            if session_id in self.canceled:
                self.canceled.discard(session_id)
                return schema.PromptResponse(stop_reason="cancelled")
            await asyncio.sleep(0.1)
        cwd = Path(self.sessions[session_id])
        marker = cwd / "fake-acp-workspace.txt"
        credential_visible = (
            bool(os.environ.get("FAKE_SUBSCRIPTION_CREDENTIAL")) or (Path.home() / ".codex" / "auth.json").is_file()
        )
        marker.write_text(f"session={session_id}\ncredential_visible={credential_visible}\n")
        text = (
            "OPENHANDS_SYMPHONY_RESULT="
            '{"outcome":"completed","summary":"Pinned ACP spike completed.","question_or_reason":""}'
        )
        await self.connection.session_update(
            session_id,
            schema.AgentMessageChunk(
                session_update="agent_message_chunk",
                content=schema.TextContentBlock(type="text", text=text),
            ),
        )
        return schema.PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self.canceled.add(session_id)

    async def close_session(self, session_id: str, **kwargs: Any) -> None:
        self.sessions.pop(session_id, None)

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None


if __name__ == "__main__":
    asyncio.run(acp.run_agent(FakeAgent()))
