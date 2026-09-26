import importlib.util
import sqlite3
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.parametrize("mismatch", [None, "source-schema", "installed-schema", "wheel"])
def test_software_preflight_checks_real_runtime_and_wheel_before_any_mutation(tmp_path, monkeypatch, mismatch):
    source, installed, runtime = (tmp_path / name for name in ("source", "installed", "runtime"))
    for root in (source, installed):
        (root / "src/symphony").mkdir(parents=True)
        for name in ("versions.env", "pyproject.toml", "uv.lock", "src/symphony/store.py"):
            (root / name).write_text("baseline\n")
    runtime.mkdir()
    live_store = runtime / "store.py"
    live_store.write_text("baseline\n")
    (source / "dist").mkdir()
    with zipfile.ZipFile(source / "dist" / UPDATE.WHEEL, "w") as wheel:
        wheel.writestr("symphony/store.py", "wrong wheel\n" if mismatch == "wheel" else "baseline\n")
    if mismatch == "source-schema":
        (source / "src/symphony/store.py").write_text("SCHEMA_VERSION = 9\n")
    if mismatch == "installed-schema":
        live_store.write_text("SCHEMA_VERSION = 7\n")
    monkeypatch.setattr(UPDATE, "run", lambda *args, **kwargs: SimpleNamespace(stdout=str(live_store)))
    if mismatch:
        with pytest.raises(ValueError, match="differs"):
            UPDATE.check_software(source, installed, runtime)
    else:
        UPDATE.check_software(source, installed, runtime)
    assert (installed / "src/symphony/store.py").read_text() == "baseline\n"
