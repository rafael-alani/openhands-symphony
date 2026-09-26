#!/usr/bin/env python3
"""Update only Symphony's application code with idle checks and software rollback."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path

INSTALL = Path("/opt/openhands-symphony")
RUNTIME = Path("/opt/openhands-symphony-tool")
ACP = Path("/opt/openhands-acp/node_modules/@agentclientprotocol/codex-acp/dist/index.js")
CONFIG = Path("/etc/openhands-symphony/config.toml")
SOURCE_PATH = Path("/etc/openhands-symphony/source-path")
SERVICE = "openhands-symphony.service"
TIMER = "openhands-symphony-reconcile.timer"
WHEEL = "openhands_symphony-0.1.0-py3-none-any.whl"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def active(unit: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False).returncode == 0


def require_idle(database: Path) -> None:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        leases = connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
        if leases:
            raise RuntimeError(f"{leases} retained run leases: wait for work to drain before updating")


def check_software(source: Path, install: Path, runtime: Path) -> None:
    for name in ("versions.env", "pyproject.toml", "uv.lock", "src/symphony/store.py"):
        if (source / name).read_bytes() != (install / name).read_bytes():
            raise ValueError(f"{name} differs: this updater only handles code without runtime or database migrations")
    installed_store = Path(run(str(runtime / "bin/python"), "-c",
        "import symphony.store; print(symphony.store.__file__)", capture_output=True).stdout.strip())
    if installed_store.read_bytes() != (source / "src/symphony/store.py").read_bytes():
        raise ValueError("installed database code differs from the staged release")
    with zipfile.ZipFile(source / "dist" / WHEEL) as wheel:
        expected = {file.relative_to(source / "src").as_posix(): file.read_bytes()
                    for file in (source / "src/symphony").rglob("*.py")}
        actual = {name: wheel.read(name) for name in wheel.namelist()
                  if name.startswith("symphony/") and name.endswith(".py")}
        if actual != expected:
            raise ValueError("deployment wheel differs from the staged Python source")


def set_defaults(text: str) -> str:
    match = re.search(r"(?m)^\[providers\.codex\][ \t]*(?:#[^\r\n]*)?\r?$", text)
    if match is None:
        raise ValueError("existing providers.codex configuration is required")
    end = re.search(r"(?m)^\[", text[match.end():])
    stop = match.end() + end.start() if end else len(text)
    block = text[match.end():stop]
    for key, value in (("reasoning_effort", "xhigh"), ("speed", "normal")):
        block = re.sub(rf"(?m)^[ \t]*{key}[ \t]*=.*(?:\n|$)", "", block)
        block = f'\n{key} = "{value}"\n' + block.lstrip("\r\n")
    return text[:match.end()] + block + text[stop:]


def sync(source: Path, target: Path) -> None:
    # Both targets are fixed generated-software directories, never application data.
    if target not in (INSTALL, RUNTIME):
        raise ValueError("unexpected software sync target")
    excludes = ["--exclude=.git", "--exclude=.venv", "--exclude=dist", "--exclude=.pytest_cache",
                "--exclude=.ruff_cache", "--exclude=__pycache__"] if target == INSTALL else []
    run("rsync", "-a", "--delete", *excludes, str(source) + "/", str(target) + "/")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--check", action="store_true", help="check software compatibility without sudo or changing services")
    args = parser.parse_args()
    if not args.check and os.geteuid() != 0:
        raise SystemExit("Use sudo for the application update")
    os.umask(0o077)
    source = Path(__file__).resolve().parents[1]
    if source == INSTALL:
        raise SystemExit("Run from the staged Git checkout, outside /opt/openhands-symphony")
    revision = run("git", "-c", f"safe.directory={source}", "-C", str(source), "rev-parse", "HEAD",
                   capture_output=True).stdout.strip()
    if revision != args.expected_commit or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise SystemExit("staged revision does not match the reviewed commit")
    run("git", "-c", f"safe.directory={source}", "-C", str(source), "diff", "--exit-code", "HEAD", "--",
        stdout=subprocess.DEVNULL)
    check_software(source, INSTALL, RUNTIME)
    # Validate the patch against a temporary copy before touching the service.
    import tempfile

    from patch_codex_acp import patch
    with tempfile.TemporaryDirectory(prefix="symphony-acp-preflight-") as temporary:
        package = Path(temporary)
        (package / "dist").mkdir()
        shutil.copy2(ACP, package / "dist/index.js")
        shutil.copy2(ACP.parent.parent / "package.json", package / "package.json")
        patch(package)
        run("node", "--check", str(package / "dist/index.js"), stdout=subprocess.DEVNULL)
    if args.check:
        print("PASS: staged source, installed database code, runtime pins, wheel, and adapter compatibility")
        return
    import tomllib
    configured = tomllib.loads(CONFIG.read_text())
    updated_config = set_defaults(CONFIG.read_text())
    tomllib.loads(updated_config)
    database = Path(configured.get("service", {}).get("state_dir", "/var/lib/openhands-symphony")) / "state.db"
    require_idle(database)
    # Verify credentials and effective ACP options before stopping the service.
    # Only public source scripts are staged here; credentials stay in the worker home.
    with tempfile.TemporaryDirectory(prefix="symphony-authenticated-preflight-") as temporary:
        probe_root = Path(temporary)
        probe_root.chmod(0o755)
        for name in ("patch_codex_acp.py", "probe_agent_settings.py"):
            shutil.copyfile(source / "scripts" / name, probe_root / name)
            (probe_root / name).chmod(0o644)
        probe = run("runuser", "-u", "openhands-agent", "--", "env", "HOME=/var/lib/openhands-agent",
                    "python3", str(probe_root / "probe_agent_settings.py"), stdout=subprocess.PIPE,
                    cwd="/var/lib/openhands-agent")
        print(probe.stdout, end="")
    rollback = Path("/root") / ("symphony-settings-rollback-" + datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ"))
    rollback.mkdir(mode=0o700)
    was_active, timer_active = active(SERVICE), active(TIMER)
    if active("openhands-symphony-reconcile.service"):
        raise SystemExit("reconciliation is running; retry after it completes")
    changed = False
    try:
        if timer_active:
            run("systemctl", "stop", TIMER)
        run("systemctl", "stop", SERVICE)
        if active("openhands-symphony-reconcile.service"):
            raise RuntimeError("reconciliation started during preflight; retry after it completes")
        require_idle(database)
        shutil.copytree(INSTALL, rollback / "source", symlinks=True)
        shutil.copytree(RUNTIME, rollback / "runtime", symlinks=True)
        shutil.copy2(CONFIG, rollback / "config.toml")
        if SOURCE_PATH.exists():
            shutil.copy2(SOURCE_PATH, rollback / "source-path")
        shutil.copy2(ACP, rollback / "codex-acp.js")
        (rollback / "adapter-probe.jsonl").write_text(probe.stdout)
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as original:
            with sqlite3.connect(rollback / "state.db") as saved:
                original.backup(saved)
                assert saved.execute("PRAGMA quick_check").fetchone() == ("ok",)
        changed = True
        sync(source, INSTALL)
        # Software must stay readable under the service account's filesystem boundary.
        run("chmod", "-R", "a+rX", str(INSTALL))
        run("uv", "pip", "install", "--python", str(RUNTIME / "bin/python"), "--no-deps", "--reinstall",
            str(source / "dist" / WHEEL))
        run("chmod", "-R", "a+rX", str(RUNTIME))
        installed = Path(run(str(RUNTIME / "bin/python"), "-c",
            "import symphony; from pathlib import Path; print(Path(symphony.__file__).parent)",
            capture_output=True).stdout.strip())
        for file in (source / "src/symphony").rglob("*.py"):
            if file.read_bytes() != (installed / file.relative_to(source / "src/symphony")).read_bytes():
                raise RuntimeError("installed wheel differs from the reviewed source")
        CONFIG.write_text(updated_config)
        shutil.copystat(rollback / "config.toml", CONFIG)
        SOURCE_PATH.write_text(str(source) + "\n")
        SOURCE_PATH.chmod(0o640)
        os.chown(SOURCE_PATH, 0, CONFIG.stat().st_gid)
        patch(ACP.parent.parent)
        run(str(RUNTIME / "bin/agentctl"), "--config", str(CONFIG), "settings")
        (INSTALL / "DEPLOYED_COMMIT").write_text(revision + "\n")
        (INSTALL / "DEPLOYED_COMMIT").chmod(0o644)
        if was_active:
            run("systemctl", "start", SERVICE)
            for attempt in range(30):
                try:
                    with urllib.request.urlopen("http://127.0.0.1:8787/agent-settings", timeout=2) as response:
                        settings = json.load(response)
                    if settings["codex"] != {"reasoning_effort": "xhigh", "speed": "normal"}:
                        raise RuntimeError("live defaults do not match the requested settings")
                    break
                except OSError:
                    if attempt == 29:
                        raise
                    time.sleep(1)
        result = {"commit": revision, "rollback": str(rollback), "service_active": active(SERVICE),
                  "config_sha256": hashlib.sha256(CONFIG.read_bytes()).hexdigest()}
        (rollback / "deployment.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    except Exception:
        if changed:
            run("systemctl", "stop", SERVICE)
            sync(rollback / "source", INSTALL)
            sync(rollback / "runtime", RUNTIME)
            shutil.copy2(rollback / "config.toml", CONFIG)
            if (rollback / "source-path").exists():
                shutil.copy2(rollback / "source-path", SOURCE_PATH)
            else:
                SOURCE_PATH.unlink(missing_ok=True)
            shutil.copy2(rollback / "codex-acp.js", ACP)
            print(f"Previous software/configuration restored. Retained rollback: {rollback}", flush=True)
        raise
    finally:
        if was_active and not active(SERVICE):
            run("systemctl", "start", SERVICE)
        if timer_active:
            run("systemctl", "start", TIMER)


if __name__ == "__main__":
    main()
