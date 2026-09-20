from __future__ import annotations

from types import SimpleNamespace

import pytest

from symphony import cli
from symphony.cli import (
    _antigravity_cpu_error,
    _authentication_environment,
    _idea_status_line,
    _job_needs_explicit_retry,
    _job_status_line,
)


@pytest.mark.parametrize("missing", [False, True])
def test_vault_check_validates_linked_files_without_config_or_mutation(tmp_path, monkeypatch, capsys, missing):
    note = tmp_path / "Project.md"
    note.write_text("---\nsymphony: idea\n---\nA test app.\n- [ ] [[Feature]]\n")
    if not missing:
        (tmp_path / "Feature.md").write_text("Build the main screen.")
    original = note.read_bytes()
    monkeypatch.setattr("sys.argv", ["agentctl", "vault-check", str(note)])
    monkeypatch.setattr(cli, "build_coordinator", lambda *a: pytest.fail("read-only check must not build a coordinator"))
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == (2 if missing else 0)
    assert note.read_bytes() == original
    output = capsys.readouterr()
    assert "missing Markdown" in output.err if missing else "checklist files: 1" in output.out


def test_antigravity_cpu_preflight_rejects_x86_vm_without_pclmulqdq() -> None:
    error = _antigravity_cpu_error(machine="x86_64", cpuinfo="flags : sse4_2 aes")

    assert error is not None
    assert "does not expose PCLMULQDQ" in error


def test_antigravity_cpu_preflight_accepts_pclmulqdq() -> None:
    assert _antigravity_cpu_error(machine="x86_64", cpuinfo="flags : sse4_2 pclmulqdq aes") is None


