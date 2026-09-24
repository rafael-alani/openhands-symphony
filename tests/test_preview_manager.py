from __future__ import annotations

import io
import socket
import subprocess
from types import SimpleNamespace

import pytest

from symphony.ideas_contract import parse_runtime
from symphony.preview_manager import ManagedPreview, PreviewManager, PreviewManagerError
from symphony.preview_queue import PreviewQueue


def _run(command: list[str], cwd=None) -> str:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


class FakeProcess:
    next_pid = 1000

    def __init__(self):
        FakeProcess.next_pid += 1
        self.pid = FakeProcess.next_pid
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


class FakePreviewManager(PreviewManager):
    next_probe_port = 25000

    @classmethod
    def _available_loopback_port(cls):
        cls.next_probe_port += 1
        return cls.next_probe_port

    def _start_release(self, repository, commit, release, runtime):
        process = FakeProcess()
        managed = ManagedPreview(repository, commit, release, runtime, process, io.BytesIO())
        self._active[repository] = managed
        return managed

    def _health(self, preview):
        if "SystemExit" in (preview.release / "app.py").read_text():
            preview.process.returncode = 2
            raise PreviewManagerError("preview exited before health succeeded with 2")

    def _stop_preview(self, preview):
        preview.process.returncode = preview.process.returncode or 0
        preview.log_handle.close()
        if self._active.get(preview.repository) is preview:
            self._active.pop(preview.repository, None)


def _commit(repository, message: str) -> str:
    _run(["git", "add", "--all"], repository)
    _run(
        [
            "git",
            "-c",
            "user.name=Preview Test",
            "-c",
            "user.email=preview@example.invalid",
            "commit",
            "-m",
            message,
        ],
        repository,
    )
    return _run(["git", "rev-parse", "HEAD"], repository)


def _runtime(port: int) -> str:
    return (
        'provider = "codex"\n\n[preview]\nstart = ["python3", "app.py"]\n'
        f'port = {port}\nhealth_path = "/health"\nstartup_timeout_seconds = 3\n'
    )


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


HEALTHY_APP = """
import http.server
import os

host = os.environ["HOST"]
port = int(os.environ["PORT"])
http.server.ThreadingHTTPServer((host, port), http.server.SimpleHTTPRequestHandler).serve_forever()
"""


def test_preview_manager_keeps_last_good_release_when_candidate_fails(tmp_path):
    workspace_root = tmp_path / "workspaces"
    repository = workspace_root / "runs" / "published"
    repository.mkdir(parents=True)
    _run(["git", "init", "-b", "main"], repository)
    (repository / ".symphony").mkdir()
    port = 14317
    (repository / ".symphony" / "idea.toml").write_text(_runtime(port))
    (repository / "app.py").write_text(HEALTHY_APP)
    first_commit = _commit(repository, "healthy preview")

    root = tmp_path / "preview"
    queue = PreviewQueue(root, workspace_root)
    queue.publish_allowlist(("solo/idea",))
    first_run = SimpleNamespace(
        repository="solo/idea",
        published_commit=first_commit,
        worktree=str(repository),
    )
    assert queue.enqueue(first_run, "")

    manager = FakePreviewManager(root, poll_seconds=0.01)
    try:
        assert manager.run_once() == 1
        first = queue.status("solo/idea")
        assert first is not None
        assert first.state == "healthy"
        assert first.active_commit == first.last_good_commit == first_commit
        first_process = manager._active["solo/idea"].process
        current = manager._project_dir("solo/idea") / "current"
        assert current.is_symlink()
        assert current.resolve() == manager._release_dir("solo/idea", first_commit)

        (repository / "app.py").write_text("raise SystemExit(2)\n")
        failed_commit = _commit(repository, "candidate exits")
        failed_run = SimpleNamespace(
            repository="solo/idea",
            published_commit=failed_commit,
            worktree=str(repository),
        )
        assert queue.enqueue(failed_run, "")

        assert manager.run_once() == 1
        rolled_back = queue.status("solo/idea")
        assert rolled_back is not None
        assert rolled_back.state == "rollback"
        assert rolled_back.desired_commit == failed_commit
        assert rolled_back.active_commit == rolled_back.last_good_commit == first_commit
        assert manager._active["solo/idea"].process is first_process
        assert current.resolve() == manager._release_dir("solo/idea", first_commit)
    finally:
        manager.close()

    restored_manager = FakePreviewManager(root, poll_seconds=0.01)
    try:
        restored_manager.restore()
        restored = queue.status("solo/idea")
        assert restored is not None
        assert restored.state == "rollback"
        assert restored.active_commit == restored.last_good_commit == first_commit
    finally:
        restored_manager.close()


