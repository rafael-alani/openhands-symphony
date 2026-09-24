from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import FrameType

import httpx

from .ideas_contract import IdeaContractError, IdeaRuntime, parse_runtime, preview_argv, safe_path
from .models import utcnow
from .preview_queue import COMMIT_PATTERN, PreviewRequest, PreviewStatus, preview_repository_key, sha256_file
from .validation import redact


class PreviewManagerError(RuntimeError):
    pass


@dataclass
class ManagedPreview:
    repository: str
    commit: str
    release: Path
    runtime: IdeaRuntime
    process: subprocess.Popen[bytes]
    log_handle: object


class PreviewManager:
    """Credential-free owner of stable idea checkouts and preview processes."""

    def __init__(self, root: Path, *, poll_seconds: float = 2, releases_to_keep: int = 2):
        self.root = root.resolve()
        self.poll_seconds = poll_seconds
        self.releases_to_keep = max(1, releases_to_keep)
        self.queue_dir = self.root / "queue"
        self.archive_dir = self.root / "archives"
        self.status_dir = self.root / "status"
        self.control_dir = self.root / "control"
        self.projects_dir = self.root / "projects"
        self.home_dir = self.root / "home"
        self._active: dict[str, ManagedPreview] = {}
        self._stop = False
        self._lock_handle: object | None = None
        for directory in (
            self.root,
            self.queue_dir,
            self.archive_dir,
            self.status_dir,
            self.control_dir,
            self.projects_dir,
            self.home_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def acquire_lock(self) -> None:
        handle = (self.root / "manager.lock").open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise PreviewManagerError("another preview manager owns the state directory") from exc
        self._lock_handle = handle

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, object]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o640)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _project_dir(self, repository: str) -> Path:
        return self.projects_dir / preview_repository_key(repository)

    def _state_path(self, repository: str) -> Path:
        return self._project_dir(repository) / "state.json"

    def _status_path(self, repository: str) -> Path:
        return self.status_dir / f"{preview_repository_key(repository)}.json"

    def _read_status(self, repository: str) -> PreviewStatus | None:
        try:
            payload = json.loads(self._status_path(repository).read_text(encoding="utf-8"))
            return PreviewStatus(**payload)
        except (OSError, ValueError, TypeError):
            return None

    def _allowed_repositories(self) -> set[str]:
        try:
            payload = json.loads((self.control_dir / "allowlist.json").read_text(encoding="utf-8"))
            repositories = payload.get("repositories") if isinstance(payload, dict) else None
            if not isinstance(repositories, list) or not all(isinstance(value, str) for value in repositories):
                return set()
            return set(repositories)
        except (OSError, ValueError, TypeError):
            # The allowlist is the preview service's authorization input. A
            # missing or malformed control file must never widen authority.
            return set()

    def _enforce_allowlist(self) -> set[str]:
        allowed = self._allowed_repositories()
        for repository, preview in list(self._active.items()):
            if repository in allowed:
                continue
            self._stop_preview(preview)
            previous = self._read_status(repository)
            stopped = self._write_status(
                repository,
                desired_commit=previous.desired_commit if previous else preview.commit,
                active_commit=None,
                last_good_commit=previous.last_good_commit if previous else preview.commit,
                state="stopped",
                runtime=preview.runtime,
                detail="preview stopped because the repository is no longer ideas-allowlisted",
            )
            self._write_project_state(stopped)
        return allowed

    def _write_status(
        self,
        repository: str,
        *,
        desired_commit: str | None,
        active_commit: str | None,
        last_good_commit: str | None,
        state: str,
        runtime: IdeaRuntime | None,
        detail: str,
    ) -> PreviewStatus:
        health_url = None
        port = None
        if runtime is not None:
            port = runtime.port
            health_url = f"http://127.0.0.1:{runtime.port}{runtime.health_path}"
        status = PreviewStatus(
            repository=repository,
            desired_commit=desired_commit,
            active_commit=active_commit,
            last_good_commit=last_good_commit,
            state=state,
            port=port,
            health_url=health_url,
            detail=redact(detail, 4000),
            updated_at=utcnow(),
        )
        self._atomic_json(self._status_path(repository), asdict(status))
        return status

    def _write_project_state(self, status: PreviewStatus) -> None:
        project = self._project_dir(status.repository)
        project.mkdir(parents=True, exist_ok=True)
        self._atomic_json(self._state_path(status.repository), asdict(status))

    def _set_current(self, repository: str, release: Path) -> None:
        project = self._project_dir(repository)
        project.mkdir(parents=True, exist_ok=True)
        temporary = project / ".current"
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        temporary.symlink_to(release.relative_to(project), target_is_directory=True)
        os.replace(temporary, project / "current")

    def _load_request(self, path: Path) -> PreviewRequest:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            request = PreviewRequest(**payload)
        except (OSError, ValueError, TypeError) as exc:
            raise PreviewManagerError(f"invalid preview request {path.name}: {exc}") from exc
        expected = self.queue_dir / f"{preview_repository_key(request.repository)}.json"
        if path.resolve() != expected.resolve() or not COMMIT_PATTERN.fullmatch(request.commit):
            raise PreviewManagerError("preview request has an invalid repository or commit")
        archive = (self.root / request.archive).resolve()
        if not archive.is_relative_to(self.archive_dir.resolve()) or not archive.is_file():
            raise PreviewManagerError("preview archive escapes the managed archive directory")
        if sha256_file(archive) != request.archive_sha256:
            raise PreviewManagerError("preview archive checksum does not match its request")
        return request

    def _release_dir(self, repository: str, commit: str) -> Path:
        return self._project_dir(repository) / "releases" / commit

    def _prepare_release(self, request: PreviewRequest) -> tuple[Path, IdeaRuntime]:
        archive = (self.root / request.archive).resolve()
        release = self._release_dir(request.repository, request.commit)
        marker = release / ".symphony-preview-archive"
        if not marker.is_file() or marker.read_text(errors="replace").strip() != request.archive_sha256:
            releases = release.parent
            releases.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=f".{request.commit}.", dir=releases))
            try:
                with tarfile.open(archive, "r:") as bundle:
                    bundle.extractall(temporary, filter="data")
                marker_target = temporary / marker.name
                marker_target.write_text(request.archive_sha256 + "\n")
                if release.exists():
                    shutil.rmtree(release)
                os.replace(temporary, release)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)
        runtime_path = release / ".symphony" / "idea.toml"
        if runtime_path.is_symlink() or not runtime_path.is_file():
            raise PreviewManagerError("release has no regular .symphony/idea.toml")
        runtime = parse_runtime(runtime_path.read_bytes())
        if request.setup_script:
            script = release / safe_path(request.setup_script)
            if script.exists():
                if script.is_symlink() or not script.is_file() or not script.resolve().is_relative_to(release.resolve()):
                    raise PreviewManagerError("preview setup script is not a confined regular file")
                setup = subprocess.run(
                    ["bash", str(script)],
                    cwd=release,
                    env=self._environment(runtime),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=1800,
                    check=False,
                )
                if setup.returncode != 0:
                    output = setup.stdout.decode(errors="replace") if isinstance(setup.stdout, bytes) else setup.stdout
                    raise PreviewManagerError(f"preview setup failed: {redact(output or '', 4000)}")
        return release, runtime

    def _environment(self, runtime: IdeaRuntime) -> dict[str, str]:
        return {
            "HOME": str(self.home_dir),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "CI": "true",
            "HOST": "127.0.0.1",
            "PORT": str(runtime.port),
            "NO_COLOR": "1",
        }

    def _port_conflict(self, repository: str, port: int) -> str | None:
        for other_repository, preview in self._active.items():
            if other_repository != repository and preview.process.poll() is None and preview.runtime.port == port:
                return other_repository
        return None

    def _start_release(
        self,
        repository: str,
        commit: str,
        release: Path,
        runtime: IdeaRuntime,
    ) -> ManagedPreview:
        conflict = self._port_conflict(repository, runtime.port)
        if conflict:
            raise PreviewManagerError(f"preview port {runtime.port} is already assigned to {conflict}")
        logs = self._project_dir(repository) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log_handle = (logs / f"{commit}.log").open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                preview_argv(runtime),
                cwd=release,
                env=self._environment(runtime),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            log_handle.close()
            raise
        managed = ManagedPreview(repository, commit, release, runtime, process, log_handle)
        self._active[repository] = managed
        return managed

    @staticmethod
    def _available_loopback_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    def _health(self, preview: ManagedPreview) -> None:
        url = f"http://127.0.0.1:{preview.runtime.port}{preview.runtime.health_path}"
        deadline = time.monotonic() + preview.runtime.startup_timeout_seconds
        while time.monotonic() < deadline:
            if preview.process.poll() is not None:
                raise PreviewManagerError(f"preview exited before health succeeded with {preview.process.returncode}")
            try:
                response = httpx.get(url, timeout=2)
                if 200 <= response.status_code < 400:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        raise PreviewManagerError(f"preview health check timed out: {url}")

    def _stop_preview(self, preview: ManagedPreview) -> None:
        if preview.process.poll() is None:
            try:
                os.killpg(preview.process.pid, signal.SIGTERM)
                preview.process.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        # Parent exit is not proof that a dev server's descendants exited.
        try:
            os.killpg(preview.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            preview.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        close = getattr(preview.log_handle, "close", None)
        if close:
            close()
        if self._active.get(preview.repository) is preview:
            self._active.pop(preview.repository, None)

    def _runtime_for_release(self, release: Path) -> IdeaRuntime:
        return parse_runtime((release / ".symphony" / "idea.toml").read_bytes())

    def _deploy(self, request: PreviewRequest) -> PreviewStatus:
        previous_status = self._read_status(request.repository)
        previous_commit = previous_status.last_good_commit if previous_status else None
        current = self._active.get(request.repository)
        if current and current.commit == request.commit and current.process.poll() is None:
            return self._write_status(
                request.repository,
                desired_commit=request.commit,
                active_commit=request.commit,
                last_good_commit=request.commit,
                state="healthy",
                runtime=current.runtime,
                detail="published preview is healthy",
            )

        try:
            release, runtime = self._prepare_release(request)
        except (OSError, subprocess.SubprocessError, tarfile.TarError, IdeaContractError, PreviewManagerError) as exc:
            active = current if current and current.process.poll() is None else None
            status = self._write_status(
                request.repository,
                desired_commit=request.commit,
                active_commit=active.commit if active else None,
                last_good_commit=previous_commit,
                state="rollback" if active else "failed",
                runtime=active.runtime if active else None,
                detail=f"deployment preparation failed; last good preview retained: {type(exc).__name__}: {exc}",
            )
            self._write_project_state(status)
            self._cleanup(request.repository, status)
            return status

        stable_port = None
        if previous_status and previous_status.last_good_commit:
            stable_port = previous_status.port
            if stable_port is None:
                try:
                    stable_port = self._runtime_for_release(
                        self._release_dir(request.repository, previous_status.last_good_commit)
                    ).port
                except (OSError, IdeaContractError):
                    pass
        if stable_port is None and current and current.process.poll() is None:
            stable_port = current.runtime.port
        if stable_port is not None and runtime.port != stable_port:
            active = current if current and current.process.poll() is None else None
            status = self._write_status(
                request.repository,
                desired_commit=request.commit,
                active_commit=active.commit if active else None,
                last_good_commit=previous_commit,
                state="rollback" if active else "failed",
                runtime=active.runtime if active else None,
                detail=(
                    f"preview port is immutable after the first healthy release: "
                    f"expected {stable_port}, received {runtime.port}"
                ),
            )
            self._write_project_state(status)
            self._cleanup(request.repository, status)
            return status

        retained = current if current and current.process.poll() is None else None
        candidate = None
        try:
            if retained is not None:
                previous_commit = retained.commit
                # Exercise the candidate on a temporary loopback port while
                # the last-good process continues serving its stable port.
                probe_runtime = replace(runtime, port=self._available_loopback_port())
                candidate = self._start_release(request.repository, request.commit, release, probe_runtime)
                self._active[request.repository] = retained
                self._health(candidate)
                self._stop_preview(candidate)
                candidate = None
                self._active[request.repository] = retained
                self._stop_preview(retained)
                retained = None

            candidate = self._start_release(request.repository, request.commit, release, runtime)
            self._health(candidate)
        except (OSError, subprocess.SubprocessError, PreviewManagerError) as exc:
            if candidate is not None:
                self._stop_preview(candidate)
            rollback = retained
            if rollback is not None:
                self._active[request.repository] = rollback
            rollback_error = ""
            if rollback is None and previous_commit:
                previous_release = self._release_dir(request.repository, previous_commit)
                try:
                    previous_runtime = self._runtime_for_release(previous_release)
                    rollback = self._start_release(
                        request.repository,
                        previous_commit,
                        previous_release,
                        previous_runtime,
                    )
                    self._health(rollback)
                    self._set_current(request.repository, previous_release)
                except (OSError, subprocess.SubprocessError, IdeaContractError, PreviewManagerError) as rollback_exc:
                    if rollback:
                        self._stop_preview(rollback)
                    rollback = None
                    rollback_error = f"; rollback failed: {type(rollback_exc).__name__}: {rollback_exc}"
            status = self._write_status(
                request.repository,
                desired_commit=request.commit,
                active_commit=rollback.commit if rollback else None,
                last_good_commit=previous_commit,
                state="rollback" if rollback else "failed",
                runtime=rollback.runtime if rollback else runtime,
                detail=f"candidate failed health; last good preview retained: {type(exc).__name__}: {exc}{rollback_error}",
            )
            self._write_project_state(status)
            self._cleanup(request.repository, status)
            return status

        status = self._write_status(
            request.repository,
            desired_commit=request.commit,
            active_commit=request.commit,
            last_good_commit=request.commit,
            state="healthy",
            runtime=runtime,
            detail="published preview is healthy",
        )
        self._set_current(request.repository, release)
        self._write_project_state(status)
        self._cleanup(request.repository, status)
        return status

    def _cleanup(self, repository: str, status: PreviewStatus) -> None:
        releases = self._project_dir(repository) / "releases"
        protected = {value for value in (status.active_commit, status.last_good_commit) if value}
        if releases.is_dir():
            candidates = sorted(
                (path for path in releases.iterdir() if path.is_dir() and COMMIT_PATTERN.fullmatch(path.name)),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            kept = 0
            for candidate in candidates:
                if candidate.name in protected or kept < self.releases_to_keep:
                    kept += 1
                    continue
                shutil.rmtree(candidate, ignore_errors=True)

        archives = self.archive_dir / preview_repository_key(repository)
        if archives.is_dir():
            candidates = sorted(archives.glob("*.tar"), key=lambda path: path.stat().st_mtime, reverse=True)
            kept = 0
            for candidate in candidates:
                commit = candidate.stem
                if commit in protected or kept < self.releases_to_keep:
                    kept += 1
                    continue
                try:
                    candidate.unlink()
                except OSError:
                    pass

        logs = self._project_dir(repository) / "logs"
        if logs.is_dir():
            candidates = sorted(logs.glob("*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
            kept = 0
            for candidate in candidates:
                commit = candidate.stem
                if commit in protected or kept < self.releases_to_keep:
                    kept += 1
                    continue
                try:
                    candidate.unlink()
                except OSError:
                    pass

    def _restart_dead(self) -> None:
        for repository, preview in list(self._active.items()):
            if preview.process.poll() is None:
                continue
            self._stop_preview(preview)
            status = self._read_status(repository)
            commit = status.last_good_commit if status else preview.commit
            if not commit:
                continue
            release = self._release_dir(repository, commit)
            restarted = None
            try:
                runtime = self._runtime_for_release(release)
                restarted = self._start_release(repository, commit, release, runtime)
                self._health(restarted)
                self._set_current(repository, release)
                next_status = self._write_status(
                    repository,
                    desired_commit=status.desired_commit if status else commit,
                    active_commit=commit,
                    last_good_commit=commit,
                    state="healthy" if (status is None or status.desired_commit == commit) else "rollback",
                    runtime=runtime,
                    detail="preview process restarted after an unexpected exit",
                )
            except (OSError, subprocess.SubprocessError, IdeaContractError, PreviewManagerError) as exc:
                if restarted is not None:
                    self._stop_preview(restarted)
                next_status = self._write_status(
                    repository,
                    desired_commit=status.desired_commit if status else commit,
                    active_commit=None,
                    last_good_commit=commit,
                    state="failed",
                    runtime=preview.runtime,
                    detail=f"last good preview could not restart: {type(exc).__name__}: {exc}",
                )
            self._write_project_state(next_status)

    def restore(self) -> None:
        allowed = self._allowed_repositories()
        for state_path in sorted(self.projects_dir.glob("*/state.json")):
            status = None
            preview = None
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
                status = PreviewStatus(**payload)
                if status.repository not in allowed:
                    stopped = self._write_status(
                        status.repository,
                        desired_commit=status.desired_commit,
                        active_commit=None,
                        last_good_commit=status.last_good_commit,
                        state="stopped",
                        runtime=None,
                        detail="preview remains stopped because the repository is not ideas-allowlisted",
                    )
                    self._write_project_state(stopped)
                    continue
                commit = status.last_good_commit
                if not commit or not COMMIT_PATTERN.fullmatch(commit):
                    continue
                release = self._release_dir(status.repository, commit)
                runtime = self._runtime_for_release(release)
                preview = self._start_release(status.repository, commit, release, runtime)
                self._health(preview)
                self._set_current(status.repository, release)
                restored = self._write_status(
                    status.repository,
                    desired_commit=status.desired_commit,
                    active_commit=commit,
                    last_good_commit=commit,
                    state="healthy" if status.desired_commit == commit else "rollback",
                    runtime=runtime,
                    detail="last good preview restored after manager start",
                )
                self._write_project_state(restored)
                self._cleanup(status.repository, restored)
            except (OSError, ValueError, TypeError, subprocess.SubprocessError, IdeaContractError, PreviewManagerError) as exc:
                if preview is not None:
                    self._stop_preview(preview)
                if status is not None:
                    failed = self._write_status(
                        status.repository,
                        desired_commit=status.desired_commit,
                        active_commit=None,
                        last_good_commit=status.last_good_commit,
                        state="failed",
                        runtime=None,
                        detail=f"last good preview could not be restored: {type(exc).__name__}: {exc}",
                    )
                    self._write_project_state(failed)
                continue

    def run_once(self) -> int:
        allowed = self._enforce_allowlist()
        self._restart_dead()
        processed = 0
        for request_path in sorted(self.queue_dir.glob("*.json"), key=lambda path: path.stat().st_mtime):
            request = None
            try:
                request = self._load_request(request_path)
                if request.repository not in allowed:
                    stopped = self._write_status(
                        request.repository,
                        desired_commit=request.commit,
                        active_commit=None,
                        last_good_commit=(
                            self._read_status(request.repository).last_good_commit
                            if self._read_status(request.repository)
                            else None
                        ),
                        state="stopped",
                        runtime=None,
                        detail="preview request rejected because the repository is not ideas-allowlisted",
                    )
                    self._write_project_state(stopped)
                    self._cleanup(request.repository, stopped)
                    continue
                self._write_status(
                    request.repository,
                    desired_commit=request.commit,
                    active_commit=(self._active.get(request.repository).commit if request.repository in self._active else None),
                    last_good_commit=(self._read_status(request.repository).last_good_commit if self._read_status(request.repository) else None),
                    state="pending",
                    runtime=(self._active.get(request.repository).runtime if request.repository in self._active else None),
                    detail="preview deployment queued",
                )
                self._deploy(request)
                processed += 1
            except (OSError, ValueError, TypeError, PreviewManagerError) as exc:
                # Invalid requests are quarantined by removal. The orchestrator
                # records future valid commits under a fresh immutable archive.
                try:
                    payload = json.loads(request_path.read_text(encoding="utf-8"))
                    repository = str(payload.get("repository") or "")
                    if repository:
                        previous = self._read_status(repository)
                        active = self._active.get(repository)
                        failed = self._write_status(
                            repository,
                            desired_commit=str(payload.get("commit") or "") or None,
                            active_commit=active.commit if active and active.process.poll() is None else None,
                            last_good_commit=previous.last_good_commit if previous else None,
                            state="rollback" if active and active.process.poll() is None else "failed",
                            runtime=active.runtime if active and active.process.poll() is None else None,
                            detail=f"invalid preview request: {type(exc).__name__}: {exc}",
                        )
                        self._write_project_state(failed)
                        self._cleanup(repository, failed)
                except Exception:
                    pass
            finally:
                try:
                    current_payload = json.loads(request_path.read_text(encoding="utf-8"))
                    current_commit = current_payload.get("commit") if isinstance(current_payload, dict) else None
                    if request is None or current_commit == request.commit:
                        request_path.unlink()
                except (OSError, ValueError, TypeError):
                    pass
        return processed

    def close(self) -> None:
        for preview in list(self._active.values()):
            self._stop_preview(preview)
        if self._lock_handle is not None:
            close = getattr(self._lock_handle, "close", None)
            if close:
                close()
            self._lock_handle = None

    def serve(self) -> None:
        self.acquire_lock()

        def stop(_signal: int, _frame: FrameType | None) -> None:
            self._stop = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        self.restore()
        try:
            while not self._stop:
                self.run_once()
                time.sleep(self.poll_seconds)
        finally:
            self.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Credential-free persistent idea preview manager")
    parser.add_argument("--root", type=Path, default=Path("/var/lib/openhands-preview"))
    parser.add_argument("--poll-seconds", type=float, default=2)
    args = parser.parse_args()
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be greater than zero")
    PreviewManager(args.root, poll_seconds=args.poll_seconds).serve()


if __name__ == "__main__":
    main()
