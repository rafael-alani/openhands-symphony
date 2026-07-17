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
