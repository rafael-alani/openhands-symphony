from __future__ import annotations

import io
import subprocess
from types import SimpleNamespace

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
