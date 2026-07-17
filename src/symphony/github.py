from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Protocol

from .intake import SYSTEM_STATE_LABELS, TRUSTED_ASSOCIATIONS, parse_control_command, route, validate_repository_name
from .models import IssueSnapshot, Job, JobState

STATUS_MARKER = "<!-- openhands-symphony-status -->"


class GitHubError(RuntimeError):
    pass


class StaleIssueError(GitHubError):
    pass


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    is_draft: bool
    head: str


class GitHubBackend(Protocol):
    def get_issue(self, repository: str, issue_number: int) -> IssueSnapshot: ...
    def list_ready_issues(self, repository: str) -> list[IssueSnapshot]: ...
    def find_open_pr(self, repository: str, branch: str) -> PullRequest | None: ...
    def remote_branch_exists(self, repository: str, branch: str) -> bool: ...
    def guard_code_mutation(self, job: Job, *, allow_existing_branch: bool) -> IssueSnapshot: ...
    def recent_issue_comments(self, repository: str, issue_number: int) -> list[str]: ...
    def list_control_commands(self, repository: str) -> list[tuple[int, int, str]]: ...
    def update_status_comment(self, job: Job, body: str) -> int: ...
    def set_state_labels(self, job: Job, state: JobState) -> None: ...
    def create_draft_pr(self, job: Job, title: str, body: str, generated_label: str) -> PullRequest: ...
    def update_pr_validation(self, job: Job, validation: str) -> None: ...
    def pr_review_context(self, repository: str, pr_number: int) -> dict[str, Any]: ...
    def pr_state(self, repository: str, pr_number: int) -> dict[str, Any]: ...
    def post_review(self, repository: str, pr_number: int, body: str, event: str) -> None: ...
    def set_control_state(self, repository: str, issue_number: int, command: str) -> None: ...
    def ensure_contract_labels(self, repository: str, labels: dict[str, tuple[str, str]]) -> None: ...


