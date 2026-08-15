from __future__ import annotations

import argparse
import math
import os
import shlex
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from ideasync import __version__
from ideasync.config import AppConfig, RepositoryConfig, load_config, save_config
from ideasync.contract import read_preview_contract, validate_repository
from ideasync.errors import ConfigError, ContractError, IdeasyncError
from ideasync.fs import atomic_write, copy_file, mirror_tree
from ideasync.git import Git
from ideasync.paths import AppPaths, ensure_within
from ideasync.runtime import StructuredLogger
from ideasync.scheduler import scheduler_for
from ideasync.sync import ASSETS_PATH, PROGRESS_PATH, SPEC_PATH, SyncEngine, discover_vault_specs, validate_routed_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ideasync", description="Sync ideas-tier files through dedicated clones.")
    parser.add_argument("--data-dir", help="Tool-owned data directory (or set IDEASYNC_DATA_DIR).")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="Initialize a data directory and vault contract.")
    initialize.add_argument("--vault", required=True, help="Dedicated ideas vault directory.")
    initialize.add_argument("--quiet-period-seconds", type=float, default=30.0)
    initialize.add_argument("--dry-run", action="store_true")

    add = subparsers.add_parser("add", help="Add one owner/repo and create its dedicated managed clone.")
    add.add_argument("repository")
    add.add_argument("--remote", help="Git remote URL; defaults to git@github.com:owner/repo.git.")
    add.add_argument("--dry-run", action="store_true")

    sync = subparsers.add_parser("sync", help="Run one sync pass for one or every configured repository.")
    sync.add_argument("repository", nargs="?")
    sync.add_argument("--dry-run", action="store_true")

    subparsers.add_parser("status", help="Print the last vault health report.")
    subparsers.add_parser("doctor", help="Check local configuration and managed clones without mutation.")

    open_command = subparsers.add_parser("open", help="Print the future preview's SSH forwarding command.")
    open_command.add_argument("repository")
    open_command.add_argument("--host", default="your-vm")

    install = subparsers.add_parser("install-schedule", help="Install the two-minute launchd schedule.")
    install.add_argument("--dry-run", action="store_true")
    uninstall = subparsers.add_parser("uninstall-schedule", help="Uninstall the launchd schedule.")
    uninstall.add_argument("--dry-run", action="store_true")
    return parser


def _paths_overlap(first: Path, second: Path) -> bool:
    resolved_first = first.resolve(strict=False)
    resolved_second = second.resolve(strict=False)
    return resolved_first == resolved_second or resolved_first.is_relative_to(resolved_second) or resolved_second.is_relative_to(resolved_first)


def command_init(paths: AppPaths, vault_value: str, quiet_period: float, *, dry_run: bool) -> str:
    if not math.isfinite(quiet_period) or quiet_period < 0:
        raise ConfigError("quiet period must be a finite, non-negative number")
    vault = Path(vault_value).expanduser().absolute()
    if _paths_overlap(paths.data_dir, vault):
        raise ConfigError("the vault and ideasync data directory must not contain one another")
    if paths.config_file.exists():
        raise ConfigError(f"ideasync is already initialized at {paths.config_file}")
    if dry_run:
        return f"would initialize data at {paths.data_dir} with vault {vault} and quiet period {quiet_period:g}s"
    for directory in paths.required_directories():
        directory.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    if not vault.is_dir():
        raise ConfigError(f"vault is not a directory: {vault}")
    config = AppConfig(vault=vault, quiet_period_seconds=quiet_period)
    save_config(paths, config)
    StructuredLogger(paths.data_dir, paths.log_file).write(
        "initialized", vault=str(vault), quiet_period_seconds=quiet_period
    )
    return f"initialized ideasync at {paths.data_dir}; vault: {vault}"


def _default_remote(repository: str) -> str:
    return f"git@github.com:{repository}.git"


