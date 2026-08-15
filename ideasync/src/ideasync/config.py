from __future__ import annotations

import json
import math
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from ideasync.contract import validate_repository
from ideasync.errors import ConfigError, ContractError
from ideasync.fs import atomic_write
from ideasync.paths import AppPaths


@dataclass(frozen=True)
class RepositoryConfig:
    name: str
    remote: str
    branch: str


@dataclass(frozen=True)
class AppConfig:
    vault: Path
    quiet_period_seconds: float = 30.0
    repositories: tuple[RepositoryConfig, ...] = ()
    version: int = 1

    def repository(self, name: str) -> RepositoryConfig | None:
        folded = name.casefold()
        return next((repository for repository in self.repositories if repository.name.casefold() == folded), None)

    def with_repository(self, repository: RepositoryConfig) -> AppConfig:
        if self.repository(repository.name) is not None:
            raise ConfigError(f"repository is already configured: {repository.name}")
        repositories = tuple(sorted((*self.repositories, repository), key=lambda item: item.name.casefold()))
        return replace(self, repositories=repositories)


def _expect_keys(raw: dict[str, object], allowed: set[str], context: str) -> None:
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise ConfigError(f"{context} contains unsupported keys: {', '.join(unexpected)}")


def load_config(paths: AppPaths) -> AppConfig:
    if not paths.config_file.is_file():
        raise ConfigError(f"ideasync is not initialized; missing {paths.config_file}")
    try:
        raw = tomllib.loads(paths.config_file.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid config {paths.config_file}: {exc}") from exc
    _expect_keys(raw, {"version", "vault", "quiet_period_seconds", "repositories"}, "config")
    version = raw.get("version")
    vault = raw.get("vault")
    quiet = raw.get("quiet_period_seconds", 30)
    repositories_raw = raw.get("repositories", [])
    if version != 1:
        raise ConfigError(f"unsupported config version: {version!r}")
    if not isinstance(vault, str) or not Path(vault).is_absolute():
        raise ConfigError("config vault must be an absolute path")
    if (
        isinstance(quiet, bool)
        or not isinstance(quiet, int | float)
        or not math.isfinite(quiet)
        or quiet < 0
    ):
        raise ConfigError("quiet_period_seconds must be a finite, non-negative number")
    if not isinstance(repositories_raw, list):
        raise ConfigError("repositories must be an array of tables")
    repositories: list[RepositoryConfig] = []
    names: set[str] = set()
    for index, entry in enumerate(repositories_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"repositories[{index}] must be a table")
        _expect_keys(entry, {"name", "remote", "branch"}, f"repositories[{index}]")
        name = entry.get("name")
        remote = entry.get("remote")
        branch = entry.get("branch")
        if not isinstance(name, str) or not isinstance(remote, str) or not remote or not isinstance(branch, str) or not branch:
            raise ConfigError(f"repositories[{index}] requires non-empty name, remote, and branch strings")
        try:
            validate_repository(name)
        except ContractError as exc:
            raise ConfigError(str(exc)) from exc
        if name.casefold() in names:
            raise ConfigError(f"duplicate repository in config: {name}")
        names.add(name.casefold())
        repositories.append(RepositoryConfig(name=name, remote=remote, branch=branch))
    return AppConfig(
        vault=Path(vault),
        quiet_period_seconds=float(quiet),
        repositories=tuple(repositories),
        version=1,
    )


def render_config(config: AppConfig) -> bytes:
    lines = [
        f"version = {config.version}",
        f"vault = {json.dumps(str(config.vault))}",
        f"quiet_period_seconds = {config.quiet_period_seconds:g}",
    ]
    for repository in config.repositories:
        lines.extend(
            [
                "",
                "[[repositories]]",
                f"name = {json.dumps(repository.name)}",
                f"remote = {json.dumps(repository.remote)}",
                f"branch = {json.dumps(repository.branch)}",
            ]
        )
    return ("\n".join(lines) + "\n").encode()


def save_config(paths: AppPaths, config: AppConfig) -> bool:
    return atomic_write(paths.data_dir, paths.config_file, render_config(config))
