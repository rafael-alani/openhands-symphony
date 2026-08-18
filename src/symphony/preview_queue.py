from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .intake import validate_repository_name
from .models import IdeaRun, utcnow
from .store import Store
from .validation import redact, validation_environment
from .workspace import WorkspaceError

COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preview_repository_key(repository: str) -> str:
    validate_repository_name(repository)
    readable = repository.replace("/", "--")
    suffix = hashlib.sha256(repository.encode()).hexdigest()[:10]
    return f"{readable}-{suffix}"


@dataclass(frozen=True)
class PreviewRequest:
    repository: str
    commit: str
    archive: str
    archive_sha256: str
    setup_script: str
    queued_at: str


@dataclass(frozen=True)
class PreviewStatus:
    repository: str
    desired_commit: str | None
    active_commit: str | None
    last_good_commit: str | None
    state: str
    port: int | None
    health_url: str | None
    detail: str
    updated_at: str


class PreviewQueue:
    """Immutable handoff from the credentialed orchestrator to the preview account."""

    def __init__(self, root: Path, workspace_root: Path):
        self.root = root
        self.workspace_root = workspace_root.resolve()

    @property
    def queue_dir(self) -> Path:
        return self.root / "queue"

    @property
    def archive_dir(self) -> Path:
        return self.root / "archives"

    @property
    def status_dir(self) -> Path:
        return self.root / "status"

    @property
    def control_dir(self) -> Path:
        return self.root / "control"

    def _prepare_handoff_directories(self) -> None:
        for directory in (self.root, self.queue_dir, self.archive_dir, self.status_dir, self.control_dir):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                directory.chmod(0o2770)
            except PermissionError:
                # Production ownership and setgid policy are installed ahead of time.
                # A service account may create entries without owning their parent.
                pass

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, object]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o660)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def publish_allowlist(self, repositories: tuple[str, ...]) -> None:
        for repository in repositories:
            validate_repository_name(repository)
        self._prepare_handoff_directories()
        self._atomic_json(
            self.control_dir / "allowlist.json",
            {"repositories": sorted(repositories), "updated_at": utcnow()},
        )

    @staticmethod
    def _repository_cache(root: Path, repository: str) -> Path:
        return root / "repositories" / repository.replace("/", "--")

    def _git_source(self, run: IdeaRun) -> Path:
        candidates = []
        if run.worktree:
            candidates.append(Path(run.worktree))
        candidates.append(self._repository_cache(self.workspace_root, run.repository))
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if not resolved.is_relative_to(self.workspace_root):
                continue
            exists = subprocess.run(
                ["git", "-C", str(resolved), "cat-file", "-e", f"{run.published_commit}^{{commit}}"],
                env=validation_environment(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if exists.returncode == 0:
                return resolved
        raise WorkspaceError(f"published preview commit is absent from the local repository cache: {run.repository}")

    def status(self, repository: str) -> PreviewStatus | None:
        path = self.status_dir / f"{preview_repository_key(repository)}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("repository") != repository:
                return None
            return PreviewStatus(**payload)
        except (OSError, ValueError, TypeError):
            return None

    def queued_request(self, repository: str) -> PreviewRequest | None:
        path = self.queue_dir / f"{preview_repository_key(repository)}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("repository") != repository:
                return None
            return PreviewRequest(**payload)
        except (OSError, ValueError, TypeError):
            return None

    def enqueue(self, run: IdeaRun, setup_script: str) -> bool:
        commit = run.published_commit or ""
        if not COMMIT_PATTERN.fullmatch(commit):
            raise WorkspaceError("preview deployment requires a full published commit hash")
        validate_repository_name(run.repository)
        known_status = self.status(run.repository)
        if known_status and known_status.desired_commit == commit:
            return False
        queued = self.queued_request(run.repository)
        if queued and queued.commit == commit:
            return False

        self._prepare_handoff_directories()
        key = preview_repository_key(run.repository)
        repository_archives = self.archive_dir / key
        repository_archives.mkdir(parents=True, exist_ok=True)
        try:
            repository_archives.chmod(0o2770)
        except PermissionError:
            pass
        archive = repository_archives / f"{commit}.tar"
        if not archive.is_file():
            source = self._git_source(run)
            descriptor, temporary = tempfile.mkstemp(prefix=f".{commit}.", dir=repository_archives)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    process = subprocess.run(
                        ["git", "-C", str(source), "archive", "--format=tar", commit],
                        env=validation_environment(),
                        stdout=handle,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                if process.returncode != 0:
                    detail = process.stderr.decode(errors="replace") if isinstance(process.stderr, bytes) else process.stderr
                    raise WorkspaceError(f"unable to archive preview commit: {redact(detail or '')}")
                os.chmod(temporary, 0o640)
                os.replace(temporary, archive)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        archive_sha = sha256_file(archive)
        request = PreviewRequest(
            repository=run.repository,
            commit=commit,
            archive=str(archive.relative_to(self.root)),
            archive_sha256=archive_sha,
            setup_script=setup_script,
            queued_at=utcnow(),
        )
        self._atomic_json(self.queue_dir / f"{key}.json", asdict(request))
        return True

    def sync_store(self, store: Store, repositories: tuple[str, ...]) -> None:
        for repository in repositories:
            status = self.status(repository)
            if status is None or store.get_idea_project(repository) is None:
                continue
            latest = store.latest_published_idea_run(repository)
            state = status.state
            if latest and latest.published_commit != status.desired_commit:
                state = "pending"
            store.update_idea_preview(
                repository,
                state,
                last_good_commit=status.last_good_commit,
            )