def test_antigravity_auth_forces_remote_oauth_after_sudo_strips_ssh(monkeypatch) -> None:
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_CLIENT", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)

    environment = _authentication_environment("antigravity")

    assert environment["SSH_CONNECTION"]
    assert environment["SSH_CLIENT"]
    assert environment["SSH_TTY"] == "/dev/tty"
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/openhands-agent/bus"
    assert environment["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


@pytest.mark.parametrize(
    ("provider", "expected_command"),
    [
        ("claude", ["/opt/provider-clis/node_modules/.bin/claude", "auth", "status"]),
        ("codex", ["/opt/provider-clis/node_modules/.bin/codex", "login", "status"]),
    ],
)
def test_auth_skips_oauth_when_provider_is_already_authenticated(
    provider, expected_command, tmp_path, monkeypatch, capsys
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setenv("SYMPHONY_AUTH_MARKER_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "_run_interactive", lambda command, **_kwargs: commands.append(command) or 0)

    assert cli._authenticate_provider(provider) == 0

    assert commands == [expected_command]
    assert (tmp_path / f"{provider}.json").is_file()
    assert f"{provider} is already authenticated; no login needed" in capsys.readouterr().out


def test_github_auth_skips_oauth_but_keeps_git_credential_setup(monkeypatch, capsys) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(cli, "_run_interactive", lambda command, **_kwargs: commands.append(command) or 0)

    assert cli._authenticate_provider("github") == 0

    assert commands == [
        ["gh", "auth", "status", "--hostname", "github.com"],
        ["gh", "auth", "setup-git"],
    ]
    assert "github is already authenticated; no login needed" in capsys.readouterr().out


def test_github_auth_runs_oauth_only_after_status_probe_fails(monkeypatch) -> None:
    commands: list[list[str]] = []
    statuses = iter([1, 0, 0, 0])
    monkeypatch.setattr(
        cli,
        "_run_interactive",
        lambda command, **_kwargs: commands.append(command) or next(statuses),
    )

    assert cli._authenticate_provider("github") == 0

    assert commands == [
        ["gh", "auth", "status", "--hostname", "github.com"],
        ["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web"],
        ["gh", "auth", "status", "--hostname", "github.com"],
        ["gh", "auth", "setup-git"],
    ]


def test_antigravity_auth_skips_oauth_when_status_succeeds(tmp_path, monkeypatch, capsys) -> None:
    commands: list[list[str]] = []
    monkeypatch.setenv("SYMPHONY_AUTH_MARKER_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "_antigravity_cpu_error", lambda: None)
    monkeypatch.setattr(cli, "_run_interactive", lambda command, **_kwargs: commands.append(command) or 0)

    assert cli._authenticate_provider("antigravity") == 0

    assert commands == [["agy", "models"]]
    assert (tmp_path / "antigravity.json").is_file()
    assert "antigravity is already authenticated; no login needed" in capsys.readouterr().out


def test_start_skips_systemctl_start_when_target_is_already_active(monkeypatch, capsys) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(cli, "_run_interactive", lambda command, **_kwargs: commands.append(command) or 0)

    assert cli._systemctl("start") == 0

    assert commands == [
        ["systemctl", "is-active", "--quiet", "openhands-symphony.target"],
        ["systemctl", "is-active", "--quiet", "openhands-symphony-firewall.service"],
        ["systemctl", "is-active", "--quiet", "openhands-agent-dbus.service"],
        ["systemctl", "is-active", "--quiet", "openhands-agent-keyring.service"],
        ["systemctl", "is-active", "--quiet", "openhands-browser.service"],
        ["systemctl", "is-active", "--quiet", "openhands-canvas.service"],
        ["systemctl", "is-active", "--quiet", "openhands-idea-preview.service"],
        ["systemctl", "is-active", "--quiet", "openhands-symphony.service"],
        ["systemctl", "is-active", "--quiet", "openhands-symphony-reconcile.timer"],
    ]
    assert "already active; no start needed" in capsys.readouterr().out


def test_start_runs_systemctl_start_when_target_is_inactive(monkeypatch) -> None:
    commands: list[list[str]] = []
    statuses = iter([3, 0])
    monkeypatch.setattr(
        cli,
        "_run_interactive",
        lambda command, **_kwargs: commands.append(command) or next(statuses),
    )

    assert cli._systemctl("start") == 0

    assert commands == [
        ["systemctl", "is-active", "--quiet", "openhands-symphony.target"],
        ["systemctl", "start", "openhands-symphony.target"],
    ]


def test_start_restarts_active_target_when_a_required_unit_is_unhealthy(monkeypatch, capsys) -> None:
    commands: list[list[str]] = []
    statuses = iter([0, 0, 0, 0, 3, 0])
    monkeypatch.setattr(
        cli,
        "_run_interactive",
        lambda command, **_kwargs: commands.append(command) or next(statuses),
    )

    assert cli._systemctl("start") == 0

    assert commands[-2:] == [
        ["systemctl", "is-active", "--quiet", "openhands-browser.service"],
        ["systemctl", "restart", "openhands-symphony.target"],
    ]
    assert "required unit is not; restarting" in capsys.readouterr().out


def test_graduation_stop_explicitly_quiesces_every_ideas_capable_unit(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(cli, "_run_interactive", lambda command, **_kwargs: commands.append(command) or 0)

    assert cli._stop_for_graduation() == 0

    assert commands == [
        [
            "systemctl",
            "stop",
            "openhands-symphony.target",
            "openhands-symphony.service",
            "openhands-symphony-reconcile.timer",
            "openhands-symphony-reconcile.service",
            "openhands-idea-preview.service",
        ]
    ]


@pytest.mark.parametrize("was_active", [True, False])
def test_graduate_holds_global_lock_across_stop_apply_and_restart(
    tmp_path, monkeypatch, was_active
) -> None:
    events: list[str] = []
    config = SimpleNamespace(
        ideas=SimpleNamespace(repositories=("solo/idea",), private_only=True),
        vault=SimpleNamespace(enabled=False),
        service=SimpleNamespace(state_dir=tmp_path / "state"),
    )

    class FakeStore:
        def __init__(self, _path):
            pass

        def acquire_operation_lock(self, name, owner, seconds):
            assert name == "graduate"
            assert owner.startswith("graduate-cli-")
            assert seconds == 7200
            events.append("lock")
            return True

        def release_operation_lock(self, name, owner):
            assert name == "graduate"
            assert owner.startswith("graduate-cli-")
            events.append("unlock")

    class FakeGraduator:
        def __init__(self, received_config, config_path, backend, *, store):
            assert received_config is config
            assert config_path == (tmp_path / "config.toml").resolve()
            assert backend == "backend"
            assert isinstance(store, FakeStore)

        def apply(self, repository, approval_id, *, operation_lock_held=False):
            assert (repository, approval_id) == ("solo/idea", "approved-plan")
            assert operation_lock_held
            events.append("apply")
            return SimpleNamespace(archive_commit="archive", retired_runs=(), issue_urls=(), warnings=())

    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, "GhGraduationBackend", lambda *_args, **_kwargs: "backend")
    monkeypatch.setattr(cli, "Store", FakeStore)
    monkeypatch.setattr(cli, "Graduator", FakeGraduator)
    monkeypatch.setattr(cli, "_target_is_active", lambda: events.append("probe") or was_active)
    monkeypatch.setattr(cli, "_stop_for_graduation", lambda: events.append("stop") or 0)
    monkeypatch.setattr(cli, "_systemctl", lambda action: events.append(action) or 0)

    assert cli._graduate(str(tmp_path / "config.toml"), "solo/idea", "approved-plan") == 0

    expected = ["lock", "probe", "stop", "apply"]
    expected.append("start")
    expected.append("unlock")
    assert events == expected


def test_graduate_restarts_and_unlocks_when_apply_fails(tmp_path, monkeypatch) -> None:
    events: list[str] = []
    config = SimpleNamespace(
        ideas=SimpleNamespace(repositories=("solo/idea",), private_only=True),
        vault=SimpleNamespace(enabled=False),
        service=SimpleNamespace(state_dir=tmp_path / "state"),
    )

    class FakeStore:
        def __init__(self, _path):
            pass

        def acquire_operation_lock(self, *_args, **_kwargs):
            events.append("lock")
            return True

        def release_operation_lock(self, *_args, **_kwargs):
            events.append("unlock")

    class FakeGraduator:
        def __init__(self, *_args, **_kwargs):
            pass

        def apply(self, *_args, **_kwargs):
            events.append("apply")
            raise RuntimeError("archive failed")

    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, "GhGraduationBackend", lambda *_args, **_kwargs: "backend")
    monkeypatch.setattr(cli, "Store", FakeStore)
    monkeypatch.setattr(cli, "Graduator", FakeGraduator)
    monkeypatch.setattr(cli, "_target_is_active", lambda: True)
    monkeypatch.setattr(cli, "_stop_for_graduation", lambda: events.append("stop") or 0)
    monkeypatch.setattr(cli, "_systemctl", lambda action: events.append(action) or 0)

    with pytest.raises(RuntimeError, match="archive failed"):
        cli._graduate(str(tmp_path / "config.toml"), "solo/idea", "approved-plan")

    assert events == ["lock", "stop", "apply", "start", "unlock"]


def test_auth_runs_oauth_only_after_status_probe_fails(tmp_path, monkeypatch) -> None:
    commands: list[list[str]] = []
    statuses = iter([1, 0, 0])
    monkeypatch.setenv("SYMPHONY_AUTH_MARKER_DIR", str(tmp_path))
    monkeypatch.setattr(
        cli,
        "_run_interactive",
        lambda command, **_kwargs: commands.append(command) or next(statuses),
    )

    assert cli._authenticate_provider("codex") == 0

    assert commands == [
        ["/opt/provider-clis/node_modules/.bin/codex", "login", "status"],
        ["/opt/provider-clis/node_modules/.bin/codex", "login", "--device-auth"],
        ["/opt/provider-clis/node_modules/.bin/codex", "login", "status"],
    ]


def test_job_status_exposes_phase_conversation_and_report(tmp_path) -> None:
    report = tmp_path / "run-123" / "run.md"
    report.parent.mkdir()
    report.write_text("# report\n")
    job = SimpleNamespace(
        repository="solo/project",
        issue_number=21,
        state="running",
        implementation_provider="codex",
        attempt=2,
        phase="implementation",
        id="run-123",
        conversation_id="conversation-456",
        review_conversation_id=None,
        pr_url=None,
    )

    line = _job_status_line(job, tmp_path)

    assert "phase=implementation" in line
    assert "conversation=conversation-456" in line
    assert "review_conversation=-" in line
    assert f"report={report}" in line


def test_queued_preconversation_attempts_are_explicitly_retried_for_legacy_recovery() -> None:
    job = SimpleNamespace(
        state=SimpleNamespace(value="queued"),
        review_required=False,
        phase="explicit-requeue",
        attempt=3,
        conversation_id=None,
        pr_number=None,
    )

    assert _job_needs_explicit_retry(job)


def test_idea_status_exposes_hash_state_publication_and_question(tmp_path) -> None:
    report = tmp_path / "idea-run" / "run.md"
    report.parent.mkdir()
    report.write_text("# idea report\n")
    project = SimpleNamespace(
        repository="solo/idea",
        latest_observed_spec_hash="new-hash",
        latest_completed_spec_hash="old-hash",
        preview_state="healthy",
        last_good_preview_commit="preview-commit",
    )
    run = SimpleNamespace(
        id="idea-run",
        state=SimpleNamespace(value="question"),
        published_commit="commit-1",
        question="Pick A or B?",
    )

    line = _idea_status_line(project, run, tmp_path)

    assert "latest=new-hash" in line
    assert "completed=old-hash" in line
    assert "publication=commit-1" in line
    assert "preview=healthy" in line
    assert "last_good=preview-commit" in line
    assert "question=Pick A or B?" in line
    assert f"report={report}" in line