class GhCLIBackend:
    """GitHub adapter using argv-only gh calls; no issue text is evaluated by a shell."""

    def __init__(self, allowlist: tuple[str, ...], *, private_only: bool = True, bot_login: str = ""):
        self.allowlist = set(allowlist)
        self.private_only = private_only
        self._configured_bot_login = bot_login
        self._resolved_bot_login: str | None = None

    def _allowed(self, repository: str) -> None:
        validate_repository_name(repository)
        if repository not in self.allowlist:
            raise GitHubError(f"repository is not allowlisted: {repository}")

    @staticmethod
    def _run(args: list[str], *, json_output: bool = False) -> Any:
        environment = os.environ.copy()
        environment.setdefault("GH_CONFIG_DIR", "/var/lib/openhands-symphony/github")
        process = subprocess.run(
            ["gh", *args],
            env=environment,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip() or f"exit {process.returncode}"
            raise GitHubError(f"gh command failed: {detail}")
        if json_output:
            try:
                return json.loads(process.stdout)
            except json.JSONDecodeError as exc:
                raise GitHubError("gh returned invalid JSON") from exc
        return process.stdout.strip()

    def _bot_login(self) -> str:
        if self._configured_bot_login:
            return self._configured_bot_login
        if self._resolved_bot_login is None:
            user = self._run(["api", "user"], json_output=True)
            self._resolved_bot_login = str(user.get("login") or "")
            if not self._resolved_bot_login:
                raise GitHubError("unable to resolve the authenticated GitHub login")
        return self._resolved_bot_login

    def _paginated_issue_comments(self, repository: str, path: str) -> list[dict[str, Any]]:
        pages = self._run(["api", "--paginate", "--slurp", path], json_output=True)
        if not isinstance(pages, list):
            raise GitHubError("gh returned an invalid paginated comment response")
        comments: list[dict[str, Any]] = []
        for page in pages:
            if not isinstance(page, list):
                raise GitHubError("gh returned an invalid comment page")
            comments.extend(row for row in page if isinstance(row, dict))
        return comments

    def get_issue(self, repository: str, issue_number: int) -> IssueSnapshot:
        self._allowed(repository)
        if issue_number < 1:
            raise GitHubError("issue number must be positive")
        issue = self._run(["api", f"repos/{repository}/issues/{issue_number}"], json_output=True)
        if "pull_request" in issue:
            raise GitHubError(f"{repository}#{issue_number} is a pull request, not an issue")
        repo = self._run(["api", f"repos/{repository}"], json_output=True)
        private = bool(repo.get("private", False))
        if self.private_only and not private:
            raise GitHubError(f"public repositories are disabled: {repository}")
        return IssueSnapshot(
            repository=repository,
            number=int(issue["number"]),
            title=str(issue.get("title") or ""),
            body=str(issue.get("body") or ""),
            state=str(issue.get("state") or ""),
            labels=tuple(sorted(str(value["name"]) for value in issue.get("labels", []))),
            updated_at=str(issue.get("updated_at") or ""),
            private=private,
            default_branch=str(repo.get("default_branch") or "main"),
        )

    def list_ready_issues(self, repository: str) -> list[IssueSnapshot]:
        self._allowed(repository)
        rows = self._run(
            [
                "issue",
                "list",
                "--repo",
                repository,
                "--state",
                "open",
                "--label",
                "agent:ready",
                "--limit",
                "100",
                "--json",
                "number",
            ],
            json_output=True,
        )
        return [self.get_issue(repository, int(row["number"])) for row in rows]

    def find_open_pr(self, repository: str, branch: str) -> PullRequest | None:
        self._allowed(repository)
        rows = self._run(
            [
                "pr",
                "list",
                "--repo",
                repository,
                "--state",
                "open",
                "--head",
                branch,
                "--json",
                "number,url,isDraft,headRefName",
                "--limit",
                "10",
            ],
            json_output=True,
        )
        if not rows:
            return None
        row = rows[0]
        return PullRequest(int(row["number"]), str(row["url"]), bool(row["isDraft"]), str(row["headRefName"]))

    def remote_branch_exists(self, repository: str, branch: str) -> bool:
        self._allowed(repository)
        environment = os.environ.copy()
        environment.setdefault("GH_CONFIG_DIR", "/var/lib/openhands-symphony/github")
        process = subprocess.run(
            ["gh", "api", f"repos/{repository}/git/ref/heads/{branch}"],
            env=environment,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        if process.returncode not in {0, 1}:
            raise GitHubError("unable to check generated branch")
        return process.returncode == 0

    def recent_issue_comments(self, repository: str, issue_number: int) -> list[str]:
        self._allowed(repository)
        rows = self._run(
            ["api", f"repos/{repository}/issues/{issue_number}/comments?per_page=20&sort=created&direction=desc"],
            json_output=True,
        )
        result: list[str] = []
        for row in reversed(rows):
            author = str((row.get("user") or {}).get("login") or "unknown")
            body = str(row.get("body") or "")
            if STATUS_MARKER in body or body.strip().lower().startswith("/agent"):
                continue
            result.append(f"Comment by {author}:\n{body}")
        return result

    def list_control_commands(self, repository: str) -> list[tuple[int, int, str]]:
        """List trusted exact commands repository-wide so reconciliation can recover missed webhooks."""
        self._allowed(repository)
        rows = self._paginated_issue_comments(
            repository,
            f"repos/{repository}/issues/comments?per_page=100&sort=created&direction=asc",
        )
        commands: list[tuple[int, int, str]] = []
        for row in rows:
            command = parse_control_command(str(row.get("body") or ""))
            association = str(row.get("author_association") or "").upper()
            issue_url = str(row.get("issue_url") or "")
            number = issue_url.rsplit("/", 1)[-1]
            comment_id = row.get("id")
            if command and association in TRUSTED_ASSOCIATIONS and number.isdigit() and comment_id is not None:
                commands.append((int(number), int(comment_id), command))
        return commands

    def _live_issue_guard(self, job: Job) -> IssueSnapshot:
        live = self.get_issue(job.repository, job.issue_number)
        if live.state.lower() != "open":
            raise StaleIssueError("issue was closed")
        return live

    def _live_control_guard(self, job: Job) -> IssueSnapshot:
        live = self._live_issue_guard(job)
        labels = set(live.labels)
        if "agent:paused" in labels or "agent:manual-only" in labels:
            raise StaleIssueError("issue is paused or manual-only")
        return live

    def guard_code_mutation(self, job: Job, *, allow_existing_branch: bool) -> IssueSnapshot:
        """Re-read all live inputs immediately before a push or PR mutation."""
        live = self._live_control_guard(job)
        if live.content_hash() != job.content_hash:
            raise StaleIssueError("issue title or body changed after claim")
        decision = route(live, {job.review_provider} if job.review_provider else set())
        if (
            not decision.eligible
            or decision.implementation_provider != job.implementation_provider
            or decision.review_required != job.review_required
            or decision.review_provider != job.review_provider
        ):
            raise StaleIssueError(f"routing changed after claim: {decision.reason or 'provider changed'}")
        pr = self.find_open_pr(job.repository, job.branch)
        if pr and pr.number != job.pr_number:
            raise StaleIssueError(f"an implementation PR already exists: {pr.url}")
        if not allow_existing_branch and self.remote_branch_exists(job.repository, job.branch):
            raise StaleIssueError(f"generated branch already exists: {job.branch}")
        return live

    def _find_status_comment(self, repository: str, issue_number: int) -> int | None:
        comments = self._paginated_issue_comments(
            repository,
            f"repos/{repository}/issues/{issue_number}/comments?per_page=100",
        )
        for comment in comments:
            author = str((comment.get("user") or {}).get("login") or "")
            if author == self._bot_login() and STATUS_MARKER in str(comment.get("body") or ""):
                return int(comment["id"])
        return None

    def _owned_status_comment(self, repository: str, comment_id: int) -> bool:
        try:
            comment = self._run(
                ["api", f"repos/{repository}/issues/comments/{comment_id}"],
                json_output=True,
            )
        except GitHubError:
            return False
        author = str((comment.get("user") or {}).get("login") or "")
        return author == self._bot_login() and STATUS_MARKER in str(comment.get("body") or "")

    def update_status_comment(self, job: Job, body: str) -> int:
        # A status update is control-plane mutation and may explain a pause.
        if job.state == JobState.DONE:
            self.get_issue(job.repository, job.issue_number)
        else:
            self._live_issue_guard(job)
        comment_id = job.status_comment_id
        if comment_id and not self._owned_status_comment(job.repository, comment_id):
            comment_id = None
        comment_id = comment_id or self._find_status_comment(job.repository, job.issue_number)
        rendered = f"{STATUS_MARKER}\n{body}"
        if job.state == JobState.DONE:
            self.get_issue(job.repository, job.issue_number)
        else:
            self._live_issue_guard(job)
        if comment_id:
            self._run(
                [
                    "api",
                    "--method",
                    "PATCH",
                    f"repos/{job.repository}/issues/comments/{comment_id}",
                    "-f",
                    f"body={rendered}",
                ]
            )
            return comment_id
        payload = self._run(
            [
                "api",
                "--method",
                "POST",
                f"repos/{job.repository}/issues/{job.issue_number}/comments",
                "-f",
                f"body={rendered}",
            ],
            json_output=True,
        )
        return int(payload["id"])

    def set_state_labels(self, job: Job, state: JobState) -> None:
        if state == JobState.DONE:
            self.get_issue(job.repository, job.issue_number)
        else:
            self._live_issue_guard(job)
        desired = {
            JobState.QUEUED: "agent:queued",
            JobState.RUNNING: "agent:running",
            JobState.NEEDS_GUIDANCE: "agent:needs-guidance",
            JobState.PR_OPEN: "agent:pr-open",
            JobState.REVIEWING: "agent:pr-open",
            JobState.BLOCKED: "agent:failed",
            JobState.FAILED: "agent:failed",
            JobState.CANCELED: "agent:failed",
            JobState.DONE: "agent:done",
        }[state]
        live = self.get_issue(job.repository, job.issue_number)
        current = set(live.labels) & SYSTEM_STATE_LABELS
        args = ["issue", "edit", str(job.issue_number), "--repo", job.repository]
        for label in sorted(current - {desired}):
            args += ["--remove-label", label]
        if desired not in current:
            args += ["--add-label", desired]
        if len(args) > 6:
            self._run(args)

    def create_draft_pr(self, job: Job, title: str, body: str, generated_label: str) -> PullRequest:
        self.guard_code_mutation(job, allow_existing_branch=True)
        existing = self.find_open_pr(job.repository, job.branch)
        if existing:
            return existing
        output = self._run(
            [
                "pr",
                "create",
                "--repo",
                job.repository,
                "--head",
                job.branch,
                "--draft",
                "--title",
                title,
                "--body",
                body,
            ]
        )
        rows = self._run(
            [
                "pr",
                "list",
                "--repo",
                job.repository,
                "--state",
                "open",
                "--head",
                job.branch,
                "--json",
                "number,url,isDraft,headRefName",
                "--limit",
                "1",
            ],
            json_output=True,
        )
        if not rows:
            raise GitHubError(f"draft PR creation returned no discoverable PR: {output}")
        row = rows[0]
        pr = PullRequest(int(row["number"]), str(row["url"]), bool(row["isDraft"]), str(row["headRefName"]))
        live = self._live_control_guard(job)
        if live.content_hash() != job.content_hash:
            raise StaleIssueError("issue title or body changed before PR labeling")
        self._run(["pr", "edit", str(pr.number), "--repo", job.repository, "--add-label", generated_label])
        return pr

    def update_pr_validation(self, job: Job, validation: str) -> None:
        """Replace the generated PR's validation section after a repair pass."""
        if not job.pr_number:
            raise GitHubError("cannot update validation evidence before a PR exists")
        current = self._run(
            ["pr", "view", str(job.pr_number), "--repo", job.repository, "--json", "body,headRefName,state"],
            json_output=True,
        )
        if str(current.get("state") or "").upper() != "OPEN" or str(current.get("headRefName") or "") != job.branch:
            raise StaleIssueError("implementation PR is no longer open on the generated branch")
        body = str(current.get("body") or "")
        start = body.find("## Validation\n")
        end = body.find("\n## Unresolved risks", start + 1)
        if start < 0 or end < 0:
            raise GitHubError("generated PR body is missing its validation section")
        updated = body[:start] + f"## Validation\n\n{validation}\n" + body[end:]
        self.guard_code_mutation(job, allow_existing_branch=True)
        self._run(["pr", "edit", str(job.pr_number), "--repo", job.repository, "--body", updated])

    def pr_review_context(self, repository: str, pr_number: int) -> dict[str, Any]:
        self._allowed(repository)
        return self._run(
            [
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repository,
                "--json",
                "url,isDraft,baseRefName,headRefName,mergeStateStatus,statusCheckRollup,files,commits",
            ],
            json_output=True,
        )

    def pr_state(self, repository: str, pr_number: int) -> dict[str, Any]:
        self._allowed(repository)
        return self._run(
            [
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repository,
                "--json",
                "url,state,mergedAt,isDraft",
            ],
            json_output=True,
        )

    def post_review(self, repository: str, pr_number: int, body: str, event: str) -> None:
        self._allowed(repository)
        flag = {"approve": "--approve", "request-changes": "--request-changes", "comment": "--comment"}.get(event)
        if flag is None:
            raise GitHubError(f"invalid review event: {event}")
        self._run(["pr", "review", str(pr_number), "--repo", repository, flag, "--body", body])

    def set_control_state(self, repository: str, issue_number: int, command: str) -> None:
        self._allowed(repository)
        live = self.get_issue(repository, issue_number)
        if live.state.lower() != "open":
            raise StaleIssueError("issue was closed")
        labels = set(live.labels)
        args = ["issue", "edit", str(issue_number), "--repo", repository]
        if command == "pause":
            if "agent:paused" not in labels:
                args += ["--add-label", "agent:paused"]
        elif command in {"resume", "retry"}:
            if "agent:paused" in labels:
                args += ["--remove-label", "agent:paused"]
            if "agent:ready" not in labels:
                args += ["--add-label", "agent:ready"]
        elif command == "cancel":
            if "agent:ready" in labels:
                args += ["--remove-label", "agent:ready"]
        if len(args) == 5:
            return
        self._run(args)

    def ensure_contract_labels(self, repository: str, labels: dict[str, tuple[str, str]]) -> None:
        self._allowed(repository)
        for name, (color, description) in labels.items():
            self._run(
                [
                    "label",
                    "create",
                    name,
                    "--repo",
                    repository,
                    "--force",
                    "--color",
                    color,
                    "--description",
                    description,
                ]
            )
