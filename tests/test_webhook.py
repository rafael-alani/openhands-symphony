from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
from conftest import FakeGitHub, issue, make_config

from symphony.coordinator import Coordinator
from symphony.providers.fake import FakeProvider
from symphony.store import Store
from symphony.webhook import create_app


class DummyScheduler:
    def start(self):
        return None

    def stop(self, wait=True):
        return None

    def tick(self):
        return 0


def test_duplicate_github_delivery_creates_one_job_and_one_status_comment(tmp_path):
    snapshot = issue()
    config = make_config(tmp_path)
    config.service.webhook_secret_file.write_text("test-secret\n")
    store = Store(config.service.state_dir / "state.db")
    github = FakeGitHub([snapshot])
    coordinator = Coordinator(config, store, github, {"codex": FakeProvider("codex")})
    app = create_app(store, coordinator, DummyScheduler(), config.service.webhook_secret_file)
    payload = json.dumps(
        {
            "action": "labeled",
            "repository": {"full_name": snapshot.repository},
            "issue": {"number": snapshot.number},
        }
    ).encode()
    signature = "sha256=" + hmac.new(b"test-secret", payload, hashlib.sha256).hexdigest()
    headers = {
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": "delivery-1",
        "X-Hub-Signature-256": signature,
        "Content-Type": "application/json",
    }

    async def deliver():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post("/webhooks/github", content=payload, headers=headers)
            second = await client.post("/webhooks/github", content=payload, headers=headers)
        return first, second

    first, second = asyncio.run(deliver())
    assert first.status_code == 200
    assert second.json()["duplicate"] is True
    assert len(store.list_jobs()) == 1
    assert github.comment_creates == 1


def test_ideas_mode_ignores_issue_events_without_touching_github(tmp_path):
    snapshot = issue()
    config = make_config(tmp_path)
    config.service.webhook_secret_file.write_text("test-secret\n")
    store = Store(config.service.state_dir / "state.db")
    store.register_vault_project(snapshot.repository, "/obsidian/project.md", managed=False, port_start=10000, port_end=10999)
    store.request_vault_mode(snapshot.repository, "idea")
    store.activate_vault_mode(snapshot.repository)
    # No issue exists in the backend: a read would fail this request.
    github = FakeGitHub([])
    coordinator = Coordinator(config, store, github, {"codex": FakeProvider("codex")})
    app = create_app(store, coordinator, DummyScheduler(), config.service.webhook_secret_file)
    payload = json.dumps({"action": "labeled", "repository": {"full_name": snapshot.repository}, "issue": {"number": 1}}).encode()
    signature = "sha256=" + hmac.new(b"test-secret", payload, hashlib.sha256).hexdigest()

    async def deliver():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/webhooks/github", content=payload, headers={
                "X-GitHub-Event": "issues", "X-GitHub-Delivery": "ignored-delivery", "X-Hub-Signature-256": signature,
            })

    response = asyncio.run(deliver())
    assert response.status_code == 200
    assert "ignored" in response.json()
    assert not store.list_jobs()


def test_campaign_board_push_wakes_integrator_once_without_normal_intake(tmp_path):
    from symphony.hack_store import HackStore

    config = make_config(tmp_path)
    config.service.webhook_secret_file.write_text("test-secret\n")
    store = Store(config.service.state_dir / "state.db")
    HackStore(store).start_campaign("solo/project", "github", "main", "a" * 40, "codex", 1)
    coordinator = Coordinator(config, store, FakeGitHub([]), {"codex": FakeProvider("codex")})
    scheduler = DummyScheduler()
    calls = []
    scheduler.tick = lambda: calls.append("wake")
    app = create_app(store, coordinator, scheduler, config.service.webhook_secret_file)
    payload = json.dumps({
        "repository": {"full_name": "solo/project", "private": True, "default_branch": "main"},
        "ref": "refs/heads/main", "commits": [{"modified": ["hack/BOARD.md"]}],
    }).encode()
    signature = "sha256=" + hmac.new(b"test-secret", payload, hashlib.sha256).hexdigest()

    async def deliver():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers = {"X-GitHub-Event": "push", "X-GitHub-Delivery": "campaign-board", "X-Hub-Signature-256": signature}
            first = await client.post("/webhooks/github", content=payload, headers=headers)
            second = await client.post("/webhooks/github", content=payload, headers=headers)
            return first, second

    first, second = asyncio.run(deliver())
    assert first.json()["campaign_reconcile"] is True
    assert second.json()["duplicate"] is True
    assert calls == ["wake"]
    assert not store.list_jobs()
    assert not store.list_idea_runs()
