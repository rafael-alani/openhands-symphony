from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import create_worktree

from symphony.workspace import WorkspaceError, WorkspaceManager, validation_argv


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


def test_agent_gets_writable_content_but_read_only_git_metadata(tmp_path):
    worktree = create_worktree(tmp_path, "agent/1-test")
    manager = WorkspaceManager(tmp_path)

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
    assert "--umask" in argv
