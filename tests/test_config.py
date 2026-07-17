from __future__ import annotations

from pathlib import Path

import pytest

from symphony.config import load_config


def _config(path: Path, *, host: str = "127.0.0.1", setup_script: str = ".openhands/setup.sh") -> Path:
    path.write_text(
        f'''[service]
listen_host = "{host}"

[github]
allowed_repositories = ["solo/project"]

[scheduler.provider_concurrency]
codex = 1

[providers.codex]
enabled = true
adapter = "openhands-acp"
acp_command = ["codex-acp"]
auth_command = ["codex", "login", "status"]

[repositories."solo/project"]
setup_script = "{setup_script}"
'''
    )
    return path


def test_config_rejects_public_bind(tmp_path):
    with pytest.raises(ValueError, match="must be loopback"):
        load_config(_config(tmp_path / "config.toml", host="0.0.0.0"))


def test_config_rejects_setup_path_escape(tmp_path):
    with pytest.raises(ValueError, match="confined relative path"):
        load_config(_config(tmp_path / "config.toml", setup_script="../outside.sh"))


def test_config_rejects_unsafe_validation_account(tmp_path):
    path = _config(tmp_path / "config.toml")
    path.write_text(path.read_text().replace('listen_host = "127.0.0.1"', 'validation_user = "bad;user"'))
    with pytest.raises(ValueError, match="safe local account"):
        load_config(path)