def test_preview_manager_restarts_a_crashed_last_good_process(tmp_path):
    workspace_root = tmp_path / "workspaces"
    repository = workspace_root / "runs" / "published"
    repository.mkdir(parents=True)
    _run(["git", "init", "-b", "main"], repository)
    (repository / ".symphony").mkdir()
    port = 14318
    (repository / ".symphony" / "idea.toml").write_text(_runtime(port))
    (repository / "app.py").write_text(HEALTHY_APP)
    commit = _commit(repository, "healthy preview")

    root = tmp_path / "preview"
    queue = PreviewQueue(root, workspace_root)
    queue.publish_allowlist(("solo/idea",))
    queue.enqueue(SimpleNamespace(repository="solo/idea", published_commit=commit, worktree=str(repository)), "")
    manager = FakePreviewManager(root, poll_seconds=0.01)
    try:
        manager.run_once()
        process = manager._active["solo/idea"].process
        process.terminate()
        process.wait(timeout=5)

        manager.run_once()

        restarted = manager._active["solo/idea"].process
        assert restarted.pid != process.pid
        assert queue.status("solo/idea").state == "healthy"

        queue.publish_allowlist(())
        manager.run_once()
        stopped = queue.status("solo/idea")
        assert stopped is not None and stopped.state == "stopped"
        assert stopped.active_commit is None
        assert "solo/idea" not in manager._active
    finally:
        manager.close()


@pytest.mark.parametrize("recovery", ["restore", "restart"])
def test_preview_manager_stops_unhealthy_live_process_after_recovery_failure(tmp_path, monkeypatch, recovery):
    root = tmp_path / "preview"
    queue = PreviewQueue(root, tmp_path / "workspaces")
    queue.publish_allowlist(("solo/idea",))
    manager = FakePreviewManager(root)
    commit = "a" * 40
    release = manager._release_dir("solo/idea", commit)
    (release / ".symphony").mkdir(parents=True)
    (release / ".symphony" / "idea.toml").write_text(_runtime(14318))
    runtime = parse_runtime(_runtime(14318).encode())
    previous = manager._start_release("solo/idea", commit, release, runtime)
    status = manager._write_status(
        "solo/idea", desired_commit=commit, active_commit=commit, last_good_commit=commit,
        state="healthy", runtime=runtime, detail="healthy",
    )
    manager._write_project_state(status)
    failed_processes = []

    def unhealthy(preview):
        failed_processes.append(preview.process)
        raise PreviewManagerError("health timed out while the process remained alive")

    monkeypatch.setattr(manager, "_health", unhealthy)
    try:
        if recovery == "restore":
            manager.close()
            manager.restore()
        else:
            previous.process.terminate()
            manager._restart_dead()
        assert failed_processes and failed_processes[0].poll() is not None
        assert "solo/idea" not in manager._active
        assert queue.status("solo/idea").state == "failed"
    finally:
        manager.close()


def test_new_request_arriving_during_deploy_is_not_deleted(tmp_path):
    workspace_root = tmp_path / "workspaces"
    repository = workspace_root / "runs" / "published"
    repository.mkdir(parents=True)
    _run(["git", "init", "-b", "main"], repository)
    (repository / ".symphony").mkdir()
    (repository / ".symphony" / "idea.toml").write_text(_runtime(14319))
    (repository / "app.py").write_text(HEALTHY_APP)
    first_commit = _commit(repository, "first preview")

    root = tmp_path / "preview"
    queue = PreviewQueue(root, workspace_root)
    queue.publish_allowlist(("solo/idea",))
    queue.enqueue(
        SimpleNamespace(repository="solo/idea", published_commit=first_commit, worktree=str(repository)),
        "",
    )
    (repository / "version.txt").write_text("second\n")
    second_commit = _commit(repository, "second preview")
    second_run = SimpleNamespace(
        repository="solo/idea",
        published_commit=second_commit,
        worktree=str(repository),
    )

    class RacingManager(FakePreviewManager):
        raced = False

        def _deploy(self, request):
            if not self.raced:
                self.raced = True
                queue.enqueue(second_run, "")
            return super()._deploy(request)

    manager = RacingManager(root, poll_seconds=0.01)
    try:
        assert manager.run_once() == 1
        pending = queue.queued_request("solo/idea")
        assert pending is not None and pending.commit == second_commit

        assert manager.run_once() == 1
        status = queue.status("solo/idea")
        assert status is not None
        assert status.active_commit == status.last_good_commit == second_commit
    finally:
        manager.close()


