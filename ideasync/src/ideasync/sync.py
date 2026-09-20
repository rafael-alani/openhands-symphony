from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ideasync.config import AppConfig, RepositoryConfig
from ideasync.contract import read_frontmatter, read_preview_contract
from ideasync.errors import ContractError, GitError, IdeasyncError
from ideasync.fs import FileChanges, atomic_write, copy_file, mirror_tree, same_content
from ideasync.git import Git
from ideasync.paths import AppPaths, ensure_within
from ideasync.runtime import Notifier, StructuredLogger, repository_lock

SPEC_PATH = Path("idea/SPEC.md")
PROGRESS_PATH = Path("idea/PROGRESS.md")
ASSETS_PATH = Path("idea/assets")
GRADUATION_ARCHIVE_PATH = Path("archive/ideas")
SPEC_COMMIT_MESSAGE = "ideasync: update idea spec"


@dataclass(frozen=True)
class SyncResult:
    repository: str
    state: str
    detail: str

    @property
    def failed(self) -> bool:
        return self.state == "failed"


@dataclass(frozen=True)
class SyncSummary:
    results: tuple[SyncResult, ...]
    dry_run: bool

    @property
    def failed(self) -> bool:
        return any(result.failed for result in self.results)


def discover_vault_specs(vault: Path) -> dict[str, Path]:
    if not vault.is_dir():
        raise ContractError(f"vault does not exist or is not a directory: {vault}")
    discovered: dict[str, Path] = {}
    for path in sorted(vault.rglob("SPEC.md")):
        relative = path.relative_to(vault)
        if relative.parts and relative.parts[0] == "_ideasync":
            continue
        ensure_within(vault, path)
        frontmatter = read_frontmatter(path)
        key = frontmatter.repository.casefold()
        if key in discovered:
            raise ContractError(
                f"duplicate vault route {frontmatter.repository}: {discovered[key]} and {path}"
            )
        discovered[key] = path
    return discovered


def validate_routed_file(path: Path, repository: str) -> None:
    frontmatter = read_frontmatter(path)
    if frontmatter.repository.casefold() != repository.casefold():
        raise ContractError(f"{path}: routes to {frontmatter.repository}, expected {repository}")


def graduation_archive(clone: Path, repository: str) -> Path | None:
    """Return a valid archived Ideas contract when the active one was graduated."""

    active_contract = (clone / SPEC_PATH, clone / PROGRESS_PATH, clone / ASSETS_PATH, clone / ".symphony/idea.toml")
    if any(path.exists() or path.is_symlink() for path in active_contract):
        return None
    archive_root = clone / GRADUATION_ARCHIVE_PATH
    if archive_root.is_symlink() or not archive_root.is_dir():
        return None
    ensure_within(clone, archive_root)
    for candidate in sorted(archive_root.iterdir(), reverse=True):
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        ensure_within(clone, candidate)
        archived_spec = candidate / SPEC_PATH
        archived_runtime = candidate / ".symphony/idea.toml"
        try:
            validate_routed_file(archived_spec, repository)
            read_preview_contract(archived_runtime)
        except ContractError:
            continue
        return candidate
    return None


