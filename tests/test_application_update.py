import importlib.util
import io
import shutil
import sqlite3
import subprocess
import sys
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


def test_update_restores_execute_permissions_after_sync_from_nonexecutable_checkout(tmp_path, monkeypatch):
    source, installed = tmp_path / "checkout", tmp_path / "installed"
    scripts = source / "scripts"
    scripts.mkdir(parents=True)
    installed.mkdir()
    for name in ("codex_acp_wrapper.sh", "claude_acp_wrapper.sh", "probe_agent_settings.py"):
        path = scripts / name
        path.write_text("#!/bin/sh\nprintf 'worker-launch-ok\\n'\n")
        path.chmod(0o664)
    (scripts / "README.md").write_text("not an executable\n")
    (scripts / "README.md").chmod(0o644)
    monkeypatch.setattr(UPDATE, "INSTALL", installed)

    UPDATE.sync(source, installed)
    wrapper = installed / "scripts/codex_acp_wrapper.sh"
    with pytest.raises(PermissionError):
        subprocess.run([str(wrapper)], check=True)
    UPDATE.install_source(source)
    for name in ("codex_acp_wrapper.sh", "claude_acp_wrapper.sh", "probe_agent_settings.py"):
        path = installed / "scripts" / name
        assert path.stat().st_mode & 0o777 == 0o755
        assert subprocess.check_output([str(path)], text=True) == "worker-launch-ok\n"
        assert path.read_bytes() == (scripts / name).read_bytes()
    assert (installed / "scripts/README.md").stat().st_mode & 0o111 == 0
    # A second deployment must survive rsync copying the non-executable modes again.
    UPDATE.install_source(source)
    assert subprocess.check_output([str(wrapper)], text=True) == "worker-launch-ok\n"


def test_installed_launch_probe_uses_worker_identity_and_exact_configured_command(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="verified installed command\n")

    monkeypatch.setattr(UPDATE, "run", run)
    command = ["/opt/openhands-symphony/scripts/codex_acp_wrapper.sh", "--example"]
    configured = {"providers": {
        "codex": {"enabled": True, "acp_command": command},
        "claude": {"enabled": True, "acp_command": ["/opt/claude-wrapper"]},
        "disabled": {"enabled": False, "acp_command": ["/missing"]},
    }}
    assert UPDATE.verify_installed_launch(configured) == "verified installed command\n"
    assert [args for args, _ in calls[:2]] == [
        ("runuser", "-u", "openhands-agent", "--", "test", "-x", command[0]),
        ("runuser", "-u", "openhands-agent", "--", "test", "-x", "/opt/claude-wrapper"),
    ]
    args, kwargs = calls[-1]
    assert args == ("runuser", "-u", "openhands-agent", "--", "env", "HOME=/var/lib/openhands-agent",
                    "python3", str(UPDATE.INSTALL / "scripts/probe_agent_settings.py"), "--command", *command)
    assert kwargs["cwd"] == "/var/lib/openhands-agent"


