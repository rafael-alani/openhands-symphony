from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .models import IssueSnapshot

IMPLEMENTATION_LABELS = {
    "agent:claude": "claude",
    "agent:codex": "codex",
    "agent:antigravity": "antigravity",
}
REVIEW_LABELS = {
    "review:claude": "claude",
    "review:codex": "codex",
    "review:antigravity": "antigravity",
}
USER_CONTROLLED_LABELS = {
    "agent:ready",
    *IMPLEMENTATION_LABELS,
    "review:required",
    *REVIEW_LABELS,
    "agent:paused",
    "agent:manual-only",
}
SYSTEM_STATE_LABELS = {
    "agent:queued",
    "agent:running",
    "agent:needs-guidance",
    "agent:pr-open",
    "agent:failed",
    "agent:done",
}
ALL_LABELS = USER_CONTROLLED_LABELS | SYSTEM_STATE_LABELS | {"generated-by-agent"}

CONTROL_PATTERN = re.compile(r"^/agent\s+(pause|resume|retry|cancel)\s*$", re.IGNORECASE)
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class RoutingDecision:
    eligible: bool
    implementation_provider: str | None = None
    review_required: bool = False
    review_provider: str | None = None
    reason: str = ""


def validate_repository_name(repository: str) -> None:
    if not REPOSITORY_PATTERN.fullmatch(repository) or ".." in repository:
        raise ValueError(f"invalid repository identifier: {repository!r}")


def route(snapshot: IssueSnapshot, available_reviewers: set[str] | None = None) -> RoutingDecision:
    labels = set(snapshot.labels)
    if snapshot.state.lower() != "open":
        return RoutingDecision(False, reason="issue is closed")
    if "agent:paused" in labels:
        return RoutingDecision(False, reason="issue is paused")
    if "agent:manual-only" in labels:
        return RoutingDecision(False, reason="issue is manual-only")
    if "agent:ready" not in labels:
        return RoutingDecision(False, reason="agent:ready is absent")

    implementation = [provider for label, provider in IMPLEMENTATION_LABELS.items() if label in labels]
    if len(implementation) != 1:
        return RoutingDecision(False, reason="exactly one implementation-provider label is required")

    review_required = "review:required" in labels
    requested_reviewers = [provider for label, provider in REVIEW_LABELS.items() if label in labels]
    if len(requested_reviewers) > 1:
        return RoutingDecision(False, reason="at most one review-provider label is allowed")
    if requested_reviewers and not review_required:
        return RoutingDecision(False, reason="review-provider label requires review:required")

    review_provider = requested_reviewers[0] if requested_reviewers else None
    if review_required and review_provider is None:
        candidates = sorted((available_reviewers or set()) - {implementation[0]})
        if not candidates and implementation[0] in (available_reviewers or set()):
            candidates = [implementation[0]]
        review_provider = candidates[0] if candidates else None
        if review_provider is None:
            return RoutingDecision(False, reason="review requested but no review provider is available")

    return RoutingDecision(True, implementation[0], review_required, review_provider)


def parse_control_command(body: str) -> str | None:
    match = CONTROL_PATTERN.fullmatch(body.strip())
    return match.group(1).lower() if match else None


def short_slug(title: str, max_length: int = 48) -> str:
    value = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:max_length].rstrip("-") or "work"


def branch_name(issue_number: int, title: str) -> str:
    if issue_number < 1:
        raise ValueError("issue number must be positive")
    return f"agent/{issue_number}-{short_slug(title)}"
