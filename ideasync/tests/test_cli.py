from __future__ import annotations

import plistlib
from collections.abc import Callable
from pathlib import Path

from conftest import REPOSITORY, Harness, git, graduate_remote

from ideasync import cli
from ideasync.cli import main
from ideasync.config import load_config


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


def test_remove_deregisters_only_a_graduated_repo_and_preserves_vault(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory()
    graduate_remote(harness)
    synced = harness.invoke("sync", REPOSITORY)
    assert synced.code == 0, synced.stderr
    head = git("rev-parse", "HEAD", cwd=harness.managed_clone).stdout.strip()
    vault_before = tree_contents(harness.vault_directory)
    data_before = tree_contents(harness.data)

    preview = harness.invoke("remove", REPOSITORY, "--dry-run")

    assert preview.code == 0, preview.stderr
    assert "vault files would be preserved" in preview.stdout
    assert tree_contents(harness.data) == data_before
    assert tree_contents(harness.vault_directory) == vault_before

    removed = harness.invoke("remove", REPOSITORY)
    after = harness.invoke("sync")

    assert removed.code == 0, removed.stderr
    assert "vault preserved" in removed.stdout
    assert not harness.managed_clone.exists()
    assert (harness.paths.retired_clone_for(REPOSITORY) / head).is_dir()
    assert load_config(harness.paths).repository(REPOSITORY) is None
    assert tree_contents(harness.vault_directory) == vault_before
    assert after.code == 0
    assert "no repositories configured" in after.stdout


def test_remove_refuses_an_active_ideas_repository(harness_factory: Callable[..., Harness]) -> None:
    harness = harness_factory()

    removed = harness.invoke("remove", REPOSITORY)

    assert removed.code == 1
    assert "graduate the repository before removing it" in removed.stderr
    assert harness.managed_clone.is_dir()
    assert load_config(harness.paths).repository(REPOSITORY) is not None


def test_doctor_and_open_dry_run(harness_factory: Callable[..., Harness]) -> None:
    harness = harness_factory()

    doctor = harness.invoke("doctor")
    opened = harness.invoke("open", REPOSITORY, "--dry-run")

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


def test_open_uses_pinned_port_when_latest_contract_drifted(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory()
    contract = harness.managed_clone / ".symphony" / "idea.toml"
    contract.write_text(contract.read_text().replace("4317", "5317"))
    git("add", ".symphony/idea.toml", cwd=harness.managed_clone)
    git("commit", "-m", "change preview port", cwd=harness.managed_clone)

    opened = harness.invoke("open", REPOSITORY, "--dry-run")
    doctor = harness.invoke("doctor")

    assert opened.code == 0, opened.stderr
    assert "127.0.0.1:4317:127.0.0.1:4317" in opened.stdout
    assert doctor.code == 1
    assert "preview port changed from pinned port 4317 to 5317" in doctor.stdout
