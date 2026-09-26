from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ExistingWorkspace, FakeGitHub, create_worktree, issue, make_config

from symphony.agent_settings import AgentSettings
from symphony.config import RepositoryConfig, load_config
from symphony.coordinator import Coordinator
from symphony.ideas_contract import git_blob_hash, parse_runtime, spec_settings
from symphony.intake import route
from symphony.models import JobState
from symphony.providers.fake import FakeProvider
from symphony.providers.openhands import OpenHandsACPProvider
from symphony.store import Store
from symphony.vault import VaultError, read_note
from symphony.vault_project import compile_project

ROOT = Path(__file__).resolve().parents[1]


def test_old_configuration_inherits_requested_defaults_and_overrides_independently(tmp_path):
    config = make_config(tmp_path)
    assert config.agent_settings("codex") == AgentSettings("xhigh", "normal")
    config = replace(config, repositories={"solo/project": RepositoryConfig(speed="fast")})
    assert config.agent_settings("codex", "solo/project") == AgentSettings("xhigh", "fast")
    assert config.agent_settings("codex", "solo/project", AgentSettings("low", "normal")) == AgentSettings("low", "normal")


@pytest.mark.parametrize("setting", ['reasoning_effort = "maximum"', 'speed = "warp"', 'speed = true'])
def test_invalid_configuration_fails_at_load(tmp_path, setting):
    path = tmp_path / "config.toml"
    text = (ROOT / "examples/config.toml").read_text()
    text = text.replace('reasoning_effort = "xhigh" # low, medium, high, xhigh', setting)
    if setting.startswith("speed"):
        text = text.replace('speed = "normal" # normal or fast (increased subscription usage)\n', '')
    path.write_text(text)
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.parametrize("labels", [
    ("reasoning:low", "reasoning:high"), ("speed:normal", "speed:fast"),
    ("reasoning:maximum",), ("speed:warp",),
])
def test_invalid_or_conflicting_labels_are_ineligible(labels):
    assert not route(replace(issue(), labels=("agent:ready", "agent:codex", *labels))).eligible


def test_non_codex_choices_fail_clearly():
    decision = route(replace(issue(), labels=("agent:ready", "agent:claude", "speed:fast")))
    assert not decision.eligible
    assert "only for Codex" in decision.reason


def test_settings_label_changes_are_guarded_but_state_labels_are_not_spec_changes():
    original = issue()
    assert original.content_hash() == replace(original, labels=(*original.labels, "agent:running")).content_hash()
    assert original.content_hash() != replace(original, labels=(*original.labels, "speed:fast")).content_hash()


@pytest.mark.parametrize("overrides, expected", [
    (AgentSettings(), AgentSettings("xhigh", "normal")),
    (AgentSettings("low", "fast"), AgentSettings("low", "fast")),
    (AgentSettings(speed="fast"), AgentSettings("xhigh", "fast")),
])
def test_launch_sends_per_conversation_config_without_mutating_shared_defaults(tmp_path, monkeypatch, overrides, expected):
    provider = OpenHandsACPProvider("codex", "http://test", ("wrapper",), ("auth",))
    payloads = []

    def request(method, path, **kwargs):
        payloads.append(kwargs["json"])
        return {"id": str(len(payloads))}

    monkeypatch.setattr(provider, "_request", request)
    provider.start(tmp_path, "test", "run", settings=overrides)
    provider.start(tmp_path, "review", "review", read_only=True)
    assert payloads[0]["agent_settings"]["acp_command"] == ["wrapper"]
    config = json.loads(payloads[0]["agent_settings"]["acp_env"]["CODEX_CONFIG"])
    assert config == {"model_reasoning_effort": expected.reasoning_effort,
                      "service_tier": "fast" if expected.speed == "fast" else None}
    assert payloads[0]["tags"]["speed"] == expected.speed
    assert payloads[1]["tags"]["speed"] == "normal"
    assert payloads[1]["tags"]["reasoning_effort"] == "xhigh"
    assert payloads[1]["agent_settings"]["acp_session_mode"] == "read-only"


