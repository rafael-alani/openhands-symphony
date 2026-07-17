from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace


def load_bridge_module():
    path = Path(__file__).parents[1] / "scripts" / "antigravity_acp_bridge.py"
    spec = importlib.util.spec_from_file_location("symphony_antigravity_bridge", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_antigravity_bridge_uses_official_print_mode_and_sanitized_environment(tmp_path, monkeypatch):
    module = load_bridge_module()
    captured: dict[str, object] = {}

    class Process:
        returncode = 0
        pid = 123

        async def communicate(self):
            return b'OPENHANDS_SYMPHONY_RESULT={"outcome":"completed","summary":"ok"}', None

    async def create_process(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Process()

    class Connection:
        def __init__(self):
            self.messages = []

        async def session_update(self, session_id, message):
            self.messages.append((session_id, message))

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setenv("GH_TOKEN", "must-not-pass")
    monkeypatch.setenv("GH_CONFIG_DIR", "/must-not-pass")
    monkeypatch.setenv("LOCAL_BACKEND_API_KEY", "must-not-pass")
    monkeypatch.setenv("BROWSER_USE_API_KEY", "must-not-pass")
    bridge = module.AntigravityBridge()
    connection = Connection()
    bridge.on_connect(connection)

    async def exercise():
        session = await bridge.new_session(str(tmp_path))
        assert session.modes.current_mode_id == "default"
        response = await bridge.prompt(session.session_id, [SimpleNamespace(text="implement safely")])
        return response

    response = asyncio.run(exercise())
    args = captured["args"]
    environment = captured["kwargs"]["env"]
    assert args[:6] == (
        "/usr/local/bin/agy",
        "--sandbox",
        "--mode",
        "accept-edits",
        "--print-timeout",
        "120m",
    )
    assert args[-2:] == ("--print", "implement safely")
    assert captured["kwargs"]["cwd"] == tmp_path.resolve()
    assert environment["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert "GH_TOKEN" not in environment
    assert "GH_CONFIG_DIR" not in environment
    assert "LOCAL_BACKEND_API_KEY" not in environment
    assert "BROWSER_USE_API_KEY" not in environment
    assert response.stop_reason == "end_turn"
    assert "OPENHANDS_SYMPHONY_RESULT=" in connection.messages[0][1].content.text


def test_antigravity_bridge_maps_review_mode_to_official_plan_mode(tmp_path, monkeypatch):
    module = load_bridge_module()
    captured: dict[str, object] = {}

    class Process:
        returncode = 0
        pid = 321

        async def communicate(self):
            return b"review complete", None

    async def create_process(*args, **kwargs):
        captured["args"] = args
        return Process()

    class Connection:
        async def session_update(self, session_id, message):
            return None

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create_process)
    bridge = module.AntigravityBridge()
    bridge.on_connect(Connection())

    async def exercise():
        session = await bridge.new_session(str(tmp_path))
        await bridge.set_session_mode(session.session_id, "plan")
        await bridge.prompt(session.session_id, [SimpleNamespace(text="review")])

    asyncio.run(exercise())
    args = captured["args"]
    assert args[args.index("--mode") + 1] == "plan"


def test_antigravity_bridge_classifies_nonzero_quota_failure(tmp_path, monkeypatch):
    module = load_bridge_module()

    class Process:
        returncode = 9
        pid = 456

        async def communicate(self):
            return b"Provider usage quota exhausted", None

    async def create_process(*args, **kwargs):
        return Process()

    class Connection:
        message = None

        async def session_update(self, session_id, message):
            self.message = message

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create_process)
    bridge = module.AntigravityBridge()
    connection = Connection()
    bridge.on_connect(connection)

    async def exercise():
        session = await bridge.new_session(str(tmp_path))
        await bridge.prompt(session.session_id, [SimpleNamespace(text="work")])

    asyncio.run(exercise())
    assert '"failure_kind":"quota"' in connection.message.content.text
    assert '"exit_code":9' in connection.message.content.text


def test_antigravity_bridge_uses_native_continue_for_subsequent_turn(tmp_path, monkeypatch):
    module = load_bridge_module()
    calls: list[tuple[object, ...]] = []

    class Process:
        returncode = 0
        pid = 789

        async def communicate(self):
            return b"ok", None

    async def create_process(*args, **kwargs):
        calls.append(args)
        return Process()

    class Connection:
        async def session_update(self, session_id, message):
            return None

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create_process)
    bridge = module.AntigravityBridge()
    bridge.on_connect(Connection())

    async def exercise():
        session = await bridge.new_session(str(tmp_path))
        await bridge.prompt(session.session_id, [SimpleNamespace(text="first")])
        await bridge.prompt(session.session_id, [SimpleNamespace(text="second")])

    asyncio.run(exercise())

    assert "--continue" not in calls[0]
    assert calls[1][1] == "--continue"
