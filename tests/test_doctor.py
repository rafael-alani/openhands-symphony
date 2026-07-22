from __future__ import annotations

from pathlib import Path

from symphony import doctor
from symphony.doctor import _credential_exposure, _service_failure_detail


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
