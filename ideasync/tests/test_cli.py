from __future__ import annotations

import plistlib
from collections.abc import Callable
from pathlib import Path

from conftest import REPOSITORY, Harness

from ideasync import cli
from ideasync.cli import main


def tree_contents(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_init_dry_run_creates_nothing(tmp_path: Path, capsys) -> None:
    data = tmp_path / "data"
    vault = tmp_path / "vault"

    code = main(["--data-dir", str(data), "init", "--vault", str(vault), "--dry-run"])
    output = capsys.readouterr()

    assert code == 0, output.err
    assert "would initialize" in output.out
    assert not data.exists()
    assert not vault.exists()


def test_add_and_schedule_dry_runs_create_nothing(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory()
    before = {"data": tree_contents(harness.data), "vault": tree_contents(harness.vault)}

    add = harness.invoke("add", "acme/another-idea", "--dry-run")
    install = harness.invoke("install-schedule", "--dry-run")
    uninstall = harness.invoke("uninstall-schedule", "--dry-run")

    after = {"data": tree_contents(harness.data), "vault": tree_contents(harness.vault)}
    assert add.code == install.code == uninstall.code == 0
    assert "would clone" in add.stdout
    definition = plistlib.loads(install.stdout.encode())
    assert definition["StartInterval"] == 120
    assert definition["ProgramArguments"][-1] == "sync"
    assert "would boot out" in uninstall.stdout
    assert after == before


def test_doctor_and_open_dry_run(harness_factory: Callable[..., Harness]) -> None:
    harness = harness_factory()

    doctor = harness.invoke("doctor")
    opened = harness.invoke("open", REPOSITORY, "--host", "ideas-vm", "--dry-run")

    assert doctor.code == 0, doctor.stderr
    assert "clone and contracts are healthy" in doctor.stdout
    assert opened.code == 0
    assert "would open http://127.0.0.1:4317/" in opened.stdout
    assert "-L 127.0.0.1:4317:127.0.0.1:4317 -- ideas-vm" in opened.stdout


def test_open_starts_tunnel_and_browser(harness_factory: Callable[..., Harness], monkeypatch) -> None:
    harness = harness_factory()
    commands: list[list[str]] = []
    opened: list[str] = []

    class Process:
        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            return None

        def kill(self):
            return None

    monkeypatch.setattr(cli.subprocess, "Popen", lambda command: commands.append(command) or Process())
    monkeypatch.setattr(cli, "_wait_for_tunnel", lambda _process, _port: None)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url) or True)

    result = harness.invoke("open", REPOSITORY, "--host", "ideas-vm", "--local-port", "14317")

    assert result.code == 0, result.stderr
    assert commands[0][-4:] == ["-L", "127.0.0.1:14317:127.0.0.1:4317", "--", "ideas-vm"]
    assert opened == ["http://127.0.0.1:14317/"]
    assert "closed preview tunnel" in result.stdout
