#!/usr/bin/env python3
"""Install the filesystem grant for an enabled Syncthing vault (no Syncthing pairing)."""
from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import subprocess
from pathlib import Path

from symphony.config import load_config


def install(config_path: Path, unit_root: Path = Path("/etc/systemd/system")) -> None:
    config = load_config(config_path)
    if not config.vault.enabled:
        return
    root = config.vault.path
    account = pwd.getpwnam("openhands-symphony")
    try:
        vault_group = grp.getgrnam("symphony-vault")
    except KeyError:
        subprocess.run(["groupadd", "--system", "symphony-vault"], check=True)
        vault_group = grp.getgrnam("symphony-vault")
    # install.sh reconciles service groups; restore this grant on every update
    # so interactive agentctl and the optional Syncthing transport retain access.
    subprocess.run(["usermod", "-aG", vault_group.gr_name, account.pw_name], check=True)
    for path in (root, root / config.vault.projects_dir, root / "_symphony"):
        # Preserve all ownership/modes on an existing synced vault.
        if not path.exists():
            path.mkdir(parents=True, mode=0o2770)
            os.chown(path, account.pw_uid, vault_group.gr_gid)
            path.chmod(0o2770)
    escaped = json.dumps(str(root).replace("%", "%%"), ensure_ascii=False)
    for unit in ("openhands-symphony.service", "openhands-symphony-reconcile.service"):
        directory = unit_root / f"{unit}.d"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "vault.conf").write_text(
            f"[Service]\nReadWritePaths={escaped}\nSupplementaryGroups=symphony-vault\n"
        )
    # Provider full-access mode must not expose the original notes. Enforce
    # this independently of synced file modes and the provider's own sandbox.
    for unit in ("openhands-canvas.service", "openhands-browser.service", "openhands-idea-preview.service"):
        directory = unit_root / f"{unit}.d"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "vault.conf").write_text(f"[Service]\nInaccessiblePaths=-{escaped}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/openhands-symphony/config.toml"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("run as root to install the service filesystem grant")
    install(args.config)
