from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ideasync.errors import ContractError
from ideasync.paths import ensure_within


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def same_content(path: Path, data: bytes) -> bool:
    return path.is_file() and not path.is_symlink() and content_hash(path.read_bytes()) == content_hash(data)


def atomic_write(root: Path, path: Path, data: bytes, *, mode: int = 0o644) -> bool:
    ensure_within(root, path)
    if path.is_symlink():
        raise ContractError(f"refusing to replace symlink: {path}")
    if same_content(path, data):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_within(root, path.parent)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return True


@dataclass
class FileChanges:
    copied: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.copied or self.removed)

    def extend(self, other: FileChanges) -> None:
        self.copied.extend(other.copied)
        self.removed.extend(other.removed)


def copy_file(
    source: Path,
    target: Path,
    *,
    source_root: Path,
    target_root: Path,
    label: str,
    dry_run: bool,
) -> FileChanges:
    ensure_within(source_root, source)
    ensure_within(target_root, target)
    changes = FileChanges()
    if not source.exists():
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                raise ContractError(f"expected a regular managed file: {target}")
            changes.removed.append(label)
            if not dry_run:
                target.unlink()
        return changes
    if source.is_symlink() or not source.is_file():
        raise ContractError(f"refusing to copy non-regular file: {source}")
    data = source.read_bytes()
    if target.is_symlink():
        raise ContractError(f"refusing to replace symlink: {target}")
    if not same_content(target, data):
        changes.copied.append(label)
        if not dry_run:
            atomic_write(target_root, target, data)
    return changes


def _tree_files(root: Path) -> dict[Path, Path]:
    if not root.exists():
        return {}
    if root.is_symlink() or not root.is_dir():
        raise ContractError(f"expected a regular directory, not a symlink: {root}")
    result: dict[Path, Path] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            candidate = current_path / directory
            if candidate.is_symlink():
                raise ContractError(f"refusing to traverse symlink: {candidate}")
        for filename in files:
            candidate = current_path / filename
            if candidate.is_symlink() or not candidate.is_file():
                raise ContractError(f"refusing to copy non-regular file: {candidate}")
            result[candidate.relative_to(root)] = candidate
    return result


def mirror_tree(source: Path, target: Path, *, source_root: Path, target_root: Path, dry_run: bool) -> FileChanges:
    ensure_within(source_root, source)
    ensure_within(target_root, target)
    source_files = _tree_files(source)
    target_files = _tree_files(target)
    changes = FileChanges()
    for relative, source_file in sorted(source_files.items(), key=lambda item: str(item[0])):
        target_file = target / relative
        label = f"assets/{relative.as_posix()}"
        changes.extend(
            copy_file(
                source_file,
                target_file,
                source_root=source_root,
                target_root=target_root,
                label=label,
                dry_run=dry_run,
            )
        )
    for relative, target_file in sorted(target_files.items(), key=lambda item: str(item[0]), reverse=True):
        if relative in source_files:
            continue
        changes.removed.append(f"assets/{relative.as_posix()}")
        if not dry_run:
            target_file.unlink()
    if not dry_run and target.exists():
        directories = sorted((path for path in target.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True)
        for directory in directories:
            if not any(directory.iterdir()):
                directory.rmdir()
        if not any(target.iterdir()):
            target.rmdir()
    return changes