def command_add(paths: AppPaths, repository_name: str, remote: str | None, *, dry_run: bool) -> str:
    validate_repository(repository_name)
    config = load_config(paths)
    if config.repository(repository_name) is not None:
        raise ConfigError(f"repository is already configured: {repository_name}")
    remote_value = remote or _default_remote(repository_name)
    destination = paths.clone_for(repository_name)
    if destination.exists():
        raise ConfigError(f"managed clone path already exists but is not configured: {destination}")
    if dry_run:
        return f"would clone {remote_value} into {destination} and bootstrap {repository_name} in {config.vault}"

    inventory = discover_vault_specs(config.vault)
    existing_vault_spec = inventory.get(repository_name.casefold())
    temporary_root = Path(tempfile.mkdtemp(prefix="add-", dir=paths.temp_dir))
    temporary_clone = temporary_root / "clone"
    git = Git()
    moved = False
    configured = False
    try:
        git.clone(remote_value, temporary_clone)
        git.configure_identity(temporary_clone)
        branch = git.current_branch(temporary_clone)
        validate_routed_file(temporary_clone / SPEC_PATH, repository_name)
        progress = temporary_clone / PROGRESS_PATH
        if progress.exists() or progress.is_symlink():
            validate_routed_file(progress, repository_name)
        read_preview_contract(temporary_clone / ".symphony" / "idea.toml")

        if existing_vault_spec is None:
            vault_directory = config.vault / repository_name.split("/", 1)[1]
            ensure_within(config.vault, vault_directory)
            if vault_directory.exists():
                if vault_directory.is_symlink() or not vault_directory.is_dir() or any(vault_directory.iterdir()):
                    raise ConfigError(f"refusing to bootstrap into non-empty vault directory: {vault_directory}")
        else:
            vault_directory = existing_vault_spec.parent
            validate_routed_file(existing_vault_spec, repository_name)

        copy_file(
            progress,
            vault_directory / "PROGRESS.md",
            source_root=temporary_clone,
            target_root=config.vault,
            label="PROGRESS.md",
            dry_run=True,
        )
        mirror_tree(
            temporary_clone / ASSETS_PATH,
            vault_directory / "assets",
            source_root=temporary_clone,
            target_root=config.vault,
            dry_run=True,
        )

        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary_clone, destination)
        moved = True
        vault_directory.mkdir(parents=True, exist_ok=True)
        if existing_vault_spec is None:
            atomic_write(config.vault, vault_directory / "SPEC.md", (destination / SPEC_PATH).read_bytes())
        copy_file(
            destination / PROGRESS_PATH,
            vault_directory / "PROGRESS.md",
            source_root=destination,
            target_root=config.vault,
            label="PROGRESS.md",
            dry_run=False,
        )
        mirror_tree(
            destination / ASSETS_PATH,
            vault_directory / "assets",
            source_root=destination,
            target_root=config.vault,
            dry_run=False,
        )
        updated = config.with_repository(RepositoryConfig(repository_name, remote_value, branch))
        save_config(paths, updated)
        configured = True
        StructuredLogger(paths.data_dir, paths.log_file).write(
            "repository_added", repository=repository_name, branch=branch, clone=str(destination)
        )
        return f"added {repository_name}; managed clone: {destination}; vault directory: {vault_directory}"
    finally:
        if moved and not configured:
            ensure_within(paths.data_dir, destination)
            shutil.rmtree(destination, ignore_errors=True)
        shutil.rmtree(temporary_root, ignore_errors=True)


def command_sync(paths: AppPaths, repository: str | None, *, dry_run: bool) -> tuple[int, str]:
    if repository is not None:
        validate_repository(repository)
    config = load_config(paths)
    summary = SyncEngine(paths, config).sync(repository, dry_run=dry_run)
    lines = []
    for result in summary.results:
        lines.append(f"{result.repository}: {result.state}: {result.detail}")
    if not lines:
        lines.append("no repositories configured")
    return (1 if summary.failed else 0), "\n".join(lines)


def command_status(paths: AppPaths) -> str:
    config = load_config(paths)
    status_path = config.vault / "_ideasync" / "STATUS.md"
    if not status_path.is_file() or status_path.is_symlink():
        return "No sync status has been written yet."
    return status_path.read_text(encoding="utf-8", errors="strict").rstrip()


