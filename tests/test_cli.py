from __future__ import annotations

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
