import importlib.util
import sqlite3
import tomllib
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "application_update", Path(__file__).resolve().parents[1] / "scripts/update_application.py",
)
UPDATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPDATE)


def test_update_preserves_other_configuration_and_explicitly_sets_defaults():
    original = '''[service]
state_dir = "/srv/symphony"
[providers.codex] # retained section
enabled = true
acp_command = ["wrapper"]
reasoning_effort = "low"
speed = "fast"
[providers.claude]
enabled = true
permission_mode = "full"
'''
    updated = UPDATE.set_defaults(original)
    assert UPDATE.set_defaults(updated) == updated
    expected = tomllib.loads(original)
    expected["providers"]["codex"].update(reasoning_effort="xhigh", speed="normal")
    assert tomllib.loads(updated) == expected


def test_update_refuses_active_or_stale_leases_without_modifying_database(tmp_path):
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE leases (run_id TEXT)")
    UPDATE.require_idle(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO leases VALUES ('retained')")
    with pytest.raises(RuntimeError, match="wait for work to drain"):
        UPDATE.require_idle(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT run_id FROM leases").fetchall() == [("retained",)]