def command_doctor(paths: AppPaths) -> tuple[int, str]:
    checks: list[tuple[bool, str]] = []
    checks.append((shutil.which("git") is not None, "git executable is available"))
    try:
        config = load_config(paths)
    except IdeasyncError as exc:
        checks.append((False, str(exc)))
        return 1, _format_checks(checks)
    checks.append((config.vault.is_dir(), f"vault exists: {config.vault}"))
    checks.append((os.access(config.vault, os.R_OK | os.W_OK), "vault is readable and writable"))
    try:
        inventory = discover_vault_specs(config.vault)
        checks.append((True, f"validated {len(inventory)} routed vault spec(s)"))
    except IdeasyncError as exc:
        checks.append((False, str(exc)))
        inventory = {}
    git = Git()
    for repository in config.repositories:
        clone = paths.clone_for(repository.name)
        try:
            if not clone.is_dir():
                raise ConfigError(f"managed clone is missing: {clone}")
            if git.current_branch(clone) != repository.branch:
                raise ConfigError(f"managed clone branch differs from configured branch {repository.branch}")
            if git.origin_url(clone) != repository.remote:
                raise ConfigError("managed clone origin differs from configured remote")
            git.assert_clean(clone)
            validate_routed_file(clone / SPEC_PATH, repository.name)
            progress = clone / PROGRESS_PATH
            if progress.exists() or progress.is_symlink():
                validate_routed_file(progress, repository.name)
            read_preview_contract(clone / ".symphony" / "idea.toml")
            if repository.name.casefold() not in inventory:
                raise ContractError("no vault spec routes to this repository")
        except IdeasyncError as exc:
            checks.append((False, f"{repository.name}: {exc}"))
        else:
            checks.append((True, f"{repository.name}: clone and contracts are healthy"))
    return (0 if all(ok for ok, _ in checks) else 1), _format_checks(checks)


def _format_checks(checks: list[tuple[bool, str]]) -> str:
    return "\n".join(f"[{'ok' if ok else 'FAIL'}] {message}" for ok, message in checks)


def command_open(paths: AppPaths, repository_name: str, host: str) -> str:
    validate_repository(repository_name)
    config = load_config(paths)
    repository = config.repository(repository_name)
    if repository is None:
        raise ConfigError(f"repository is not configured: {repository_name}")
    preview = read_preview_contract(paths.clone_for(repository.name) / ".symphony" / "idea.toml")
    command = ["ssh", "-N", "-L", f"{preview.port}:127.0.0.1:{preview.port}", host]
    return f"Preview management is not implemented yet. When the VM preview exists, run:\n{shlex.join(command)}"


def _dispatch(arguments: argparse.Namespace, paths: AppPaths) -> tuple[int, str]:
    if arguments.command == "init":
        return 0, command_init(paths, arguments.vault, arguments.quiet_period_seconds, dry_run=arguments.dry_run)
    if arguments.command == "add":
        return 0, command_add(paths, arguments.repository, arguments.remote, dry_run=arguments.dry_run)
    if arguments.command == "sync":
        return command_sync(paths, arguments.repository, dry_run=arguments.dry_run)
    if arguments.command == "status":
        return 0, command_status(paths)
    if arguments.command == "doctor":
        return command_doctor(paths)
    if arguments.command == "open":
        return 0, command_open(paths, arguments.repository, arguments.host)
    if arguments.command in {"install-schedule", "uninstall-schedule"}:
        load_config(paths)
        scheduler = scheduler_for(paths)
        if arguments.command == "install-schedule":
            output = scheduler.install(dry_run=arguments.dry_run)
        else:
            output = scheduler.uninstall(dry_run=arguments.dry_run)
        if not arguments.dry_run:
            StructuredLogger(paths.data_dir, paths.log_file).write(
                "schedule_installed" if arguments.command == "install-schedule" else "schedule_uninstalled"
            )
        return 0, output
    raise AssertionError(f"unhandled command: {arguments.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    paths = AppPaths.from_value(arguments.data_dir)
    try:
        code, output = _dispatch(arguments, paths)
    except (IdeasyncError, OSError) as exc:
        print(f"ideasync: {exc}", file=sys.stderr)
        return 1
    if output:
        print(output)
    return code
