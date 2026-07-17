from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from .coordinator import Coordinator, IntakeError
from .scheduler import Scheduler
from .store import Store

TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def _issue_identity(payload: dict[str, Any]) -> tuple[str | None, int | None]:
    repository = (payload.get("repository") or {}).get("full_name")
    issue = payload.get("issue") or {}
    number = issue.get("number")
    return (str(repository) if repository else None, int(number) if number else None)


def create_app(
    store: Store,
    coordinator: Coordinator,
    scheduler: Scheduler,
    secret_file: Path,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler.start()
        try:
            yield
        finally:
            scheduler.stop(wait=False)

    app = FastAPI(title="OpenHands Symphony", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/github")
    async def github_webhook(
        request: Request,
        x_github_event: str = Header(default="", alias="X-GitHub-Event"),
        x_github_delivery: str = Header(default="", alias="X-GitHub-Delivery"),
        x_hub_signature_256: str = Header(default="", alias="X-Hub-Signature-256"),
    ) -> dict[str, Any]:
        if not x_github_event or not x_github_delivery:
            raise HTTPException(400, "missing GitHub event headers")
        if not secret_file.is_file():
            raise HTTPException(503, "webhook secret is not configured")
        secret = secret_file.read_bytes().strip()
        body = await request.body()
        expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, x_hub_signature_256):
            raise HTTPException(401, "invalid webhook signature")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "invalid JSON") from exc
        repository, issue_number = _issue_identity(payload)
        inserted = store.record_delivery(
            x_github_delivery,
            x_github_event,
            hashlib.sha256(body).hexdigest(),
            repository,
            issue_number,
        )
        if not inserted:
            return {"accepted": True, "duplicate": True}
        if not repository or not issue_number:
            return {"accepted": True, "ignored": "event has no issue"}

        if x_github_event == "issues":
            action = str(payload.get("action") or "")
            if action in {"labeled", "unlabeled", "edited", "opened", "reopened", "closed"}:
                try:
                    snapshot = coordinator.github.get_issue(repository, issue_number)
                    job, created = coordinator.enqueue(snapshot)
                    scheduler.tick()
                    return {"accepted": True, "created": created, "job_id": job.id}
                except IntakeError as exc:
                    return {"accepted": True, "ineligible": str(exc)}

        if x_github_event == "issue_comment" and payload.get("action") == "created":
            comment = payload.get("comment") or {}
            from .intake import parse_control_command

            command = parse_control_command(str(comment.get("body") or ""))
            association = str(comment.get("author_association") or "").upper()
            if command and association in TRUSTED_ASSOCIATIONS:
                job = coordinator.control(repository, issue_number, command)
                scheduler.tick()
                return {"accepted": True, "command": command, "job_id": job.id if job else None}
            if command:
                return {"accepted": True, "ignored": "untrusted command author"}
        return {"accepted": True, "ignored": "event/action is not routed"}

    return app