def test_preview_manager_fails_closed_until_allowlist_is_valid(tmp_path):
    workspace_root = tmp_path / "workspaces"
    repository = workspace_root / "runs" / "published"
    repository.mkdir(parents=True)
    _run(["git", "init", "-b", "main"], repository)
    (repository / ".symphony").mkdir()
    (repository / ".symphony" / "idea.toml").write_text(_runtime(14320))
    (repository / "app.py").write_text(HEALTHY_APP)
    commit = _commit(repository, "healthy preview")

    root = tmp_path / "preview"
    queue = PreviewQueue(root, workspace_root)
    run = SimpleNamespace(repository="solo/idea", published_commit=commit, worktree=str(repository))
    assert queue.enqueue(run, "")
    manager = FakePreviewManager(root, poll_seconds=0.01)
    try:
        manager.run_once()
        stopped = queue.status("solo/idea")
        assert stopped is not None and stopped.state == "stopped"
        assert "solo/idea" not in manager._active

        queue.publish_allowlist(("solo/idea",))
        assert queue.enqueue(run, "")
        manager.run_once()
        assert queue.status("solo/idea").state == "healthy"

        (root / "control" / "allowlist.json").write_text("not json")
        manager.run_once()
        assert queue.status("solo/idea").state == "stopped"
        assert "solo/idea" not in manager._active
    finally:
        manager.close()


def test_preview_manager_bounds_artifacts_after_preparation_failures(tmp_path):
    workspace_root = tmp_path / "workspaces"
    repository = workspace_root / "runs" / "published"
    repository.mkdir(parents=True)
    _run(["git", "init", "-b", "main"], repository)
    (repository / ".symphony").mkdir()
    (repository / "app.py").write_text(HEALTHY_APP)

    root = tmp_path / "preview"
    queue = PreviewQueue(root, workspace_root)
    queue.publish_allowlist(("solo/idea",))
    manager = FakePreviewManager(root, poll_seconds=0.01, releases_to_keep=2)
    try:
        for number in range(4):
            (repository / ".symphony" / "idea.toml").write_text("invalid = [")
            (repository / "version.txt").write_text(str(number))
            commit = _commit(repository, f"invalid preview {number}")
            run = SimpleNamespace(repository="solo/idea", published_commit=commit, worktree=str(repository))
            assert queue.enqueue(run, "")
            manager.run_once()

        releases = manager._project_dir("solo/idea") / "releases"
        archives = root / "archives" / next((root / "archives").iterdir()).name
        assert len([path for path in releases.iterdir() if path.is_dir()]) == 2
        assert len(list(archives.glob("*.tar"))) == 2
    finally:
        manager.close()


def test_preview_manager_bounds_artifacts_after_successful_deployments(tmp_path):
    workspace_root = tmp_path / "workspaces"
    repository = workspace_root / "runs" / "published"
    repository.mkdir(parents=True)
    _run(["git", "init", "-b", "main"], repository)
    (repository / ".symphony").mkdir()
    runtime = _runtime(_available_loopback_port()).replace('health_path = "/health"', 'health_path = "/"')
    (repository / ".symphony" / "idea.toml").write_text(runtime)
    (repository / "app.py").write_text(HEALTHY_APP)

    root = tmp_path / "preview"
    queue = PreviewQueue(root, workspace_root)
    queue.publish_allowlist(("solo/idea",))
    manager = PreviewManager(root, poll_seconds=0.01, releases_to_keep=2)
    try:
        for number in range(4):
            (repository / "version.txt").write_text(f"{number}\n")
            commit = _commit(repository, f"healthy preview {number}")
            run = SimpleNamespace(repository="solo/idea", published_commit=commit, worktree=str(repository))
            assert queue.enqueue(run, "")
            assert manager.run_once() == 1
            assert queue.status("solo/idea").state == "healthy"

        releases = manager._project_dir("solo/idea") / "releases"
        archives = next((root / "archives").iterdir())
        logs = manager._project_dir("solo/idea") / "logs"
        assert len([path for path in releases.iterdir() if path.is_dir()]) == 2
        assert len(list(archives.glob("*.tar"))) == 2
        assert len(list(logs.glob("*.log"))) == 2
    finally:
        manager.close()


