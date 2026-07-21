from __future__ import annotations

from pathlib import Path

import pytest

from symphony.config import load_config

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "examples" / "config.toml"


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


def test_config_loads_allowlisted_label_concurrency_scopes(tmp_path):
    path = _config(tmp_path / "config.toml")
    path.write_text(
        path.read_text().replace(
            'setup_script = ".openhands/setup.sh"',
            'concurrency_scope = "label"\n'
            'concurrency_labels = { "project:frontend" = "frontend", "project:backend" = "backend" }\n'
            'setup_script = ".openhands/setup.sh"',
        )
    )

    config = load_config(path)

    assert config.concurrency_key("solo/project", ("project:frontend",)) == "solo/project:frontend"
    with pytest.raises(ValueError, match="exactly one"):
        config.concurrency_key("solo/project", ())


def test_example_config_bootstraps_repository_owned_validation() -> None:
    config = load_config(EXAMPLE_CONFIG)
    repository = config.repository("CHANGE_ME/CHANGE_ME")

    assert repository.validation_commands == ()
    assert repository.setup_script == ""
