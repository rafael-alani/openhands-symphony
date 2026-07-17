from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import IssueSnapshot, Job, ValidationResult, utcnow


class WorkspaceError(RuntimeError):
    pass


SECRET_PATTERN = re.compile(
    r"(?i)(authorization:\s*(?:bearer|token)\s+)[^\s]+|((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s]+"
)
SENSITIVE_ENV = {
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "BROWSER_USE_API_KEY",
    "GH_CONFIG_DIR",
}

DEFAULT_GH_CONFIG_DIR = "/var/lib/openhands-symphony/github"


def redact(value: str, limit: int = 50_000) -> str:
    return SECRET_PATTERN.sub(lambda match: f"{match.group(1) or match.group(2)}[REDACTED]", value)[-limit:]


def validation_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in SENSITIVE_ENV}


def validation_argv(command: tuple[str, ...], run_as_user: str) -> list[str]:
    if not run_as_user:
        return list(command)
    return [
        "sudo",
        "-n",
        "-H",
        "-u",
        run_as_user,
        "--",
        "env",
        "-i",
        f"HOME=/var/lib/{run_as_user}",
        "PATH=/opt/browser-use/bin:/usr/local/bin:/usr/bin:/bin",
        "CI=true",
        "/usr/bin/setpriv",
        "--umask",
        "0007",
        "--",
        *command,
    ]


def orchestrator_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.setdefault("GH_CONFIG_DIR", DEFAULT_GH_CONFIG_DIR)
    return environment


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 300,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        args,
        cwd=cwd,
        env=env or validation_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if process.returncode != 0:
        raise WorkspaceError(f"command failed ({process.returncode}): {args[0]}: {redact(process.stdout)}")
    return process