@pytest.mark.parametrize("checklist", [False, True])
def test_note_choices_survive_compilation_and_change_spec_identity(tmp_path, checklist):
    path = tmp_path / "Project.md"
    body = "## Build\nMake a counter.\n"
    if checklist:
        (tmp_path / "Counter.md").write_text("Implement add and reset.")
        body += "- [ ] [[Counter]]\n"
    path.write_text("---\nsymphony: idea\n---\n" + body)
    old = compile_project(read_note(path), "solo/project", tmp_path).spec
    path.write_text("---\nsymphony: idea\nreasoning_effort: high\nspeed: fast\n---\n" + body)
    spec = compile_project(read_note(path), "solo/project", tmp_path).spec
    assert spec_settings(spec, "solo/project") == AgentSettings("high", "fast")
    assert git_blob_hash(old) != git_blob_hash(spec)


def test_note_invalid_speed_is_not_silently_ignored(tmp_path):
    path = tmp_path / "Project.md"
    path.write_text("---\nsymphony: idea\nspeed: rapid\n---\nBuild it.")
    with pytest.raises(VaultError, match="speed"):
        read_note(path)


def test_issue_selection_reaches_provider_after_store_restart(tmp_path):
    snapshot = replace(issue(), labels=("agent:ready", "agent:codex", "reasoning:low", "speed:fast"))
    config = make_config(tmp_path)
    db = config.service.state_dir / "state.db"
    store = Store(db)
    github = FakeGitHub([snapshot])
    provider = FakeProvider("codex", write_files={"implemented.txt": "ok\n"})
    coordinator = Coordinator(config, store, github, {"codex": provider})
    job, _ = coordinator.enqueue(snapshot)
    restarted = Store(db)
    coordinator = Coordinator(config, restarted, github, {"codex": provider})
    coordinator.workspaces = ExistingWorkspace(create_worktree(tmp_path, job.branch))
    claimed = restarted.claim_next(owner="settings-test", global_limit=2, provider_limits={"codex": 1}, lease_seconds=180)
    assert coordinator.run_claimed(claimed).state == JobState.PR_OPEN
    assert provider.settings == [AgentSettings("low", "fast")]


def test_idea_spec_overrides_runtime_settings_during_execution(tmp_path):
    from test_ideas import RUNTIME, SPEC, _claim, _coordinator, _edit_remote, _idea_remote

    remote, _ = _idea_remote(tmp_path)
    _edit_remote(tmp_path, remote, ".symphony/idea.toml", b'speed = "fast"\n' + RUNTIME, "fast runtime")
    spec = SPEC.replace(b"symphony: idea\n", b"symphony: idea\nreasoning_effort: high\nspeed: normal\n")
    _edit_remote(tmp_path, remote, "idea/SPEC.md", spec, "task settings")
    provider = FakeProvider("codex", write_files={"app.py": "print('hello')\n"})
    config, store, coordinator, _ = _coordinator(tmp_path, provider, remote)
    coordinator.observe_repository("solo/idea")
    coordinator.run_claimed(_claim(store, config))
    assert provider.settings == [AgentSettings("high", "normal")]
    assert parse_runtime(b'speed = "fast"\n' + RUNTIME).settings.speed == "fast"


def test_pinned_acp_alias_patch_is_idempotent_and_fails_before_partial_write(tmp_path):
    spec = importlib.util.spec_from_file_location("patch_codex_acp", ROOT / "scripts/patch_codex_acp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    package = tmp_path / "acp"
    (package / "dist").mkdir(parents=True)
    (package / "package.json").write_text('{"version":"1.1.4"}')
    target = package / "dist/index.js"
    original = "\n".join((old + "\n") * count for old, _, count in module.PATCHES)
    target.write_text(original)
    module.patch(package)
    patched = target.read_text()
    module.patch(package)
    assert target.read_text() == patched
    target.write_text(original.replace(module.PATCHES[1][0], "changed upstream"))
    broken = target.read_text()
    with pytest.raises(ValueError, match="Unexpected"):
        module.patch(package)
    assert target.read_text() == broken

