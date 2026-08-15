from __future__ import annotations

import fcntl
from collections.abc import Callable
from pathlib import Path

from conftest import PROGRESS, REPOSITORY, SPEC, Harness, clone_agent, commit_and_push, git


def remote_file(harness: Harness, path: str) -> bytes:
    result = git("--git-dir", str(harness.remote), "show", f"main:{path}")
    return result.stdout.encode()


def test_both_copy_directions(harness_factory: Callable[..., Harness]) -> None:
    harness = harness_factory()
    vault_spec = harness.vault_directory / "SPEC.md"
    edited_spec = SPEC.replace(b"First wish.", b"First wish, now with a timer.")
    vault_spec.write_bytes(edited_spec)

    outbound = harness.invoke("sync", REPOSITORY)

    assert outbound.code == 0, outbound.stderr
    assert remote_file(harness, "idea/SPEC.md") == edited_spec

    agent = clone_agent(harness.remote, harness.root / "agent")
    updated_progress = PROGRESS.replace(b"not started", b"done").replace(
        b"The first wish has not been implemented yet.", b"The timer is visible and working."
    )
    (agent / "idea" / "PROGRESS.md").write_bytes(updated_progress)
    (agent / "idea" / "assets" / "first-feature.png").write_bytes(b"new-png")
    (agent / "idea" / "assets" / "timer.png").write_bytes(b"timer-png")
    commit_and_push(agent, "agent progress")

    inbound = harness.invoke("sync", REPOSITORY)

    assert inbound.code == 0, inbound.stderr
    assert (harness.vault_directory / "PROGRESS.md").read_bytes() == updated_progress
    assert (harness.vault_directory / "assets" / "first-feature.png").read_bytes() == b"new-png"
    assert (harness.vault_directory / "assets" / "timer.png").read_bytes() == b"timer-png"


def test_hash_based_noop_preserves_inbound_file(harness_factory: Callable[..., Harness]) -> None:
    harness = harness_factory()
    progress = harness.vault_directory / "PROGRESS.md"
    before = progress.stat()

    result = harness.invoke("sync", REPOSITORY)

    after = progress.stat()
    assert result.code == 0
    assert "inbound unchanged" in result.stdout
    assert (after.st_ino, after.st_mtime_ns, after.st_size) == (before.st_ino, before.st_mtime_ns, before.st_size)


def test_quiet_period_defers_outbound_spec(harness_factory: Callable[..., Harness]) -> None:
    harness = harness_factory(quiet_period=60)
    vault_spec = harness.vault_directory / "SPEC.md"
    vault_spec.write_bytes(SPEC.replace(b"First wish.", b"A half-typed wish"))

    result = harness.invoke("sync", REPOSITORY)

    assert result.code == 0
    assert "quiet period active" in result.stdout
    assert remote_file(harness, "idea/SPEC.md") == SPEC


def test_spec_commit_stages_exactly_one_path(harness_factory: Callable[..., Harness]) -> None:
    harness = harness_factory()
    edited_spec = SPEC.replace(b"First wish.", b"A complete edited wish.")
    (harness.vault_directory / "SPEC.md").write_bytes(edited_spec)

    result = harness.invoke("sync", REPOSITORY)

    assert result.code == 0, result.stderr
    changed = git("--git-dir", str(harness.remote), "diff-tree", "--no-commit-id", "--name-only", "-r", "main").stdout.splitlines()
    subject = git("--git-dir", str(harness.remote), "show", "-s", "--format=%s", "main").stdout.strip()
    assert changed == ["idea/SPEC.md"]
    assert subject == "ideasync: update idea spec"


def install_push_race_hook(harness: Harness, racer: Path) -> None:
    hook = harness.managed_clone / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\ngit -C {racer!s} push origin main\n", encoding="utf-8")
    hook.chmod(0o755)


def test_lost_push_race_rebases_once(harness_factory: Callable[..., Harness]) -> None:
    harness = harness_factory()
    racer = clone_agent(harness.remote, harness.root / "racer")
    (racer / "README.md").write_text("# Concurrent agent update\n", encoding="utf-8")
    git("add", "README.md", cwd=racer)
    git("commit", "-m", "concurrent remote update", cwd=racer)
    install_push_race_hook(harness, racer)
    edited_spec = SPEC.replace(b"First wish.", b"Wish delivered after a race.")
    (harness.vault_directory / "SPEC.md").write_bytes(edited_spec)

    result = harness.invoke("sync", REPOSITORY)

    assert result.code == 0, result.stderr
    assert remote_file(harness, "idea/SPEC.md") == edited_spec
    subjects = git("--git-dir", str(harness.remote), "log", "-2", "--format=%s", "main").stdout.splitlines()
    assert subjects == ["ideasync: update idea spec", "concurrent remote update"]


