from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import create_worktree

from symphony.workspace import WorkspaceError, WorkspaceManager, validation_argv


def test_workspace_parents_remain_agent_traversable_under_service_umask(tmp_path):
    previous_umask = os.umask(0o077)
    try:
        manager = WorkspaceManager(tmp_path / "workspaces")
    finally:
        os.umask(previous_umask)

    for directory in (manager.root, manager.root / "repositories", manager.root / "runs"):
        mode = stat.S_IMODE(directory.stat().st_mode)
        assert mode & stat.S_IXGRP
        assert not mode & stat.S_IRGRP
        assert not mode & stat.S_IWGRP

    repository = manager.root / "repositories" / "solo--project"
    repository.mkdir(mode=0o700)
    repository.chmod(0o700)
    manager._prepare_shared_parent(repository)
    assert stat.S_IMODE(repository.stat().st_mode) & stat.S_IXGRP


def test_wrapper_commit_disables_repository_controlled_git_hooks(tmp_path):
    worktree = create_worktree(tmp_path, "agent/1-test")
    hook_dir = worktree / ".githooks"
    hook_dir.mkdir()
    sentinel = tmp_path / "hook-ran"
    hook = hook_dir / "post-commit"
    hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    hook.chmod(0o755)
    subprocess.run(["git", "-C", str(worktree), "config", "core.hooksPath", ".githooks"], check=True)
    (worktree / "change.txt").write_text("safe\n")

    WorkspaceManager.commit(worktree, 1)

    assert not sentinel.exists()


def test_agent_gets_writable_shared_group_content_but_read_only_git_metadata(tmp_path, monkeypatch):
    worktree = create_worktree(tmp_path, "agent/1-test")
    manager = WorkspaceManager(tmp_path)
    changed_groups: list[Path] = []
    real_chown = os.chown

    def record_chown(path, uid, gid):
        changed_groups.append(Path(path))
        real_chown(path, uid, gid)

    monkeypatch.setattr(os, "chown", record_chown)

    manager.prepare_for_agent(worktree)

    git_dir = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-dir"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    common_git_dir = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    if sys.platform.startswith("linux"):
        assert stat.S_IMODE(worktree.stat().st_mode) & stat.S_ISGID
    assert worktree in changed_groups
    assert worktree / "README.md" in changed_groups
    assert worktree.stat().st_gid == manager.root.stat().st_gid
    assert (worktree / "README.md").stat().st_gid == manager.root.stat().st_gid
    assert stat.S_IMODE((worktree / "README.md").stat().st_mode) & stat.S_IWGRP
    assert not stat.S_IMODE((worktree / ".git").stat().st_mode) & stat.S_IWGRP
    assert not stat.S_IMODE(manager._inside(Path(git_dir)).stat().st_mode) & stat.S_IWGRP
    common_config_mode = stat.S_IMODE((manager._inside(Path(common_git_dir)) / "config").stat().st_mode)
    assert common_config_mode & stat.S_IRGRP
    assert not common_config_mode & stat.S_IWGRP


def test_privileged_remote_is_derived_from_validated_repository_name():
    assert WorkspaceManager.github_remote("solo/project") == "https://github.com/solo/project.git"
    with pytest.raises(WorkspaceError):
        WorkspaceManager.github_remote("solo/project;evil")


def test_validation_uses_clean_lower_authority_process_boundary():
    argv = validation_argv(("python3", "-m", "pytest"), "openhands-validator")
    assert argv[:7] == ["sudo", "-n", "-H", "-u", "openhands-validator", "--", "env"]
    assert "-i" in argv
    assert argv[-3:] == ["python3", "-m", "pytest"]
    assert "/usr/bin/setpriv" not in argv
    assert argv[-7:-3] == ["/bin/sh", "-c", 'umask 0007; exec "$@"', "symphony-validation"]


def test_empty_setup_script_is_disabled(tmp_path):
    manager = WorkspaceManager(tmp_path)

    assert manager.run_setup(tmp_path, "", "openhands-validator") is None


def test_setup_script_must_be_a_regular_file(tmp_path):
    manager = WorkspaceManager(tmp_path)
    setup_directory = tmp_path / ".openhands"
    setup_directory.mkdir()

    with pytest.raises(WorkspaceError, match="setup script is not a regular file"):
        manager.run_setup(tmp_path, ".openhands", "openhands-validator")
