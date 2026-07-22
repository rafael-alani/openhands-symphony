from __future__ import annotations

from types import SimpleNamespace

import pytest

from symphony import cli
from symphony.cli import _antigravity_cpu_error, _authentication_environment, _job_status_line


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