def test_conflicting_push_race_stops_and_keeps_vault_file(
    harness_factory: Callable[..., Harness], monkeypatch
) -> None:
    monkeypatch.setattr("ideasync.runtime.platform.system", lambda: "Linux")
    harness = harness_factory()
    racer = clone_agent(harness.remote, harness.root / "racer")
    racer_spec = SPEC.replace(b"First wish.", b"Remote writer changed this wish.")
    (racer / "idea" / "SPEC.md").write_bytes(racer_spec)
    git("add", "idea/SPEC.md", cwd=racer)
    git("commit", "-m", "conflicting remote spec", cwd=racer)
    install_push_race_hook(harness, racer)
    vault_spec = SPEC.replace(b"First wish.", b"Vault writer changed this wish.")
    (harness.vault_directory / "SPEC.md").write_bytes(vault_spec)

    result = harness.invoke("sync", REPOSITORY)

    assert result.code == 1
    assert "rebase conflicted" in result.stdout
    assert (harness.managed_clone / "idea" / "SPEC.md").read_bytes() == vault_spec
    assert remote_file(harness, "idea/SPEC.md") == racer_spec
    assert "Overall: **failed**" in (harness.vault / "_ideasync" / "STATUS.md").read_text(encoding="utf-8")


def test_lock_contention_stops_without_git_mutation(
    harness_factory: Callable[..., Harness], monkeypatch
) -> None:
    monkeypatch.setattr("ideasync.runtime.platform.system", lambda: "Linux")
    harness = harness_factory()
    before = git("--git-dir", str(harness.remote), "rev-parse", "main").stdout.strip()
    lock_path = harness.paths.lock_for(REPOSITORY)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = harness.invoke("sync", REPOSITORY)
    after = git("--git-dir", str(harness.remote), "rev-parse", "main").stdout.strip()

    assert result.code == 1
    assert "another sync holds" in result.stdout
    assert after == before


def tree_contents(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_dry_run_causes_zero_mutation(harness_factory: Callable[..., Harness]) -> None:
    harness = harness_factory()
    (harness.vault_directory / "SPEC.md").write_bytes(SPEC.replace(b"First wish.", b"Dry-run wish."))
    before = {
        "data": tree_contents(harness.data),
        "vault": tree_contents(harness.vault),
        "remote": tree_contents(harness.remote),
    }

    result = harness.invoke("sync", REPOSITORY, "--dry-run")

    after = {
        "data": tree_contents(harness.data),
        "vault": tree_contents(harness.vault),
        "remote": tree_contents(harness.remote),
    }
    assert result.code == 0, result.stderr
    assert "would commit and push" in result.stdout
    assert after == before


def test_frontmatter_rejection_happens_before_inbound_copy(
    harness_factory: Callable[..., Harness], monkeypatch
) -> None:
    monkeypatch.setattr("ideasync.runtime.platform.system", lambda: "Linux")
    harness = harness_factory()
    original_progress = (harness.vault_directory / "PROGRESS.md").read_bytes()
    agent = clone_agent(harness.remote, harness.root / "agent")
    (agent / "idea" / "PROGRESS.md").write_bytes(PROGRESS.replace(b"not started", b"done"))
    commit_and_push(agent, "new inbound result")
    (harness.vault_directory / "SPEC.md").write_bytes(SPEC.replace(b"symphony: idea", b"symphony: issue"))

    result = harness.invoke("sync", REPOSITORY)

    assert result.code == 1
    assert "symphony: idea" in result.stdout
    assert (harness.vault_directory / "PROGRESS.md").read_bytes() == original_progress


def test_network_loss_preserves_vault_edit_and_recovers(
    harness_factory: Callable[..., Harness], monkeypatch
) -> None:
    monkeypatch.setattr("ideasync.runtime.platform.system", lambda: "Linux")
    harness = harness_factory()
    edited_spec = SPEC.replace(b"First wish.", b"Wish saved while offline.")
    (harness.vault_directory / "SPEC.md").write_bytes(edited_spec)
    offline_remote = harness.remote.with_name("remote-offline.git")
    harness.remote.rename(offline_remote)

    failed = harness.invoke("sync", REPOSITORY)

    assert failed.code == 1
    assert (harness.vault_directory / "SPEC.md").read_bytes() == edited_spec
    offline_remote.rename(harness.remote)

    recovered = harness.invoke("sync", REPOSITORY)

    assert recovered.code == 0, recovered.stderr
    assert remote_file(harness, "idea/SPEC.md") == edited_spec
