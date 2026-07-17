from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from symphony.config import Config, GitHubConfig, ProviderConfig, RepositoryConfig, SchedulerConfig, ServiceConfig
from symphony.github import PullRequest, StaleIssueError
from symphony.intake import route
from symphony.models import IssueSnapshot, Job, JobState


class FakeGitHub:
    def __init__(self, snapshots: list[IssueSnapshot]):
        self.issues = {(item.repository, item.number): item for item in snapshots}
        self.prs: dict[tuple[str, str], PullRequest] = {}
        self.branches: set[tuple[str, str]] = set()
        self.comment_ids: dict[tuple[str, int], int] = {}
        self.comment_bodies: dict[tuple[str, int], str] = {}
        self.comment_creates = 0
        self.label_updates = 0
        self.pr_creates = 0
        self.guard_calls = 0
        self.reviews: list[tuple[str, int, str, str]] = []
        self.pr_bodies: list[str] = []
        self.pr_body_updates: list[str] = []
        self.merged_prs: set[tuple[str, int]] = set()
        self.control_commands: dict[str, list[tuple[int, int, str]]] = {}

    def get_issue(self, repository: str, issue_number: int) -> IssueSnapshot:
        return self.issues[(repository, issue_number)]

    def set_issue(self, snapshot: IssueSnapshot) -> None:
        self.issues[(snapshot.repository, snapshot.number)] = snapshot

    def list_ready_issues(self, repository: str) -> list[IssueSnapshot]:
        return [
            issue
            for issue in self.issues.values()
            if issue.repository == repository and "agent:ready" in issue.labels and issue.state == "open"
        ]

    def find_open_pr(self, repository: str, branch: str) -> PullRequest | None:
        return self.prs.get((repository, branch))

    def remote_branch_exists(self, repository: str, branch: str) -> bool:
        return (repository, branch) in self.branches

    def recent_issue_comments(self, repository: str, issue_number: int) -> list[str]:
        return []

    def list_control_commands(self, repository: str) -> list[tuple[int, int, str]]:
        return self.control_commands.get(repository, [])

    def guard_code_mutation(self, job: Job, *, allow_existing_branch: bool) -> IssueSnapshot:
        self.guard_calls += 1
        issue = self.get_issue(job.repository, job.issue_number)
        if issue.state != "open" or "agent:paused" in issue.labels or "agent:manual-only" in issue.labels:
            raise StaleIssueError("issue is no longer mutable")
        if issue.content_hash() != job.content_hash:
            raise StaleIssueError("issue title or body changed after claim")
        decision = route(issue, {job.review_provider} if job.review_provider else set())
        if (
            not decision.eligible
            or decision.implementation_provider != job.implementation_provider
            or decision.review_required != job.review_required
            or decision.review_provider != job.review_provider
        ):
            raise StaleIssueError("routing changed after claim")
        existing = self.find_open_pr(job.repository, job.branch)
        if existing and existing.number != job.pr_number:
            raise StaleIssueError("PR already exists")
        if (job.repository, job.branch) in self.branches and not allow_existing_branch:
            raise StaleIssueError("branch already exists")
        return issue

    def update_status_comment(self, job: Job, body: str) -> int:
        key = (job.repository, job.issue_number)
        if key not in self.comment_ids:
            self.comment_creates += 1
            self.comment_ids[key] = 100 + self.comment_creates
        self.comment_bodies[key] = body
        return self.comment_ids[key]

    def set_state_labels(self, job: Job, state: JobState) -> None:
        self.label_updates += 1

    def set_control_state(self, repository: str, issue_number: int, command: str) -> None:
        issue = self.get_issue(repository, issue_number)
        labels = set(issue.labels)
        if command == "pause":
            labels.add("agent:paused")
        elif command in {"resume", "retry"}:
            labels.discard("agent:paused")
            labels.add("agent:ready")
        elif command == "cancel":
            labels.discard("agent:ready")
        self.set_issue(replace(issue, labels=tuple(sorted(labels))))

    def create_draft_pr(self, job: Job, title: str, body: str, generated_label: str) -> PullRequest:
        existing = self.find_open_pr(job.repository, job.branch)
        if existing:
            return existing
        self.pr_creates += 1
        pr = PullRequest(
            self.pr_creates, f"https://example.test/{job.repository}/pull/{self.pr_creates}", True, job.branch
        )
        self.prs[(job.repository, job.branch)] = pr
        self.branches.add((job.repository, job.branch))
        self.pr_bodies.append(body)
        return pr

    def post_review(self, repository: str, pr_number: int, body: str, event: str) -> None:
        self.reviews.append((repository, pr_number, body, event))

    def update_pr_validation(self, job: Job, validation: str) -> None:
        self.pr_body_updates.append(validation)

    def pr_review_context(self, repository: str, pr_number: int) -> dict[str, object]:
        return {
            "url": f"https://example.test/{repository}/pull/{pr_number}",
            "isDraft": True,
            "statusCheckRollup": [{"name": "tests", "conclusion": "SUCCESS"}],
        }

    def pr_state(self, repository: str, pr_number: int) -> dict[str, object]:
        return {
            "state": "MERGED" if (repository, pr_number) in self.merged_prs else "OPEN",
            "mergedAt": "2026-07-16T12:00:00Z" if (repository, pr_number) in self.merged_prs else None,
            "isDraft": (repository, pr_number) not in self.merged_prs,
        }

    def ensure_contract_labels(self, repository: str, labels: dict[str, tuple[str, str]]) -> None:
        return None


