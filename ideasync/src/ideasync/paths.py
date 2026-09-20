from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from ideasync.errors import ConfigError


def default_data_dir() -> Path:
    override = os.environ.get("IDEASYNC_DATA_DIR")
    if override:
        return Path(override).expanduser().absolute()
    return (Path.home() / "Library" / "Application Support" / "ideasync").absolute()


def repository_slug(repository: str) -> str:
    readable = repository.replace("/", "--")
    suffix = hashlib.sha256(repository.encode("utf-8")).hexdigest()[:10]
    return f"{readable}-{suffix}"


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path

    @classmethod
    def from_value(cls, value: str | Path | None) -> AppPaths:
        path = default_data_dir() if value is None else Path(value).expanduser().absolute()
        return cls(data_dir=path)

    @property
    def config_file(self) -> Path:
        return self.data_dir / "config.toml"

    @property
    def clones_dir(self) -> Path:
        return self.data_dir / "clones"

    @property
    def retired_clones_dir(self) -> Path:
        return self.data_dir / "retired-clones"

    @property
    def locks_dir(self) -> Path:
        return self.data_dir / "locks"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "logs" / "ideasync.jsonl"

    @property
    def launchd_dir(self) -> Path:
        return self.data_dir / "launchd"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def clone_for(self, repository: str) -> Path:
        return self.clones_dir / repository_slug(repository)

    def retired_clone_for(self, repository: str) -> Path:
        return self.retired_clones_dir / repository_slug(repository)

    def lock_for(self, repository: str) -> Path:
        return self.locks_dir / f"{repository_slug(repository)}.lock"

    def required_directories(self) -> tuple[Path, ...]:
        return (
            self.data_dir,
            self.clones_dir,
            self.retired_clones_dir,
            self.locks_dir,
            self.log_file.parent,
            self.launchd_dir,
            self.temp_dir,
        )


def ensure_within(root: Path, target: Path) -> None:
    resolved_root = root.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    if not resolved_target.is_relative_to(resolved_root):
        raise ConfigError(f"path escapes managed root {resolved_root}: {target}")
