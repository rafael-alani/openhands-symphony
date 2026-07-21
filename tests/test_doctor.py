from __future__ import annotations

from pathlib import Path

from symphony.doctor import _credential_exposure


def test_inaccessible_worker_credential_is_treated_as_account_isolation(monkeypatch) -> None:
    def deny_stat(self: Path, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "stat", deny_stat)

    exposed, detail = _credential_exposure(Path("/var/lib/openhands-agent/.config/gh/hosts.yml"))

    assert not exposed
    assert "expected account isolation" in detail