class SyncEngine:
    def __init__(
        self,
        paths: AppPaths,
        config: AppConfig,
        *,
        git: Git | None = None,
        logger: StructuredLogger | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.paths = paths
        self.config = config
        self.git = git or Git()
        self.logger = logger or StructuredLogger(paths.data_dir, paths.log_file)
        self.notifier = notifier or Notifier()
        self.clock = clock

    def sync(self, selected: str | None = None, *, dry_run: bool = False) -> SyncSummary:
        try:
            repositories = self._select_repositories(selected)
        except IdeasyncError as exc:
            return self._finish((SyncResult(selected or "-", "failed", str(exc)),), dry_run=dry_run)
        try:
            inventory = discover_vault_specs(self.config.vault)
        except IdeasyncError as exc:
            results = tuple(SyncResult(repository.name, "failed", str(exc)) for repository in repositories)
            if not results:
                results = (SyncResult("-", "failed", str(exc)),)
            return self._finish(results, dry_run=dry_run)

        results: list[SyncResult] = []
        for repository in repositories:
            if dry_run:
                try:
                    results.append(self._sync_repository(repository, inventory, dry_run=True))
                except IdeasyncError as exc:
                    results.append(SyncResult(repository.name, "failed", str(exc)))
                continue
            try:
                with repository_lock(self.paths.data_dir, self.paths.lock_for(repository.name), repository.name):
                    self.logger.write("sync_started", repository=repository.name)
                    result = self._sync_repository(repository, inventory, dry_run=False)
            except IdeasyncError as exc:
                result = SyncResult(repository.name, "failed", str(exc))
            results.append(result)
            self.logger.write(
                "sync_finished",
                level="error" if result.failed else "info",
                repository=repository.name,
                state=result.state,
                message=result.detail,
            )
        return self._finish(tuple(results), dry_run=dry_run)

    def _select_repositories(self, selected: str | None) -> tuple[RepositoryConfig, ...]:
        if selected is None:
            return self.config.repositories
        repository = self.config.repository(selected)
        if repository is None:
            raise IdeasyncError(f"repository is not configured: {selected}")
        return (repository,)

    def _finish(self, results: tuple[SyncResult, ...], *, dry_run: bool) -> SyncSummary:
        summary = SyncSummary(results=results, dry_run=dry_run)
        if dry_run:
            return summary
        self.logger.write(
            "sync_summary",
            level="error" if summary.failed else "info",
            failed=summary.failed,
            results=[
                {"repository": result.repository, "state": result.state, "message": result.detail}
                for result in results
            ],
        )
        status_error: str | None = None
        try:
            self._write_status(summary)
        except Exception as exc:  # status failure must become visible through the other two channels
            status_error = f"could not write vault status: {exc}"
            self.logger.write("status_write_failed", level="error", message=status_error)
        failures = [result for result in results if result.failed]
        if status_error:
            failures.append(SyncResult("_ideasync", "failed", status_error))
            summary = SyncSummary(results=tuple((*results, failures[-1])), dry_run=False)
        if failures:
            message = "; ".join(f"{result.repository}: {result.detail}" for result in failures)
            try:
                self.notifier.failure(message)
            except Exception as exc:
                self.logger.write("notification_failed", level="error", message=str(exc))
        return summary

    def _write_status(self, summary: SyncSummary) -> None:
        overall = "failed" if summary.failed else "ok"
        lines = [
            "# ideasync status",
            "",
            f"Updated: {datetime.now(UTC).isoformat()}",
            f"Overall: **{overall}**",
            "",
            "| Repository | State | Detail |",
            "|---|---|---|",
        ]
        if not summary.results:
            lines.append("| — | ok | No repositories configured. |")
        for result in summary.results:
            detail = result.detail.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| `{result.repository}` | {result.state} | {detail} |")
        payload = ("\n".join(lines) + "\n").encode()
        atomic_write(self.config.vault, self.config.vault / "_ideasync" / "STATUS.md", payload)

    def _sync_repository(
        self,
        repository: RepositoryConfig,
        inventory: dict[str, Path],
        *,
        dry_run: bool,
    ) -> SyncResult:
        clone = self.paths.clone_for(repository.name)
        if not clone.is_dir():
            raise GitError(f"managed clone is missing: {clone}; run `ideasync add {repository.name}`")
        if self.git.origin_url(clone) != repository.remote:
            raise GitError("managed clone origin differs from configured remote")
        if self.git.current_branch(clone) != repository.branch:
            raise GitError(f"managed clone is not on configured branch {repository.branch}")
        self.git.assert_clean(clone)
        remote_base, pending = self._update_clone(repository, clone, dry_run=dry_run)
        archived = graduation_archive(clone, repository.name)
        if archived is not None:
            return SyncResult(
                repository.name,
                "retired",
                f"Ideas contract graduated to {archived.relative_to(clone)}; "
                f"vault preserved; run `ideasync remove {repository.name}` to deregister the managed clone",
            )
        self._validate_clone_files(repository, clone)

        vault_spec = inventory.get(repository.name.casefold())
        if vault_spec is None:
            raise ContractError(f"vault has no SPEC.md routed to {repository.name}")
        validate_routed_file(vault_spec, repository.name)
        vault_directory = vault_spec.parent

        inbound = self._copy_inbound(clone, vault_directory, dry_run=dry_run)
        vault_bytes = vault_spec.read_bytes()
        clone_spec = clone / SPEC_PATH
        outbound = "spec unchanged"

        if pending:
            if not same_content(clone_spec, vault_bytes):
                raise GitError("managed clone has an unpushed spec commit, but the vault changed again; manual recovery required")
            if dry_run:
                outbound = "would retry the pending spec push"
            else:
                self._push_with_one_rebase(repository, clone, remote_base, vault_bytes)
                outbound = "recovered and pushed the pending spec commit"
        elif not same_content(clone_spec, vault_bytes):
            age = self.clock() - vault_spec.stat().st_mtime
            if age < self.config.quiet_period_seconds:
                remaining = self.config.quiet_period_seconds - max(age, 0)
                outbound = f"spec quiet period active ({remaining:.1f}s remaining)"
            elif dry_run:
                outbound = "would commit and push idea/SPEC.md"
            else:
                atomic_write(clone, clone_spec, vault_bytes)
                self.git.add_spec(clone)
                self.git.commit_spec(clone, SPEC_COMMIT_MESSAGE)
                self._push_with_one_rebase(repository, clone, remote_base, vault_bytes)
                outbound = "committed and pushed idea/SPEC.md"

        inbound_detail = self._describe_inbound(inbound, dry_run=dry_run)
        state = "planned" if dry_run else "ok"
        return SyncResult(repository.name, state, f"{inbound_detail}; {outbound}")

    def _update_clone(self, repository: RepositoryConfig, clone: Path, *, dry_run: bool) -> tuple[str, bool]:
        remote_ref = self.git.remote_ref(repository.branch)
        if not dry_run:
            self.git.fetch(clone)
        local = self.git.rev_parse(clone)
        remote = self.git.rev_parse(clone, remote_ref)
        if local == remote:
            return remote, False
        if self.git.is_ancestor(clone, local, remote):
            if dry_run:
                raise GitError("managed clone needs a fetch/fast-forward; dry-run does not mutate Git state")
            self.git.fast_forward(clone, remote_ref)
            return self.git.rev_parse(clone, remote_ref), False
        if self.git.is_ancestor(clone, remote, local):
            self._validate_pending_spec_commit(clone, remote)
            return remote, True
        raise GitError("managed clone and remote have diverged; refusing non-fast-forward recovery")

    def _validate_pending_spec_commit(self, clone: Path, remote_base: str) -> None:
        if self.git.ahead_count(clone, remote_base) != 1:
            raise GitError("managed clone is ahead by more than one commit; refusing automatic recovery")
        if self.git.changed_paths(clone, "HEAD") != [SPEC_PATH.as_posix()]:
            raise GitError("managed clone's pending commit is not limited to idea/SPEC.md")
        if self.git.subject(clone) != SPEC_COMMIT_MESSAGE:
            raise GitError("managed clone's pending commit does not have the ideasync commit message")

    def _validate_clone_files(self, repository: RepositoryConfig, clone: Path) -> None:
        validate_routed_file(clone / SPEC_PATH, repository.name)
        progress = clone / PROGRESS_PATH
        if progress.exists() or progress.is_symlink():
            validate_routed_file(progress, repository.name)

    def _copy_inbound(self, clone: Path, vault_directory: Path, *, dry_run: bool) -> FileChanges:
        changes = copy_file(
            clone / PROGRESS_PATH,
            vault_directory / "PROGRESS.md",
            source_root=clone,
            target_root=self.config.vault,
            label="PROGRESS.md",
            dry_run=dry_run,
        )
        changes.extend(
            mirror_tree(
                clone / ASSETS_PATH,
                vault_directory / "assets",
                source_root=clone,
                target_root=self.config.vault,
                dry_run=dry_run,
            )
        )
        return changes

    @staticmethod
    def _describe_inbound(changes: FileChanges, *, dry_run: bool) -> str:
        if not changes.changed:
            return "inbound unchanged"
        prefix = "would update" if dry_run else "updated"
        parts = [*changes.copied, *(f"removed {path}" for path in changes.removed)]
        return f"{prefix} {', '.join(parts)}"

    def _push_with_one_rebase(
        self,
        repository: RepositoryConfig,
        clone: Path,
        remote_base: str,
        vault_bytes: bytes,
    ) -> None:
        first = self.git.push(clone, repository.branch)
        if first.returncode == 0:
            return
        local_commit = self.git.rev_parse(clone)
        self.git.fetch(clone)
        remote_ref = self.git.remote_ref(repository.branch)
        latest_remote = self.git.rev_parse(clone, remote_ref)
        if self.git.is_ancestor(clone, local_commit, latest_remote):
            return
        if latest_remote == remote_base:
            detail = (first.stderr or first.stdout).strip()
            raise GitError(f"spec push failed; local commit preserved for retry: {detail}")
        if not self.git.is_ancestor(clone, remote_base, latest_remote):
            raise GitError("remote branch was rewritten during push; refusing to rebase or force")
        self._validate_pending_spec_commit(clone, remote_base)
        rebased = self.git.rebase(clone, remote_ref)
        if rebased.returncode != 0:
            self.git.abort_rebase(clone)
            atomic_write(clone, clone / SPEC_PATH, vault_bytes)
            raise GitError("push race rebase conflicted; vault SPEC is preserved locally and the remote was not changed")
        if not same_content(clone / SPEC_PATH, vault_bytes):
            atomic_write(clone, clone / SPEC_PATH, vault_bytes)
            raise GitError("push race changed SPEC during rebase; vault bytes are preserved locally and the remote was not changed")
        second = self.git.push(clone, repository.branch)
        if second.returncode != 0:
            detail = (second.stderr or second.stdout).strip()
            raise GitError(f"spec push lost a second race; stopped after one rebase: {detail}")
