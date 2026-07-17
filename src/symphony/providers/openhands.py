from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from ..models import (
    AuthStatus,
    ProviderCapabilities,
    ProviderOutcome,
    ProviderResult,
    ProviderRun,
    QuotaState,
)
from .base import ProviderAdapter

RESULT_MARKER = "OPENHANDS_SYMPHONY_RESULT="


class OpenHandsProviderError(RuntimeError):
    pass


class OpenHandsACPProvider(ProviderAdapter):
    def __init__(
        self,
        name: str,
        agent_server_url: str,
        acp_command: tuple[str, ...],
        auth_command: tuple[str, ...],
        *,
        request_timeout: float = 30,
        api_key_file: Path | None = None,
        auth_marker_file: Path | None = None,
    ):
        self.name = name
        self.agent_server_url = agent_server_url.rstrip("/")
        self.acp_command = acp_command
        self.auth_command = auth_command
        self.request_timeout = request_timeout
        self.api_key_file = api_key_file
        self.auth_marker_file = auth_marker_file
        self._last_quota = QuotaState()

    @property
    def capabilities(self) -> ProviderCapabilities:
        if self.name == "antigravity":
            return ProviderCapabilities(
                supports_headless=True,
                supports_resume=True,
                supports_acp=True,
                supports_subscription_login=True,
                supports_review=True,
                autonomous_available=True,
                limitation=(
                    "Uses Symphony's small ACP bridge over the official agy --print interface. "
                    "Reloaded turns use agy's workspace-scoped --continue; the bridge does not expose a stable native "
                    "conversation ID for exact cross-workspace selection."
                ),
            )
        return ProviderCapabilities(True, True, True, True, True, True)

    def auth_status(self) -> AuthStatus:
        if not self.auth_command:
            return AuthStatus(False, False, "no authentication probe configured")
        if shutil.which(self.auth_command[0]) is None:
            return AuthStatus(False, False, f"command is not installed: {self.auth_command[0]}")
        if self.auth_marker_file is not None:
            if not self.auth_marker_file.is_file():
                return AuthStatus(
                    True,
                    False,
                    f"official CLI authentication has not been verified; run agentctl auth {self.name} as the agent user",
                )
            detail = self.auth_marker_file.read_text(errors="replace").strip()[-2000:]
            return AuthStatus(True, True, detail or "official CLI authentication was verified")
        process = subprocess.run(
            list(self.auth_command),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        detail = (process.stdout.strip() or process.stderr.strip() or f"exit {process.returncode}")[-2000:]
        return AuthStatus(True, process.returncode == 0, detail)

    def health(self) -> tuple[bool, str]:
        if not self.capabilities.autonomous_available:
            return False, self.capabilities.limitation
        try:
            self._request("GET", "/health")
            return True, "Agent Server health endpoint is available"
        except OpenHandsProviderError as exc:
            return False, f"Agent Server unavailable: {exc}"

    def quota_or_rate_limit_state(self) -> QuotaState:
        # Durable provider backoff is owned by the store. Consume this
        # transport-level observation once so a recovered provider is not held
        # indefinitely by stale in-memory state.
        state = self._last_quota
        self._last_quota = QuotaState()
        return state

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}))
        if self.api_key_file and self.api_key_file.is_file():
            key = self.api_key_file.read_text().strip()
            if key.startswith("LOCAL_BACKEND_API_KEY="):
                key = key.split("=", 1)[1]
            headers["X-Session-API-Key"] = key
        try:
            response = httpx.request(
                method,
                f"{self.agent_server_url}{path}",
                timeout=self.request_timeout,
                headers=headers,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise OpenHandsProviderError(f"Agent Server request failed: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text[-4000:]
            lowered = detail.lower()
            if response.status_code == 429 or any(word in lowered for word in ("rate limit", "quota", "usage limit")):
                retry_after = response.headers.get("retry-after")
                self._last_quota = QuotaState(
                    True, int(retry_after) if retry_after and retry_after.isdigit() else None, detail
                )
            raise OpenHandsProviderError(f"Agent Server HTTP {response.status_code}: {detail}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def start(self, workspace: Path, prompt: str, run_id: str, *, read_only: bool = False) -> ProviderRun:
        if not self.capabilities.autonomous_available:
            raise OpenHandsProviderError(self.capabilities.limitation)
        if not self.acp_command:
            raise OpenHandsProviderError(f"{self.name} has no ACP command configured")
        server_kind = {"claude": "claude-code", "codex": "codex"}.get(self.name, "custom")
        # OpenHands' built-in ACP defaults currently select bypassPermissions
        # for Claude and danger-full-access for Codex. Symphony deliberately
        # overrides both. The OpenHands ACP bridge auto-approves individual
        # permission requests, so these modes remain unattended while Codex
        # keeps its workspace-write sandbox and Claude avoids blanket bypass.
        if read_only:
            session_mode = {"claude": "plan", "codex": "read-only", "antigravity": "plan"}.get(self.name, "default")
        else:
            session_mode = {"claude": "acceptEdits", "codex": "agent"}.get(self.name, "default")
        payload = {
            "workspace": {"working_dir": str(workspace.resolve()), "kind": "LocalWorkspace"},
            "agent_settings": {
                "agent_kind": "acp",
                "acp_server": server_kind,
                "acp_command": list(self.acp_command),
                "acp_args": [],
                "acp_session_mode": session_mode,
                "acp_prompt_timeout": 600.0,
                "acp_isolate_data_dir": False,
            },
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
                "run": False,
            },
            "max_iterations": 500,
            "tags": {"runid": run_id.replace("-", "")[:32], "provider": self.name},
        }
        created = self._request("POST", "/api/conversations", json=payload)
        conversation_id = str(created.get("id") or created.get("conversation_id") or "")
        if not conversation_id:
            raise OpenHandsProviderError(f"conversation response omitted an ID: {created}")
        session_id = created.get("session_id") or created.get("agent_session_id")
        return ProviderRun(self.name, conversation_id, str(session_id) if session_id else None)

    def resume(self, run: ProviderRun, prompt: str | None = None) -> ProviderRun:
        if prompt:
            message = {"role": "user", "content": [{"type": "text", "text": prompt}], "run": True}
            self._request("POST", f"/api/conversations/{run.conversation_id}/events", json=message)
        else:
            self._request("POST", f"/api/conversations/{run.conversation_id}/run")
        return run

    @staticmethod
    def _execution_state(payload: dict[str, Any]) -> str:
        candidates = [
            payload.get("execution_status"),
            payload.get("status"),
            (payload.get("runtime_status") or {}).get("status")
            if isinstance(payload.get("runtime_status"), dict)
            else None,
        ]
        for candidate in candidates:
            if candidate:
                return str(candidate).lower().replace("_", "-")
        return "unknown"

    @staticmethod
    def _final_text(payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict):
            for key in ("text", "content", "final_response", "message"):
                value = payload.get(key)
                if isinstance(value, str):
                    return value
                if isinstance(value, list):
                    return "\n".join(
                        str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in value
                    )
        return json.dumps(payload, sort_keys=True)

    @staticmethod
    def _parse_result(text: str, run: ProviderRun) -> ProviderResult:
        marker_index = text.rfind(RESULT_MARKER)
        if marker_index >= 0:
            candidate = text[marker_index + len(RESULT_MARKER) :].strip().splitlines()[0]
            try:
                data = json.loads(candidate)
                outcome = ProviderOutcome(data.get("outcome", "completed"))
                return ProviderResult(
                    outcome,
                    str(data.get("summary") or "Agent finished."),
                    str(data.get("question_or_reason") or ""),
                    run.conversation_id,
                    run.session_id,
                    data,
                )
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        return ProviderResult(
            ProviderOutcome.COMPLETED,
            text[-4000:] or "Agent completed without a structured summary.",
            conversation_id=run.conversation_id,
            session_id=run.session_id,
        )

    def wait(self, run: ProviderRun, timeout_seconds: int) -> ProviderResult:
        deadline = time.monotonic() + timeout_seconds
        terminal = {"finished", "completed", "stopped", "paused", "error", "failed", "cancelled", "canceled"}
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self._request("GET", f"/api/conversations/{run.conversation_id}")
            state = self._execution_state(last)
            if state in terminal:
                if state in {"error", "failed"}:
                    return ProviderResult(
                        ProviderOutcome.FAILED,
                        "Agent Server run failed.",
                        json.dumps(last)[-4000:],
                        run.conversation_id,
                        run.session_id,
                    )
                if state in {"cancelled", "canceled"}:
                    return ProviderResult(
                        ProviderOutcome.CANCELED,
                        "Agent run was canceled.",
                        conversation_id=run.conversation_id,
                        session_id=run.session_id,
                    )
                final = self._request("GET", f"/api/conversations/{run.conversation_id}/agent_final_response")
                result = self._parse_result(self._final_text(final), run)
                return result
            time.sleep(2)
        self.cancel(run)
        return ProviderResult(
            ProviderOutcome.FAILED,
            "Agent run exceeded its configured timeout.",
            f"Timed out after {timeout_seconds} seconds; last state: {self._execution_state(last)}",
            run.conversation_id,
            run.session_id,
        )

    def cancel(self, run: ProviderRun) -> None:
        self._request("POST", f"/api/conversations/{run.conversation_id}/interrupt")