def issue(
    repository: str = "solo/project",
    number: int = 1,
    *,
    labels: tuple[str, ...] = ("agent:ready", "agent:codex"),
    title: str = "Add greeting",
    body: str = "Add a greeting file and test it.",
) -> IssueSnapshot:
    return IssueSnapshot(repository, number, title, body, "open", labels, "2026-07-16T00:00:00Z")


def make_config(tmp_path: Path, repositories: tuple[str, ...] = ("solo/project",), review: bool = False) -> Config:
    providers = {
        "codex": ProviderConfig(True, "fake", ("fake",), ("fake",), timeout_seconds=30),
    }
    limits = {"codex": 2}
    if review:
        providers["claude"] = ProviderConfig(True, "fake", ("fake",), ("fake",), timeout_seconds=30)
        limits["claude"] = 2
    repository_config = {
        repository: RepositoryConfig(
            validation_commands=(
                ("python3", "-c", "from pathlib import Path; assert Path('implemented.txt').read_text() == 'ok\\n'"),
            )
        )
        for repository in repositories
    }
    return Config(
        ServiceConfig(
            state_dir=tmp_path / "state",
            workspace_dir=tmp_path / "workspaces",
            report_dir=tmp_path / "reports",
            log_dir=tmp_path / "logs",
            webhook_secret_file=tmp_path / "webhook-secret",
            validation_user="",
        ),
        GitHubConfig(repositories),
        SchedulerConfig(
            poll_seconds=1,
            reconcile_seconds=60,
            lease_seconds=30,
            heartbeat_seconds=1,
            global_concurrency=3,
            provider_concurrency=limits,
        ),
        providers,
        repository_config,
    )


class ExistingWorkspace:
    def __init__(self, worktree: Path):
        self.worktree = worktree

    def checkout(self, job: Job, snapshot: IssueSnapshot) -> Path:
        return self.worktree

    def run_setup(self, worktree: Path, setup_script: str, validation_user: str):
        return None

    def prepare_for_agent(self, worktree: Path) -> None:
        return None

    @staticmethod
    def has_changes(worktree: Path) -> bool:
        from symphony.workspace import WorkspaceManager

        return WorkspaceManager.has_changes(worktree)

    @staticmethod
    def commits_ahead(worktree: Path, default_branch: str) -> int:
        from symphony.workspace import WorkspaceManager

        return WorkspaceManager.commits_ahead(worktree, default_branch)

    @staticmethod
    def remote_matches(worktree: Path, repository: str, branch: str) -> bool:
        local = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=worktree, text=True, capture_output=True, check=True
        ).stdout.strip()
        remote = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        return bool(remote) and remote.split()[0] == local

    @staticmethod
    def verify_integrity(job: Job, worktree: Path) -> None:
        return None

    @staticmethod
    def commit(worktree: Path, issue_number: int) -> str:
        from symphony.workspace import WorkspaceManager

        return WorkspaceManager.commit(worktree, issue_number)

    @staticmethod
    def push(worktree: Path, repository: str, branch: str) -> None:
        subprocess.run(["git", "push", "--set-upstream", "origin", branch], cwd=worktree, check=True)


def create_worktree(tmp_path: Path, branch: str) -> Path:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    worktree = tmp_path / "run"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, stdout=subprocess.DEVNULL)
    (source / "README.md").write_text("# test\n")
    subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(["git", "-C", str(source), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(source), "push", "-u", "origin", "main"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    subprocess.run(["git", "clone", str(remote), str(worktree)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(worktree), "checkout", "-b", branch], check=True, stdout=subprocess.DEVNULL)
    return worktree


@pytest.fixture
def default_issue() -> IssueSnapshot:
    return issue()
