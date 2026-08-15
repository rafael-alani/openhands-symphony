from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ideasync.errors import GitError


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Git:
    def __init__(self, executable: str = "git") -> None:
        self.executable = executable

    def run(self, arguments: list[str], *, cwd: Path | None = None, check: bool = True) -> CommandResult:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "LC_ALL": "C",
            }
        )
        process = subprocess.run(
            [self.executable, *arguments],
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        result = CommandResult(process.returncode, process.stdout, process.stderr)
        if check and process.returncode != 0:
            detail = (process.stderr or process.stdout).strip()
            raise GitError(f"git {' '.join(arguments[:2])} failed: {detail}")
        return result

    def clone(self, remote: str, destination: Path) -> None:
        self.run(["clone", "--origin", "origin", "--", remote, str(destination)])

    def configure_identity(self, clone: Path) -> None:
        self.run(["config", "user.name", "ideasync"], cwd=clone)
        self.run(["config", "user.email", "ideasync@localhost"], cwd=clone)

    def current_branch(self, clone: Path) -> str:
        branch = self.run(["branch", "--show-current"], cwd=clone).stdout.strip()
        if not branch:
            raise GitError(f"managed clone has a detached HEAD: {clone}")
        return branch

    def assert_clean(self, clone: Path) -> None:
        status = self.run(["status", "--porcelain=v1", "--untracked-files=all"], cwd=clone).stdout
        if status:
            lines = ", ".join(line.strip() for line in status.splitlines()[:5])
            raise GitError(f"managed clone is not clean; refusing automatic recovery: {lines}")

    def fetch(self, clone: Path) -> None:
        self.run(["fetch", "--prune", "origin"], cwd=clone)

    @staticmethod
    def remote_ref(branch: str) -> str:
        return f"refs/remotes/origin/{branch}"

    def rev_parse(self, clone: Path, revision: str = "HEAD") -> str:
        return self.run(["rev-parse", "--verify", revision], cwd=clone).stdout.strip()

    def is_ancestor(self, clone: Path, older: str, newer: str) -> bool:
        result = self.run(["merge-base", "--is-ancestor", older, newer], cwd=clone, check=False)
        if result.returncode not in (0, 1):
            detail = (result.stderr or result.stdout).strip()
            raise GitError(f"cannot compare managed clone history: {detail}")
        return result.returncode == 0

    def fast_forward(self, clone: Path, revision: str) -> None:
        self.run(["merge", "--ff-only", revision], cwd=clone)

    def ahead_count(self, clone: Path, base: str, head: str = "HEAD") -> int:
        value = self.run(["rev-list", "--count", f"{base}..{head}"], cwd=clone).stdout.strip()
        return int(value)

    def changed_paths(self, clone: Path, commit: str) -> list[str]:
        output = self.run(
            ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit],
            cwd=clone,
        ).stdout
        return sorted(path for path in output.split("\0") if path)

    def subject(self, clone: Path, commit: str = "HEAD") -> str:
        return self.run(["show", "-s", "--format=%s", commit], cwd=clone).stdout.rstrip("\n")

    def staged_paths(self, clone: Path) -> list[str]:
        output = self.run(["diff", "--cached", "--name-only", "-z"], cwd=clone).stdout
        return sorted(path for path in output.split("\0") if path)

    def add_spec(self, clone: Path) -> None:
        self.run(["add", "--", "idea/SPEC.md"], cwd=clone)
        staged = self.staged_paths(clone)
        if staged != ["idea/SPEC.md"]:
            raise GitError(f"exact staging guard failed; staged paths are: {staged}")

    def commit_spec(self, clone: Path, message: str) -> str:
        self.run(["commit", "-m", message, "--", "idea/SPEC.md"], cwd=clone)
        return self.rev_parse(clone)

    def push(self, clone: Path, branch: str) -> CommandResult:
        return self.run(
            ["push", "--porcelain", "origin", f"HEAD:refs/heads/{branch}"],
            cwd=clone,
            check=False,
        )

    def rebase(self, clone: Path, revision: str) -> CommandResult:
        return self.run(["rebase", revision], cwd=clone, check=False)

    def abort_rebase(self, clone: Path) -> None:
        self.run(["rebase", "--abort"], cwd=clone)

    def origin_url(self, clone: Path) -> str:
        return self.run(["remote", "get-url", "origin"], cwd=clone).stdout.strip()
