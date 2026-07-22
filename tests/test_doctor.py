from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from symphony import doctor
from symphony.doctor import _credential_exposure, _empty_setup_behavior, _service_failure_detail, _validator_boundary


def test_inaccessible_worker_credential_is_treated_as_account_isolation(monkeypatch) -> None:
    def deny_stat(self: Path, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "stat", deny_stat)

    exposed, detail = _credential_exposure(Path("/var/lib/openhands-agent/.config/gh/hosts.yml"))

    assert not exposed
    assert "expected account isolation" in detail


def test_inactive_service_detail_includes_recent_journal(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_run", lambda _command: (0, "chrome: user namespaces are restricted"))

    detail = _service_failure_detail("openhands-browser.service", "activating")

    assert "state=activating" in detail
    assert "user namespaces are restricted" in detail


def test_active_service_detail_does_not_read_journal(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_run", lambda _command: (_ for _ in ()).throw(AssertionError("unexpected call")))

    assert _service_failure_detail("openhands-browser.service", "active") == "active"


def test_validator_boundary_runs_the_exact_production_wrapper(monkeypatch) -> None:
    commands: list[list[str]] = []

    def successful_probe(command: list[str]):
        commands.append(command)
        return (
            0,
            "\n".join(
                (
                    "user=openhands-validator",
                    "home=/var/lib/openhands-validator",
                    "path=/opt/browser-use/bin:/usr/local/bin:/usr/bin:/bin",
                    "ci=true",
                    "umask=0007",
                    "HOME=/var/lib/openhands-validator",
                    "PATH=/opt/browser-use/bin:/usr/local/bin:/usr/bin:/bin",
                    "CI=true",
                )
            ),
        )

    monkeypatch.setattr(doctor, "_run", successful_probe)
    config = SimpleNamespace(service=SimpleNamespace(validation_user="openhands-validator"))

    check = _validator_boundary(config)

    assert check.ok
    assert commands[0][:7] == ["sudo", "-n", "-H", "-u", "openhands-validator", "--", "env"]
    assert "/usr/bin/setpriv" not in commands[0]
    assert 'umask 0007; exec "$@"' in commands[0]


def test_validator_boundary_rejects_wrong_umask(monkeypatch) -> None:
    monkeypatch.setattr(
        doctor,
        "_run",
        lambda _command: (
            0,
            "\n".join(
                (
                    "user=openhands-validator",
                    "home=/var/lib/openhands-validator",
                    "path=/opt/browser-use/bin:/usr/local/bin:/usr/bin:/bin",
                    "ci=true",
                    "umask=0022",
                )
            ),
        ),
    )
    config = SimpleNamespace(service=SimpleNamespace(validation_user="openhands-validator"))

    assert not _validator_boundary(config).ok


def test_inaccessible_service_journal_gives_privileged_diagnostic_command(monkeypatch) -> None:
    monkeypatch.setattr(
        doctor,
        "_run",
        lambda _command: (1, "No journal files were opened due to insufficient permissions."),
    )

    detail = _service_failure_detail("openhands-browser.service", "activating")

    assert "sudo journalctl -u openhands-browser.service" in detail


def test_doctor_probes_empty_setup_through_workspace_manager(tmp_path) -> None:
    calls: list[tuple[Path, str, str]] = []

    class Workspaces:
        @staticmethod
        def run_setup(worktree: Path, setup_script: str, validation_user: str):
            calls.append((worktree, setup_script, validation_user))
            return None

    config = SimpleNamespace(
        service=SimpleNamespace(workspace_dir=tmp_path, validation_user="openhands-validator")
    )
    coordinator = SimpleNamespace(workspaces=Workspaces())

    check = _empty_setup_behavior(config, coordinator)

    assert check.ok
    assert calls == [(tmp_path, "", "openhands-validator")]
