from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_GUIDANCE = "needs-guidance"
    PR_OPEN = "pr-open"
    REVIEWING = "reviewing"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELED = "canceled"
    DONE = "done"


TERMINAL_STATES = {JobState.BLOCKED, JobState.FAILED, JobState.CANCELED, JobState.DONE}
ACTIVE_STATES = {JobState.RUNNING, JobState.REVIEWING}


@dataclass(frozen=True)
class IssueSnapshot:
    repository: str
    number: int
    title: str
    body: str
    state: str
    labels: tuple[str, ...]
    updated_at: str
    private: bool = True
    default_branch: str = "main"

    def content_hash(self) -> str:
        """Hash the specification, excluding expected label/comment churn."""
        value = {"repository": self.repository, "number": self.number, "title": self.title, "body": self.body}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def revision_hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, value: str) -> IssueSnapshot:
        data = json.loads(value)
        data["labels"] = tuple(data["labels"])
        return cls(**data)


@dataclass
class ValidationResult:
    command: tuple[str, ...]
    exit_code: int | None
    started_at: str
    finished_at: str
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class ProviderCapabilities:
    supports_headless: bool
    supports_resume: bool
    supports_acp: bool
    supports_subscription_login: bool
    supports_review: bool
    autonomous_available: bool
    limitation: str = ""


@dataclass
class AuthStatus:
    available: bool
    authenticated: bool
    detail: str


@dataclass
class QuotaState:
    limited: bool = False
    retry_after_seconds: int | None = None
    detail: str = "available"


class ProviderOutcome(StrEnum):
    COMPLETED = "completed"
    NEEDS_GUIDANCE = "needs-guidance"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass
class ProviderRun:
    provider: str
    conversation_id: str
    session_id: str | None = None


@dataclass
class ProviderResult:
    outcome: ProviderOutcome
    summary: str
    question_or_reason: str = ""
    conversation_id: str | None = None
    session_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Job:
    id: str
    repository: str
    issue_number: int
    snapshot_hash: str
    content_hash: str
    snapshot_json: str
    implementation_provider: str
    review_provider: str | None
    review_required: bool
    state: JobState
    attempt: int
    branch: str
    worktree: str | None
    concurrency_key: str
    conversation_id: str | None
    session_id: str | None
    review_conversation_id: str | None
    review_session_id: str | None
    status_comment_id: int | None
    pr_number: int | None
    pr_url: str | None
    phase: str
    validation_summary: str
    actionable_message: str
    terminal_outcome: str | None
    terminal_reason: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    heartbeat_at: str | None
    finished_at: str | None
    retry_requested: bool
    cancel_requested: bool
    pause_requested: bool

    @property
    def snapshot(self) -> IssueSnapshot:
        return IssueSnapshot.from_json(self.snapshot_json)
