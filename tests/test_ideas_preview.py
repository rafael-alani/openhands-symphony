from __future__ import annotations

import subprocess
from types import SimpleNamespace

import httpx
import pytest

from symphony import ideas_preview
from symphony.ideas_contract import IdeaRuntime
from symphony.ideas_preview import IdeaPreview, PreviewError
from symphony.ideas_progress import IdeaSection


@pytest.fixture
def preview_clock(tmp_path, monkeypatch):
    now = [0.0]
    waits = []
    monkeypatch.setattr(ideas_preview.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(ideas_preview.time, "sleep", lambda duration: now.__setitem__(0, now[0] + duration))
    monkeypatch.setattr(IdeaPreview, "_available_loopback_port", lambda *_args: 4318)
    monkeypatch.setattr(ideas_preview.os, "killpg", lambda *_args: None)
    process = SimpleNamespace(pid=42, poll=lambda: None, wait=lambda timeout: waits.append(timeout) or 0)
    monkeypatch.setattr(ideas_preview.subprocess, "Popen", lambda *_args, **_kwargs: process)
    return now, waits, IdeaPreview("", tmp_path / "state")


def test_preview_total_deadline_bounds_health_requests_and_shutdown_grace(tmp_path, monkeypatch, preview_clock):
    now, waits, preview = preview_clock
    requests = []

    def unavailable(_url, *, timeout):
        requests.append(timeout)
        now[0] += timeout
        raise httpx.ReadTimeout("server never answered")

    monkeypatch.setattr(ideas_preview.httpx, "get", unavailable)
    runtime = IdeaRuntime("codex", ("app",), 4317, "/health", 30)

    with pytest.raises(PreviewError, match="deadline exhausted"):
        preview.boot_and_capture(tmp_path, runtime, (), deadline_seconds=0.3)

    assert requests == [0.3]
    assert now[0] == pytest.approx(0.3)
    assert waits[0] == 0


def test_preview_capture_uses_remaining_total_budget(tmp_path, monkeypatch, preview_clock):
    now, _, preview = preview_clock
    timeouts = []

    def healthy(_url, *, timeout):
        now[0] += 0.2
        return httpx.Response(200)

    def capture(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        now[0] += kwargs["timeout"]
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(ideas_preview.httpx, "get", healthy)
    monkeypatch.setattr(ideas_preview.subprocess, "run", capture)
    runtime = IdeaRuntime("codex", ("app",), 4317, "/", 30)
    section = IdeaSection(0, "Demo", "demo", 0, b"")

    with pytest.raises(PreviewError, match="browser-harness timed out"):
        preview.boot_and_capture(tmp_path, runtime, (section,), deadline_seconds=1)

    assert timeouts == [pytest.approx(0.8)]
    assert now[0] == pytest.approx(1)


def test_preview_rejects_symlinked_screenshot_directory_before_harness(tmp_path, monkeypatch, preview_clock):
    _, _, preview = preview_clock
    worktree = tmp_path / "worktree"
    (worktree / "idea").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (worktree / "idea/assets").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(ideas_preview.httpx, "get", lambda *_args, **_kwargs: httpx.Response(200))
    monkeypatch.setattr(ideas_preview.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("harness must not run"))
    runtime = IdeaRuntime("codex", ("app",), 4317, "/", 30)

    with pytest.raises(PreviewError, match="screenshot target escapes"):
        preview.boot_and_capture(worktree, runtime, (IdeaSection(0, "Demo", "demo", 0, b""),))

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("deadline", [0, -1, float("nan"), float("inf")])
def test_preview_rejects_invalid_total_budget_before_launch(tmp_path, monkeypatch, deadline):
    monkeypatch.setattr(ideas_preview.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("must not launch"))
    runtime = IdeaRuntime("codex", ("app",), 4317, "/", 30)
    with pytest.raises(PreviewError, match="finite positive"):
        IdeaPreview("", tmp_path).boot_and_capture(tmp_path, runtime, (), deadline_seconds=deadline)
