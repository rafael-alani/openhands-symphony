#!/usr/bin/env python3
"""Exercise a running pinned Agent Server with the deterministic ACP server."""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

import httpx


def request(client: httpx.Client, method: str, path: str, **kwargs):
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else None


def state(payload: dict) -> str:
    return str(payload.get("execution_status") or payload.get("status") or "unknown").lower()


def wait(client: httpx.Client, conversation_id: str, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = request(client, "GET", f"/api/conversations/{conversation_id}")
        if state(payload) in {"finished", "paused", "error", "stopped", "completed"}:
            return payload
        time.sleep(0.25)
    raise TimeoutError(f"conversation {conversation_id} did not stop")


def create(client: httpx.Client, workspace: Path, fake_command: list[str], delay: float = 0) -> str:
    payload = {
        "workspace": {"working_dir": str(workspace), "kind": "LocalWorkspace"},
        "agent_settings": {
            "agent_kind": "acp",
            "acp_server": "custom",
            "acp_command": fake_command,
            "acp_args": [],
            "acp_prompt_timeout": 10.0,
            "acp_isolate_data_dir": False,
        },
        "initial_message": {
            "role": "user",
            "content": [{"type": "text", "text": f"spike delay={delay}"}],
            "run": False,
        },
        "max_iterations": 10,
        "autotitle": False,
        "tags": {"spike": uuid.uuid4().hex[:12]},
    }
    created = request(client, "POST", "/api/conversations", json=payload)
    return str(created["id"])


def run_if_idle(client: httpx.Client, conversation_id: str) -> None:
    current = request(client, "GET", f"/api/conversations/{conversation_id}")
    if state(current) in {"idle", "paused", "stopped", "finished"}:
        request(client, "POST", f"/api/conversations/{conversation_id}/run")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--python", required=True, help="Python containing agent-client-protocol<0.11")
    parser.add_argument("--fake-agent", default=str(Path(__file__).with_name("fake_acp_agent.py")))
    parser.add_argument("--root", default="/private/tmp/openhands-symphony-acp-spike")
    parser.add_argument("--api-key-file", help="Agent Canvas api-key.txt (never printed)")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    first = root / "first"
    second = root / "second"
    first.mkdir(parents=True, exist_ok=True)
    second.mkdir(parents=True, exist_ok=True)
    command = [args.python, str(Path(args.fake_agent).resolve())]

    headers = {}
    if args.api_key_file:
        headers["X-Session-API-Key"] = Path(args.api_key_file).read_text().strip()
    with httpx.Client(base_url=args.url, timeout=30, headers=headers) as client:
        server = request(client, "GET", "/server_info")
        conversation_a = create(client, first, command)
        conversation_b = create(client, second, command)
        # Creating with an initial message starts the run in Agent Server 1.35.0.
        finished_a = wait(client, conversation_a)
        finished_b = wait(client, conversation_b)

        marker_a = (first / "fake-acp-workspace.txt").read_text()
        marker_b = (second / "fake-acp-workspace.txt").read_text()
        if marker_a == marker_b or not marker_a or not marker_b:
            raise AssertionError("concurrent ACP conversations did not use isolated workspaces/sessions")

        cancel_workspace = root / "cancel-resume"
        cancel_workspace.mkdir(parents=True, exist_ok=True)
        cancel_conversation = create(client, cancel_workspace, command, delay=5)
        time.sleep(0.5)
        request(client, "POST", f"/api/conversations/{cancel_conversation}/interrupt")
        paused = wait(client, cancel_conversation)
        request(
            client,
            "POST",
            f"/api/conversations/{cancel_conversation}/events",
            json={"role": "user", "content": [{"type": "text", "text": "resume delay=0"}], "run": True},
        )
        resumed = wait(client, cancel_conversation)

        output = {
            "server": server,
            "conversation_ids": [conversation_a, conversation_b],
            "cancel_resume_conversation_id": cancel_conversation,
            "workspace_isolation": True,
            "credential_inheritance_visible": "credential_visible=True" in marker_a,
            "first_terminal_state": state(finished_a),
            "second_terminal_state": state(finished_b),
            "pause_state": state(paused),
            "resume_terminal_state": state(resumed),
        }
        print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
