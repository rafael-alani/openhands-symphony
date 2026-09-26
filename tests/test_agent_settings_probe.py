import importlib.util
import json
import shlex
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "agent_settings_probe", Path(__file__).resolve().parents[1] / "scripts/probe_agent_settings.py",
)
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


@pytest.fixture
def acp_command(tmp_path):
    transcript = tmp_path / "requests.jsonl"
    fake = tmp_path / "adapter.py"
    fake.write_text('''import json, os, sys
config = json.loads(os.environ["CODEX_CONFIG"])
for line in sys.stdin:
    request = json.loads(line)
    with open(sys.argv[1], "a") as output:
        output.write(json.dumps({"method": request["method"], "config": config}) + "\\n")
    if request["method"] == "initialize":
        result = {"protocolVersion": 1}
    elif request["method"] == "session/new":
        result = {"sessionId": "probe", "models": {"currentModelId": "test-" + config["model_reasoning_effort"]},
                  "configOptions": [{"id": "fast-mode", "currentValue": "on" if config["service_tier"] else "off"}]}
    else:
        raise RuntimeError("Probe must never send a model prompt")
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
''')
    wrapper = tmp_path / "installed-wrapper.sh"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(fake), str(transcript)]) + "\n")
    wrapper.chmod(0o755)
    return wrapper, transcript


def test_settings_probe_launches_wrapper_directly_and_checks_settings_without_model_turns(tmp_path, acp_command, capsys):
    wrapper, transcript = acp_command
    PROBE.probe_settings([str(wrapper)], tmp_path)
    requests = [json.loads(line) for line in transcript.read_text().splitlines()]
    assert [item["method"] for item in requests] == ["initialize", "session/new"] * 2
    assert [item["config"] for item in requests[::2]] == [
        {"model_reasoning_effort": "xhigh", "service_tier": None},
        {"model_reasoning_effort": "low", "service_tier": "fast"},
    ]
    results = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(item["model"], item["fast_mode"]) for item in results] == [("test-xhigh", "off"), ("test-low", "on")]


def test_settings_probe_fails_on_missing_execute_bit_instead_of_bypassing_wrapper(tmp_path, acp_command):
    wrapper, transcript = acp_command
    wrapper.chmod(0o644)
    with pytest.raises(PermissionError):
        PROBE.probe_settings([str(wrapper)], tmp_path)
    assert not transcript.exists()


@pytest.mark.parametrize("output, message", [("read line; echo invalid-json", "invalid JSON"), ("read line; exit 1", "exited")])
def test_settings_probe_reports_broken_adapter_before_timeout(tmp_path, output, message):
    wrapper = tmp_path / "broken-wrapper.sh"
    wrapper.write_text(f"#!/bin/sh\n{output}\n")
    wrapper.chmod(0o755)
    with pytest.raises(RuntimeError, match=message):
        PROBE.probe_settings([str(wrapper)], tmp_path)


@pytest.mark.parametrize("kind", ["reasoning", "speed"])
def test_settings_probe_rejects_unapplied_settings(tmp_path, acp_command, kind):
    wrapper, _ = acp_command
    fake = tmp_path / "adapter.py"
    text = fake.read_text()
    if kind == "reasoning":
        text = text.replace('"test-" + config["model_reasoning_effort"]', '"test-medium"')
    else:
        text = text.replace('"on" if config["service_tier"] else "off"', '"on"')
    fake.write_text(text)
    with pytest.raises(RuntimeError, match=f"requested {kind}"):
        PROBE.probe_settings([str(wrapper)], tmp_path)


def test_installed_command_cli_does_not_use_the_copied_adapter(tmp_path, acp_command, monkeypatch, capsys):
    wrapper, transcript = acp_command
    monkeypatch.setattr(PROBE.os, "geteuid", lambda: 995)
    monkeypatch.setattr(sys, "argv", ["probe_agent_settings.py", "--command", str(wrapper)])
    PROBE.main()
    assert len(transcript.read_text().splitlines()) == 4
    assert len(capsys.readouterr().out.splitlines()) == 2