def test_launch_probe_stops_when_worker_cannot_execute_configured_provider(monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(UPDATE, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        UPDATE.verify_installed_launch({"providers": {"codex": {
            "enabled": True, "acp_command": ["/nonexecutable-wrapper"],
        }}})
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [None, "worker-access", "handshake"])
def test_update_probes_installed_launch_before_success_and_rolls_back_on_failure(tmp_path, monkeypatch, failure):
    source, installed, runtime, rollback = (tmp_path / name for name in ("source", "installed", "runtime", "rollback"))
    for root in (source, installed):
        (root / "scripts").mkdir(parents=True)
        (root / "src/symphony").mkdir(parents=True)
        (root / "scripts/codex_acp_wrapper.sh").write_text("#!/bin/sh\nexit 0\n")
        (root / "scripts/codex_acp_wrapper.sh").chmod(0o664)
        for name in ("patch_codex_acp.py", "probe_agent_settings.py"):
            (root / "scripts" / name).write_text("# fixture\n")
        (root / "src/symphony/__init__.py").write_text("new" if root == source else "old")
    (runtime / "package").mkdir(parents=True)
    (runtime / "package/__init__.py").write_text("old")
    rollback.mkdir()
    (installed / "DEPLOYED_COMMIT").write_text("previous\n")
    config, source_path = tmp_path / "config.toml", tmp_path / "source-path"
    wrapper = installed / "scripts/codex_acp_wrapper.sh"
    config.write_text(f'''[service]
state_dir = "{tmp_path}"
[providers.codex]
enabled = true
acp_command = ["{wrapper}"]
''')
    original_config = config.read_bytes()
    source_path.write_text("previous-checkout\n")
    with sqlite3.connect(tmp_path / "state.db") as connection:
        connection.execute("CREATE TABLE leases (run_id TEXT)")
    adapter = tmp_path / "adapter/dist/index.js"
    adapter.parent.mkdir(parents=True)
    adapter.write_text("retained adapter")
    (adapter.parent.parent / "package.json").write_text("{}")
    for key, value in {"INSTALL": installed, "RUNTIME": runtime, "CONFIG": config, "SOURCE_PATH": source_path,
                       "ACP": adapter, "ROLLBACK_ROOT": rollback, "__file__": str(source / "scripts/update_application.py")}.items():
        monkeypatch.setattr(UPDATE, key, value)
    monkeypatch.setattr(UPDATE.os, "geteuid", lambda: 0)
    monkeypatch.setattr(UPDATE.os, "chown", lambda *args: None)
    monkeypatch.setattr(UPDATE, "check_software", lambda *args: None)
    monkeypatch.setitem(sys.modules, "patch_codex_acp", SimpleNamespace(patch=lambda *args: None))
    monkeypatch.setattr(sys, "argv", ["update_application.py", "--expected-commit", "a" * 40])
    states = {UPDATE.SERVICE: True, UPDATE.TIMER: True}
    monkeypatch.setattr(UPDATE, "active", lambda unit: states.get(unit, False))
    actions = []

    def run(*args, **kwargs):
        actions.append(args)
        if args[0] in {"rsync", "chmod"}:
            return subprocess.run(args, check=True, text=True, **kwargs)
        if args[0] == "git" and args[-2:] == ("rev-parse", "HEAD"):
            return SimpleNamespace(stdout="a" * 40)
        if args[0] == "systemctl":
            states[args[2]] = args[1] == "start"
        if args[0] == "uv":
            shutil.copy2(source / "src/symphony/__init__.py", runtime / "package/__init__.py")
        if args[0] == str(runtime / "bin/python"):
            return SimpleNamespace(stdout=str(runtime / "package"))
        if args[:5] == ("runuser", "-u", "openhands-agent", "--", "test"):
            assert wrapper.stat().st_mode & 0o777 == 0o755
            if failure == "worker-access":
                raise subprocess.CalledProcessError(1, args)
        if "--command" in args:
            assert not states[UPDATE.SERVICE]
            assert not states[UPDATE.TIMER]
            assert (installed / "DEPLOYED_COMMIT").exists() is False
            if failure == "handshake":
                raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout="probe ok\n")

    monkeypatch.setattr(UPDATE, "run", run)
    monkeypatch.setattr(UPDATE.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(
        b'{"codex":{"reasoning_effort":"xhigh","speed":"normal"}}'))
    previous_umask = UPDATE.os.umask(0o022)
    try:
        if failure:
            with pytest.raises(subprocess.CalledProcessError):
                UPDATE.main()
            assert (installed / "src/symphony/__init__.py").read_text() == "old"
            assert (runtime / "package/__init__.py").read_text() == "old"
            assert (installed / "DEPLOYED_COMMIT").read_text() == "previous\n"
            assert wrapper.stat().st_mode & 0o777 == 0o664
            assert config.read_bytes() == original_config
            assert source_path.read_text() == "previous-checkout\n"
            assert not list(rollback.glob("*/deployment.json"))
        else:
            UPDATE.main()
            assert (installed / "DEPLOYED_COMMIT").read_text() == "a" * 40 + "\n"
            assert list(rollback.glob("*/installed-launch-probe.jsonl"))[0].read_text() == "probe ok\n"
            assert list(rollback.glob("*/deployment.json"))
            probe_index = next(i for i, args in enumerate(actions) if "--command" in args)
            start_index = next(i for i, args in enumerate(actions) if args == ("systemctl", "start", UPDATE.SERVICE))
            assert probe_index < start_index
    finally:
        UPDATE.os.umask(previous_umask)
    assert states == {UPDATE.SERVICE: True, UPDATE.TIMER: True}
    with sqlite3.connect(tmp_path / "state.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM leases").fetchone() == (0,)


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
