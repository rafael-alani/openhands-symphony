from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from .coordinator import Coordinator, IntakeError
from .github import GitHubError
from .ideas_coordinator import IdeasCoordinator, IdeasIntakeError
from .intake import TRUSTED_ASSOCIATIONS
from .scheduler import Scheduler
from .store import Store


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
    ideas: IdeasCoordinator | None = None,
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
        if x_github_event == "push" and repository and store.hack_active(repository):
            repo_payload = payload.get("repository") or {}
            default_branch = str(repo_payload.get("default_branch") or "")
            if repo_payload.get("private") is not True:
                return {"accepted": True, "ignored": "hack repository is not private"}
            if str(payload.get("ref") or "") != f"refs/heads/{default_branch}":
                return {"accepted": True, "ignored": "campaign board is read from the default branch"}
            # The integrator reads the authoritative board at an exact remote
            # commit. A webhook only wakes it; payload text is never executed.
            scheduler.tick()
            return {"accepted": True, "campaign_reconcile": True}
        if x_github_event == "push" and repository and ideas:
            repo_payload = payload.get("repository") or {}
            default_branch = str(repo_payload.get("default_branch") or "")
            if repository not in ideas.config.ideas.repositories:
                return {"accepted": True, "ignored": "repository is not ideas-allowlisted"}
            if repo_payload.get("private") is not True:
                return {"accepted": True, "ignored": "ideas repository is not private"}
            if str(payload.get("ref") or "") != f"refs/heads/{default_branch}":
                return {"accepted": True, "ignored": "push is not for the default branch"}
            changed: set[str] = set()
            for commit in payload.get("commits") or []:
                if not isinstance(commit, dict):
                    continue
                for key in ("added", "modified", "removed"):
                    changed.update(str(path) for path in commit.get(key) or [])
            commits = payload.get("commits") or []
            truncated = int(payload.get("size") or len(commits)) > len(commits)
            if ideas.config.ideas.spec_path not in changed and not truncated:
                return {"accepted": True, "ignored": "idea spec path did not change"}
            try:
                run, created = ideas.observe_repository(repository)
                scheduler.tick()
                return {"accepted": True, "created": created, "idea_run_id": run.id}
            except (IdeasIntakeError, GitHubError) as exc:
                return {"accepted": True, "ineligible": str(exc)}
        if not repository or not issue_number:
            return {"accepted": True, "ignored": "event has no issue"}
        if repository not in coordinator.config.github.allowed_repositories or not store.vault_allows(repository, "github"):
            return {"accepted": True, "ignored": "GitHub issue intake is inactive for this repository"}

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
                comment_id = comment.get("id")
                if comment_id is None:
                    return {"accepted": True, "ignored": "command comment omitted an id"}
                job, applied = coordinator.apply_control_comment(repository, issue_number, int(comment_id), command)
                scheduler.tick()
                return {
                    "accepted": True,
                    "command": command,
                    "duplicate_command": not applied,
                    "job_id": job.id if job else None,
                }
            if command:
                return {"accepted": True, "ignored": "untrusted command author"}
        return {"accepted": True, "ignored": "event/action is not routed"}

    return app
