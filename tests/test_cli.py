from __future__ import annotations

import pytest

from symphony import cli
from symphony.cli import _antigravity_cpu_error, _authentication_environment


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
