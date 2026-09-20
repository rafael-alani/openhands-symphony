from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from ideasync.cli import main
from ideasync.paths import AppPaths

REPOSITORY = "acme/pantry-pilot"
SPEC = b"""---
symphony: idea
repo: acme/pantry-pilot
---

# Pantry Pilot

## First feature

First wish.
"""
PROGRESS = b"""---
symphony: idea
repo: acme/pantry-pilot
---

# Pantry Pilot

## First feature

> **not started**
>
> The first wish has not been implemented yet.
>
> ![Initial state](assets/first-feature.png)

First wish.
"""
IDEA_TOML = b"""provider = "codex"

[preview]
start = ["python", "-m", "http.server", "{port}"]
port = 4317
health_path = "/"
startup_timeout_seconds = 10
"""


def git(*arguments: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        env=environment,
        check=check,
        capture_output=True,
        text=True,
    )


def configure_identity(repository: Path) -> None:
    git("config", "user.name", "Test User", cwd=repository)
    git("config", "user.email", "test@example.invalid", cwd=repository)


def create_remote(root: Path) -> Path:
    source = root / "seed"
    remote = root / "remote.git"
    source.mkdir(parents=True)
    git("init", "-b", "main", cwd=source)
    configure_identity(source)
    (source / "idea" / "assets").mkdir(parents=True)
    (source / ".symphony").mkdir()
    (source / "idea" / "SPEC.md").write_bytes(SPEC)
    (source / "idea" / "PROGRESS.md").write_bytes(PROGRESS)
    (source / "idea" / "assets" / "first-feature.png").write_bytes(b"initial-png")
    (source / ".symphony" / "idea.toml").write_bytes(IDEA_TOML)
    (source / "README.md").write_text("# Test idea\n", encoding="utf-8")
    git("add", ".symphony/idea.toml", "README.md", "idea/SPEC.md", "idea/PROGRESS.md", "idea/assets/first-feature.png", cwd=source)
    git("commit", "-m", "seed idea", cwd=source)
    git("init", "--bare", "--initial-branch=main", str(remote), cwd=root)
    git("remote", "add", "origin", str(remote), cwd=source)
    git("push", "-u", "origin", "main", cwd=source)
    return remote


def clone_agent(remote: Path, destination: Path) -> Path:
    git("clone", str(remote), str(destination), cwd=destination.parent)
    configure_identity(destination)
    return destination


def commit_and_push(repository: Path, message: str) -> None:
    git("add", "--all", cwd=repository)
    git("commit", "-m", message, cwd=repository)
    git("push", "origin", "main", cwd=repository)


def graduate_remote(harness: Harness) -> None:
    agent = clone_agent(harness.remote, harness.root / "graduator")
    archive = agent / "archive" / "ideas" / "accepted-spec"
    (archive / "idea" / "assets").mkdir(parents=True)
    (archive / ".symphony").mkdir(parents=True)
    for source, destination in (
        ("idea/SPEC.md", "archive/ideas/accepted-spec/idea/SPEC.md"),
        ("idea/PROGRESS.md", "archive/ideas/accepted-spec/idea/PROGRESS.md"),
        ("idea/assets/first-feature.png", "archive/ideas/accepted-spec/idea/assets/first-feature.png"),
        (".symphony/idea.toml", "archive/ideas/accepted-spec/.symphony/idea.toml"),
    ):
        git("mv", source, destination, cwd=agent)
    commit_and_push(agent, "graduate idea to Tier 1")


@dataclass
class Invocation:
    code: int
    stdout: str
    stderr: str


@dataclass
class Harness:
    root: Path
    data: Path
    vault: Path
    remote: Path
    capture: pytest.CaptureFixture[str]

    @property
    def paths(self) -> AppPaths:
        return AppPaths.from_value(self.data)

    @property
    def managed_clone(self) -> Path:
        return self.paths.clone_for(REPOSITORY)

    @property
    def vault_directory(self) -> Path:
        return self.vault / "pantry-pilot"

    def invoke(self, *arguments: str) -> Invocation:
        code = main(["--data-dir", str(self.data), *arguments])
        captured = self.capture.readouterr()
        return Invocation(code, captured.out, captured.err)


@pytest.fixture
def harness_factory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Callable[..., Harness]:
    counter = 0

    def factory(*, quiet_period: float = 0) -> Harness:
        nonlocal counter
        counter += 1
        root = tmp_path / f"case-{counter}"
        root.mkdir()
        remote = create_remote(root)
        harness = Harness(root=root, data=root / "data", vault=root / "vault", remote=remote, capture=capsys)
        initialized = harness.invoke(
            "init",
            "--vault",
            str(harness.vault),
            "--quiet-period-seconds",
            str(quiet_period),
            "--preview-host",
            "ideas-vm",
        )
        assert initialized.code == 0, initialized.stderr
        added = harness.invoke("add", REPOSITORY, "--remote", str(remote))
        assert added.code == 0, added.stderr
        return harness

    return factory