def test_preview_manager_rejects_port_changes_after_first_healthy_release(tmp_path):
    workspace_root = tmp_path / "workspaces"
    repository = workspace_root / "runs" / "published"
    repository.mkdir(parents=True)
    _run(["git", "init", "-b", "main"], repository)
    (repository / ".symphony").mkdir()
    (repository / ".symphony" / "idea.toml").write_text(_runtime(14321))
    (repository / "app.py").write_text(HEALTHY_APP)
    first_commit = _commit(repository, "healthy preview")

    root = tmp_path / "preview"
    queue = PreviewQueue(root, workspace_root)
    queue.publish_allowlist(("solo/idea",))
    queue.enqueue(
        SimpleNamespace(repository="solo/idea", published_commit=first_commit, worktree=str(repository)),
        "",
    )
    manager = FakePreviewManager(root, poll_seconds=0.01)
    try:
        manager.run_once()
        first_process = manager._active["solo/idea"].process

        (repository / ".symphony" / "idea.toml").write_text(_runtime(15321))
        second_commit = _commit(repository, "change preview port")
        queue.enqueue(
            SimpleNamespace(repository="solo/idea", published_commit=second_commit, worktree=str(repository)),
            "",
        )
        manager.run_once()

        status = queue.status("solo/idea")
        assert status is not None and status.state == "rollback"
        assert status.port == 14321
        assert status.active_commit == status.last_good_commit == first_commit
        assert "preview port is immutable" in status.detail
        assert manager._active["solo/idea"].process is first_process
    finally:
        manager.close()


def test_fresh_preview_downloads_and_builds_ignored_dependency(tmp_path):
    import functools
    import http.server
    import threading

    packages = tmp_path / "packages"
    packages.mkdir()
    (packages / "dependency.py").write_text("VALUE = 'downloaded dependency'\n")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(packages))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    workspace_root = tmp_path / "workspaces"
    repository = workspace_root / "runs" / "published"
    repository.mkdir(parents=True)
    _run(["git", "init", "-b", "main"], repository)
    (repository / ".symphony").mkdir()
    (repository / ".openhands").mkdir()
    (repository / ".symphony/idea.toml").write_text(
        _runtime(_available_loopback_port()).replace('health_path = "/health"', 'health_path = "/"')
    )
    (repository / ".gitignore").write_text("dependency.py\nbuild.txt\n__pycache__/\n")
    (repository / "app.py").write_text("from dependency import VALUE\nassert VALUE == 'downloaded dependency'\n" + HEALTHY_APP)
    (repository / ".openhands/setup.sh").write_text(
        "#!/bin/sh\nset -eu\npython3 - <<'BUILD'\n"
        "from urllib.request import urlretrieve\nfrom pathlib import Path\n"
        f"urlretrieve('http://127.0.0.1:{server.server_port}/dependency.py', 'dependency.py')\n"
        "Path('build.txt').write_text('built')\nBUILD\n"
    )
    commit = _commit(repository, "app requiring a downloaded dependency")
    root = tmp_path / "preview"
    queue = PreviewQueue(root, workspace_root)
    queue.publish_allowlist(("solo/idea",))
    queue.enqueue(SimpleNamespace(repository="solo/idea", published_commit=commit, worktree=str(repository)),
                  ".openhands/setup.sh")
    manager = PreviewManager(root, poll_seconds=0.01)
    try:
        assert not (repository / "dependency.py").exists()
        assert manager.run_once() == 1
        assert queue.status("solo/idea").state == "healthy"
        release = manager._release_dir("solo/idea", commit)
        assert (release / "dependency.py").is_file()
        assert (release / "build.txt").read_text() == "built"
    finally:
        manager.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