@dataclass
class WorkspaceManager:
    root: Path

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _inside(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise WorkspaceError(f"path escapes configured workspace root: {resolved}")
        return resolved

    @staticmethod
    def _repo_key(repository: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or ".." in repository:
            raise WorkspaceError("invalid repository name")
        return repository.replace("/", "--")

    def checkout(self, job: Job, snapshot: IssueSnapshot) -> Path:
        key = self._repo_key(job.repository)
        repository_dir = self._inside(self.root / "repositories" / key)
        worktree = self._inside(self.root / "runs" / job.id)
        repository_dir.parent.mkdir(parents=True, exist_ok=True)
        worktree.parent.mkdir(parents=True, exist_ok=True)

        if not (repository_dir / ".git").exists():
            if repository_dir.exists() and any(repository_dir.iterdir()):
                raise WorkspaceError(f"repository cache is not a git checkout: {repository_dir}")
            _run(
                ["gh", "repo", "clone", job.repository, str(repository_dir), "--", "--filter=blob:none"],
                timeout=900,
                env=orchestrator_environment(),
            )
        _run(
            ["git", "-C", str(repository_dir), "fetch", "--prune", "origin"],
            timeout=900,
            env=orchestrator_environment(),
        )

        if worktree.exists():
            if not (worktree / ".git").exists():
                raise WorkspaceError(f"existing run directory is not a git worktree: {worktree}")
            return worktree

        local_branch = (
            subprocess.run(
                ["git", "-C", str(repository_dir), "show-ref", "--verify", "--quiet", f"refs/heads/{job.branch}"],
                env=validation_environment(),
                check=False,
            ).returncode
            == 0
        )
        remote_branch = (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository_dir),
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/remotes/origin/{job.branch}",
                ],
                env=validation_environment(),
                check=False,
            ).returncode
            == 0
        )
        if local_branch:
            _run(["git", "-C", str(repository_dir), "worktree", "add", str(worktree), job.branch], timeout=300)
        elif remote_branch:
            _run(
                [
                    "git",
                    "-C",
                    str(repository_dir),
                    "worktree",
                    "add",
                    "-b",
                    job.branch,
                    str(worktree),
                    f"origin/{job.branch}",
                ],
                timeout=300,
            )
        else:
            _run(
                [
                    "git",
                    "-C",
                    str(repository_dir),
                    "worktree",
                    "add",
                    "-b",
                    job.branch,
                    str(worktree),
                    f"origin/{snapshot.default_branch}",
                ],
                timeout=300,
            )
        return worktree

    def run_setup(self, worktree: Path, setup_script: str, validation_user: str) -> ValidationResult | None:
        script = self._inside_script(worktree, setup_script)
        if not script.exists():
            return None
        return run_validation(("bash", str(script)), worktree, 1800, run_as_user=validation_user)

    def prepare_for_agent(self, worktree: Path) -> None:
        """Expose worktree content to workers while keeping Git metadata read-only."""
        root = self._inside(worktree)
        git_dir_output = _run(["git", "rev-parse", "--path-format=absolute", "--git-dir"], cwd=root).stdout.strip()
        git_dir = self._inside(Path(git_dir_output))
        common_git_output = _run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=root
        ).stdout.strip()
        common_git_dir = self._inside(Path(common_git_output))

        def apply(tree: Path, *, writable: bool) -> None:
            for directory, names, files in os.walk(tree):
                directory_path = Path(directory)
                for target in (directory_path, *(directory_path / name for name in (*names, *files))):
                    if target.is_symlink():
                        continue
                    info = target.stat()
                    if info.st_uid != os.geteuid():
                        # Files from an earlier worker turn inherit the shared
                        # setgid group and 0007 umask; only their owner may chmod.
                        continue
                    owner = stat.S_IMODE(info.st_mode) & 0o700
                    group = owner >> 3 if writable else (owner & 0o500) >> 3
                    special = stat.S_ISGID if writable and target.is_dir() else 0
                    target.chmod(owner | group | special)

        apply(root, writable=True)
        # The task worker may read, but must not alter, the worktree pointer.
        dot_git = root / ".git"
        if dot_git.is_file() and dot_git.stat().st_uid == os.geteuid():
            dot_git.chmod(0o640)
        apply(common_git_dir, writable=False)
        apply(git_dir, writable=False)

    def verify_integrity(self, job: Job, worktree: Path) -> None:
        root = self._inside(worktree)
        expected_root = self._inside(self.root / "runs" / job.id)
        if root != expected_root:
            raise WorkspaceError(f"job worktree path changed: expected {expected_root}, observed {root}")
        dot_git = root / ".git"
        if not dot_git.is_file() or dot_git.stat().st_uid != os.geteuid():
            raise WorkspaceError("worktree .git pointer was replaced by the agent user")
        match = re.fullmatch(r"gitdir:\s*(.+)\s*", dot_git.read_text(errors="replace"))
        if not match:
            raise WorkspaceError("worktree .git pointer is invalid")
        git_dir = Path(match.group(1)).resolve()
        repository_git = self._inside(self.root / "repositories" / self._repo_key(job.repository) / ".git")
        worktree_metadata = repository_git / "worktrees"
        if not git_dir.is_relative_to(worktree_metadata) or not git_dir.is_dir():
            raise WorkspaceError("worktree Git metadata escaped the repository cache")
        mode = stat.S_IMODE(git_dir.stat().st_mode)
        common_mode = stat.S_IMODE(repository_git.stat().st_mode)
        if (
            git_dir.stat().st_uid != os.geteuid()
            or mode & stat.S_IWGRP
            or repository_git.stat().st_uid != os.geteuid()
            or common_mode & stat.S_IWGRP
        ):
            raise WorkspaceError("worktree Git metadata ownership or permissions changed")

    @staticmethod
    def _inside_script(worktree: Path, script: str) -> Path:
        if Path(script).is_absolute() or ".." in Path(script).parts:
            raise WorkspaceError("setup script must be a repository-relative path")
        target = (worktree / script).resolve()
        if not target.is_relative_to(worktree.resolve()):
            raise WorkspaceError("setup script escapes the worktree")
        return target

    @staticmethod
    def has_changes(worktree: Path) -> bool:
        process = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree,
            env=validation_environment(),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if process.returncode != 0:
            raise WorkspaceError(redact(process.stderr))
        return bool(process.stdout.strip())

    @staticmethod
    def commits_ahead(worktree: Path, default_branch: str) -> int:
        process = subprocess.run(
            ["git", "rev-list", "--count", f"origin/{default_branch}..HEAD"],
            cwd=worktree,
            env=validation_environment(),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if process.returncode != 0 or not process.stdout.strip().isdigit():
            raise WorkspaceError(f"unable to compare implementation branch: {redact(process.stderr)}")
        return int(process.stdout.strip())

    @staticmethod
    def remote_matches(worktree: Path, repository: str, branch: str) -> bool:
        local = _run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()
        remote = WorkspaceManager.github_remote(repository)
        process = subprocess.run(
            ["git", "ls-remote", "--heads", remote, f"refs/heads/{branch}"],
            cwd=worktree,
            env=orchestrator_environment(),
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if process.returncode != 0:
            raise WorkspaceError(f"unable to inspect remote branch: {redact(process.stderr)}")
        if not process.stdout.strip():
            return False
        return process.stdout.split()[0] == local

    @staticmethod
    def commit(worktree: Path, issue_number: int) -> str:
        _run(["git", "add", "--all"], cwd=worktree)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=worktree,
            env=validation_environment(),
            check=False,
        ).returncode
        if staged == 0:
            raise WorkspaceError("the implementation produced no committable changes")
        if staged != 1:
            raise WorkspaceError("unable to inspect staged changes")
        _run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "user.name=OpenHands Symphony",
                "-c",
                "user.email=openhands-symphony@localhost",
                "commit",
                "-m",
                f"Implement issue #{issue_number}",
            ],
            cwd=worktree,
            timeout=300,
        )
        return _run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()

    @staticmethod
    def push(worktree: Path, repository: str, branch: str) -> None:
        remote = WorkspaceManager.github_remote(repository)
        _run(
            ["git", "push", remote, f"HEAD:refs/heads/{branch}"],
            cwd=worktree,
            timeout=900,
            env=orchestrator_environment(),
        )

    @staticmethod
    def github_remote(repository: str) -> str:
        WorkspaceManager._repo_key(repository)
        return f"https://github.com/{repository}.git"


def run_validation(
    command: tuple[str, ...],
    worktree: Path,
    timeout_seconds: int,
    *,
    run_as_user: str = "",
) -> ValidationResult:
    if not command:
        raise WorkspaceError("empty validation command")
    started = utcnow()
    try:
        process = subprocess.run(
            validation_argv(command, run_as_user),
            cwd=worktree,
            env=validation_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
        return ValidationResult(command, process.returncode, started, utcnow(), redact(process.stdout))
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return ValidationResult(command, None, started, utcnow(), redact(output), timed_out=True)
