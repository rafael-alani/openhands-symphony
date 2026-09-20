from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _installer(monkeypatch, root, *, enabled=True):
    path = Path(__file__).resolve().parents[1] / "scripts/configure_vault.py"
    spec = importlib.util.spec_from_file_location("vault_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = SimpleNamespace(vault=SimpleNamespace(enabled=enabled, path=root, projects_dir="Projects"))
    monkeypatch.setattr(module, "load_config", lambda _: config)
    monkeypatch.setattr(module.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=123, pw_gid=456, pw_name="openhands-symphony"))
    monkeypatch.setattr(module.grp, "getgrnam", lambda _: SimpleNamespace(gr_gid=789, gr_name="symphony-vault"))
    commands, ownership = [], []
    monkeypatch.setattr(module.subprocess, "run", lambda command, **kwargs: commands.append(command))
    monkeypatch.setattr(module.os, "chown", lambda *args: ownership.append(args))
    return module, commands, ownership


def test_new_vault_shares_only_with_transport_and_hides_notes_from_workers(tmp_path, monkeypatch):
    root = tmp_path / "notes with spaces %"
    module, commands, ownership = _installer(monkeypatch, root)
    units = tmp_path / "units"
    module.install(tmp_path / "config.toml", units)

    assert commands == [["usermod", "-aG", "symphony-vault", "openhands-symphony"]]
    assert ownership == [(p, 123, 789) for p in (root, root / "Projects", root / "_symphony")]
    paths = (root, root / "Projects", root / "_symphony")
    assert all(p.stat().st_mode & 0o777 == 0o770 for p in paths)
    if sys.platform == "linux":
        assert all(p.stat().st_mode & 0o2000 for p in paths)
    for unit in ("openhands-symphony", "openhands-symphony-reconcile"):
        text = (units / f"{unit}.service.d/vault.conf").read_text()
        assert 'ReadWritePaths="' in text
        assert "SupplementaryGroups=symphony-vault" in text
        assert "spaces %%" in text
    for unit in ("openhands-canvas", "openhands-browser", "openhands-idea-preview"):
        text = (units / f"{unit}.service.d/vault.conf").read_text()
        assert 'InaccessiblePaths=-"' in text
        assert "ReadWritePaths" not in text


def test_existing_vault_permissions_survive_an_installer_rerun(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    for p in (root, root / "Projects", root / "_symphony"):
        p.mkdir(mode=0o750)
    note = root / "Projects" / "Original.md"
    note.write_text("Keep my original prose.\n")
    module, _, ownership = _installer(monkeypatch, root)
    module.install(tmp_path / "config.toml", tmp_path / "units")

    assert ownership == []
    assert root.stat().st_mode & 0o777 == 0o750
    assert note.read_text() == "Keep my original prose.\n"


def test_disabled_vault_does_not_grant_access_or_create_paths(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    module, commands, ownership = _installer(monkeypatch, root, enabled=False)
    units = tmp_path / "units"
    module.install(tmp_path / "config.toml", units)
    assert commands == ownership == []
    assert not root.exists()
    assert not units.exists()
