from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from .config import Config, load_config
from .github import GhCLIBackend, GitHubError
from .ideas_contract import safe_path, validate_spec
from .ideas_github import RUNTIME_PATH, GhIdeasBackend
from .ideas_progress import IdeaSection, SectionResult, affected_sections, mirrored_spec, previous_results, sections
from .intake import validate_repository_name
from .models import IdeaSnapshot
from .preview_queue import PreviewQueue
from .store import Store
from .validation import redact
from .workspace import DEFAULT_GH_CONFIG_DIR

ISSUE_MARKER_PREFIX = "<!-- openhands-symphony-graduation:"
ARCHIVE_ROOT = "archive/ideas"
GRADUATION_LOCK = "graduate"
INTAKE_LABELS = {"agent:ready", "agent:claude", "agent:codex", "agent:antigravity"}


class GraduationError(RuntimeError):
    pass


def guard_hack_campaign(config: Config, repository: str, store: Store | None = None) -> None:
    """Read-only graduation fence, including dry runs before a Store is opened."""
    if store is not None:
        active = store.hack_active(repository)
    else:
        database = config.service.state_dir / "state.db"
        if not database.exists():
            return
        try:
            with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                has_campaigns = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hack_campaigns'"
                ).fetchone()
                active = bool(has_campaigns and connection.execute(
                    "SELECT 1 FROM hack_campaigns WHERE repository=? "
                    "AND state IN ('starting','active','draining','polishing','publishing')",
                    (repository,),
                ).fetchone())
        except sqlite3.Error as exc:
            raise GraduationError("cannot verify campaign state before graduation") from exc
    if active:
        raise GraduationError("repository has an active hack campaign; finish the campaign before graduation")


@dataclass(frozen=True)
class GraduationIssue:
    slug: str
    title: str
    body: str
    status: str
    marker: str


@dataclass(frozen=True)
class GraduationPlan:
    repository: str
    spec_hash: str
    base_commit: str
    default_branch: str
    config_sha256: str
    spec_path: str
    progress_path: str
    archive_prefix: str
    issues: tuple[GraduationIssue, ...]

    @property
    def approval_id(self) -> str:
        payload = asdict(self)
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]


@dataclass(frozen=True)
class GraduationResult:
    plan: GraduationPlan
    issue_urls: tuple[str, ...]
    archive_commit: str
    retired_runs: tuple[str, ...]
    warnings: tuple[str, ...] = ()


class GraduationBackend(Protocol):
    def get_snapshot(self, repository: str, spec_path: str, progress_path: str) -> IdeaSnapshot: ...

    def ensure_issues(self, repository: str, issues: tuple[GraduationIssue, ...]) -> tuple[str, ...]: ...

    def archive_idea_files(
        self,
        repository: str,
        *,
        expected_commit: str,
        default_branch: str,
        spec_path: str,
        progress_path: str,
        archive_prefix: str,
    ) -> str: ...


def _api_json(method: str, path: str, payload: dict[str, object]) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.setdefault("GH_CONFIG_DIR", DEFAULT_GH_CONFIG_DIR)
    process = subprocess.run(
        ["gh", "api", "--method", method, path, "--input", "-"],
        input=json.dumps(payload),
        env=environment,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or f"exit {process.returncode}"
        raise GitHubError(f"gh command failed: {redact(detail, 4000)}")
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubError("gh returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise GitHubError("gh returned an invalid object")
    return result


class GhGraduationBackend(GhIdeasBackend):
    """GitHub mutations used only after an exact graduation plan is approved."""

    def _assert_private(self, repository: str) -> None:
        self._allowed(repository)
        metadata = GhCLIBackend._run(["api", f"repos/{repository}"], json_output=True)
        if self.private_only and not bool(metadata.get("private", False)):
            raise GitHubError(f"public ideas repositories are disabled: {repository}")

    def ensure_issues(self, repository: str, issues: tuple[GraduationIssue, ...]) -> tuple[str, ...]:
        self._assert_private(repository)
        pages = GhCLIBackend._run(
            ["api", "--paginate", "--slurp", f"repos/{repository}/issues?state=all&per_page=100"],
            json_output=True,
        )
        if not isinstance(pages, list):
            raise GitHubError("gh returned an invalid issue list")
        existing: dict[str, dict[str, Any]] = {}
        for page in pages:
            if not isinstance(page, list):
                raise GitHubError("gh returned an invalid issue page")
            for row in page:
                if not isinstance(row, dict) or "pull_request" in row:
                    continue
                body = str(row.get("body") or "")
                for issue in issues:
                    if issue.marker in body:
                        existing[issue.marker] = row

        urls: list[str] = []
        for issue in issues:
            row = existing.get(issue.marker)
            if row:
                try:
                    issue_number = int(row["number"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise GitHubError("GitHub returned a marked issue without a valid number") from exc
                labels = {
                    str(label.get("name") or "")
                    for label in row.get("labels", [])
                    if isinstance(label, dict) and label.get("name")
                }
                updated = _api_json(
                    "PATCH",
                    f"repos/{repository}/issues/{issue_number}",
                    {
                        "title": issue.title,
                        "body": issue.body,
                        "state": "open",
                        "labels": sorted(labels - INTAKE_LABELS),
                    },
                )
                url = str(updated.get("html_url") or row.get("html_url") or "")
            else:
                created = _api_json(
                    "POST",
                    f"repos/{repository}/issues",
                    {"title": issue.title, "body": issue.body, "labels": []},
                )
                url = str(created.get("html_url") or "")
            if not url:
                raise GitHubError("GitHub created or updated an issue without returning its URL")
            urls.append(url)
        return tuple(urls)

    def archive_idea_files(
        self,
        repository: str,
        *,
        expected_commit: str,
        default_branch: str,
        spec_path: str,
        progress_path: str,
        archive_prefix: str,
    ) -> str:
        self._assert_private(repository)
        spec_path = safe_path(spec_path)
        progress_path = safe_path(progress_path)
        archive_prefix = safe_path(archive_prefix)
        commit = GhCLIBackend._run(
            ["api", f"repos/{repository}/commits/{quote(default_branch, safe='/')}"], json_output=True
        )
        if str(commit.get("sha") or "") != expected_commit:
            raise GraduationError("default branch moved after the approved dry run; run graduation again")
        base_tree_sha = str(((commit.get("commit") or {}).get("tree") or {}).get("sha") or "")
        if not base_tree_sha:
            raise GitHubError("GitHub returned a commit without its base tree SHA")
        tree = GhCLIBackend._run(
            ["api", f"repos/{repository}/git/trees/{expected_commit}?recursive=1"], json_output=True
        )
        if bool(tree.get("truncated")):
            raise GraduationError("repository tree is too large to archive the idea contract safely")
        rows = [row for row in tree.get("tree", []) if isinstance(row, dict)]
        if any(str(row.get("path") or "").startswith(f"{archive_prefix}/") for row in rows):
            raise GraduationError(f"graduation archive already exists: {archive_prefix}")

        assets_parent = Path(progress_path).parent / "assets"
        assets_prefix = f"{assets_parent.as_posix().rstrip('/')}/"
        exact_paths = {spec_path, progress_path, RUNTIME_PATH}
        selected = [
            row
            for row in rows
            if row.get("type") == "blob"
            and (str(row.get("path") or "") in exact_paths or str(row.get("path") or "").startswith(assets_prefix))
        ]
        selected_paths = {str(row.get("path") or "") for row in selected}
        missing = {spec_path, RUNTIME_PATH} - selected_paths
        if missing:
            raise GraduationError(f"idea contract changed before archival; missing: {', '.join(sorted(missing))}")

        edits: list[dict[str, object]] = []
        for row in selected:
            source = str(row["path"])
            edits.append(
                {
                    "path": f"{archive_prefix}/{source}",
                    "mode": str(row.get("mode") or "100644"),
                    "type": "blob",
                    "sha": str(row["sha"]),
                }
            )
            edits.append({"path": source, "mode": str(row.get("mode") or "100644"), "type": "blob", "sha": None})

        created_tree = _api_json(
            "POST",
            f"repos/{repository}/git/trees",
            {"base_tree": base_tree_sha, "tree": edits},
        )
        tree_sha = str(created_tree.get("sha") or "")
        if not tree_sha:
            raise GitHubError("GitHub created an archive tree without returning its SHA")
        created_commit = _api_json(
            "POST",
            f"repos/{repository}/git/commits",
            {
                "message": f"Archive Ideas contract after graduation ({expected_commit[:12]})",
                "tree": tree_sha,
                "parents": [expected_commit],
            },
        )
        commit_sha = str(created_commit.get("sha") or "")
        if not commit_sha:
            raise GitHubError("GitHub created an archive commit without returning its SHA")
        _api_json(
            "PATCH",
            f"repos/{repository}/git/refs/heads/{quote(default_branch, safe='/')}",
            {"sha": commit_sha, "force": False},
        )
        return commit_sha


def _trusted_results(spec: bytes, progress: bytes) -> dict[str, SectionResult]:
    if not progress:
        return {}
    results = previous_results(progress)
    previous_spec = mirrored_spec(progress)
    changed = {section.slug for section in affected_sections(previous_spec, spec)}
    return {
        section.slug: results[section.slug]
        for section in sections(spec)
        if section.slug in results and section.slug not in changed
    }


def _issue_for_section(
    repository: str,
    spec_hash: str,
    spec_path: str,
    section: IdeaSection,
    result: SectionResult | None,
) -> GraduationIssue:
    status = result.status if result else "not started"
    summary = result.summary if result else "No current result was recorded for this wish."
    title = section.title.strip()
    if not title:
        raise GraduationError("idea spec contains an empty section title")
    if len(title) > 256:
        title = f"{title[:253]}..."
    prose = section.body.decode("utf-8").strip()
    marker = f"{ISSUE_MARKER_PREFIX}{spec_hash}:{section.slug} -->"
    body_parts = [prose] if prose else ["_The Ideas spec contained no prose below this heading._"]
    body_parts.extend(
        [
            "---",
            f"Graduated from `{spec_path}` (`## {section.title}`) in `{repository}`.",
            f"Ideas status at graduation: **{status}** — {summary}",
            "This issue is intentionally unrouted. Approve Tier 1 agent work by adding `agent:ready` "
            "and exactly one `agent:*` provider label.",
            marker,
        ]
    )
    body = "\n\n".join(body_parts)
    if len(body.encode("utf-8")) > 65_536:
        raise GraduationError(f"section is too large for one GitHub issue: {section.title}")
    return GraduationIssue(section.slug, title, body, status, marker)


def _array_span(text: str, section: str, key: str) -> tuple[int, int]:
    table = re.search(rf"(?m)^[ \t]*\[{re.escape(section)}\][ \t]*(?:#.*)?$", text)
    if not table:
        raise GraduationError(f"config is missing [{section}]")
    next_table = re.search(r"(?m)^[ \t]*\[", text[table.end() :])
    section_end = table.end() + next_table.start() if next_table else len(text)
    assignment = re.search(rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=", text[table.end() : section_end])
    if not assignment:
        raise GraduationError(f"config is missing {section}.{key}")
    value_start = table.end() + assignment.end()
    opening = text.find("[", value_start, section_end)
    if opening < 0:
        raise GraduationError(f"config value {section}.{key} is not an array")

    depth = 0
    quote_char = ""
    escaped = False
    comment = False
    for index in range(opening, section_end):
        character = text[index]
        if comment:
            if character == "\n":
                comment = False
            continue
        if quote_char:
            if quote_char == '"' and escaped:
                escaped = False
            elif quote_char == '"' and character == "\\":
                escaped = True
            elif character == quote_char:
                quote_char = ""
            continue
        if character == "#":
            comment = True
        elif character in {'"', "'"}:
            quote_char = character
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return opening, index + 1
    raise GraduationError(f"config array {section}.{key} is not closed")


def graduated_config_bytes(path: Path, config: Config, repository: str) -> bytes:
    original = path.read_text(encoding="utf-8")
    github_repositories = (*config.github.allowed_repositories, repository)
    ideas_repositories = tuple(value for value in config.ideas.repositories if value != repository)
    updated = original
    for section, key, values in (
        ("github", "allowed_repositories", github_repositories),
        ("ideas", "repositories", ideas_repositories),
    ):
        start, end = _array_span(updated, section, key)
        updated = f"{updated[:start]}{json.dumps(values)}{updated[end:]}"
    return updated.encode("utf-8")


def _atomic_exchange(first: Path, second: Path) -> None:
    """Atomically exchange two same-filesystem paths or fail closed."""

    library = ctypes.CDLL(None, use_errno=True)
    first_bytes = os.fsencode(first)
    second_bytes = os.fsencode(second)
    result = -1
    if sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        renameat2 = library.renameat2
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, first_bytes, -100, second_bytes, 2)  # AT_FDCWD, RENAME_EXCHANGE
    elif sys.platform == "darwin" and hasattr(library, "renamex_np"):
        renamex = library.renamex_np
        renamex.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex.restype = ctypes.c_int
        result = renamex(first_bytes, second_bytes, 2)  # RENAME_SWAP
    else:
        raise GraduationError("this platform cannot atomically compare-and-replace the Symphony configuration")
    if result != 0:
        error = ctypes.get_errno()
        raise GraduationError(f"unable to atomically exchange the Symphony configuration: {os.strerror(error)}")


def _atomic_write(path: Path, content: bytes, *, expected: bytes | None = None) -> None:
    info = path.stat()
    if os.geteuid() not in {0, info.st_uid}:
        raise GraduationError(f"graduation must run as the owner of {path} or as root")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.graduation.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, info.st_mode & 0o7777)
        if os.geteuid() == 0:
            os.chown(temporary, info.st_uid, info.st_gid)
        if expected is None:
            os.replace(temporary, path)
        else:
            temporary_path = Path(temporary)
            _atomic_exchange(path, temporary_path)
            observed = temporary_path.read_bytes()
            if observed != expected:
                # If the replacement itself is still current, restore the
                # bytes observed at the atomic exchange. If another writer has
                # already replaced it, leave that newer edit untouched.
                if path.read_bytes() == content:
                    _atomic_exchange(path, temporary_path)
                raise GraduationError("configuration changed during graduation; review a new dry run")
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _preflight_atomic_write(path: Path) -> None:
    info = path.stat()
    if os.geteuid() not in {0, info.st_uid}:
        raise GraduationError(f"graduation must run as the owner of {path} or as root")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.graduation-check.", dir=path.parent)
    os.close(descriptor)
    Path(temporary).unlink()


class Graduator:
    def __init__(
        self,
        config: Config,
        config_path: Path,
        backend: GraduationBackend,
        *,
        store: Store | None = None,
    ):
        self.config = config
        self.config_path = config_path
        self.backend = backend
        self.store = store

    def plan(self, repository: str) -> GraduationPlan:
        validate_repository_name(repository)
        guard_hack_campaign(self.config, repository, self.store)
        if repository not in self.config.ideas.repositories:
            raise GraduationError(f"repository is not ideas-allowlisted: {repository}")
        if repository in self.config.github.allowed_repositories:
            raise GraduationError(f"repository is already Tier 1 allowlisted: {repository}")
        original_config = self.config_path.read_bytes()
        snapshot = self.backend.get_snapshot(repository, self.config.ideas.spec_path, self.config.ideas.progress_path)
        if not snapshot.private:
            raise GraduationError("public ideas repositories cannot graduate through the unattended workflow")
        validate_spec(snapshot.spec_content, repository)
        trusted = _trusted_results(snapshot.spec_content, snapshot.previous_progress)
        issues = tuple(
            _issue_for_section(
                repository,
                snapshot.spec_hash,
                self.config.ideas.spec_path,
                section,
                trusted.get(section.slug),
            )
            for section in sections(snapshot.spec_content)
            if trusted.get(section.slug, SectionResult("not started", "")).status != "done"
        )
        return GraduationPlan(
            repository=repository,
            spec_hash=snapshot.spec_hash,
            base_commit=snapshot.base_commit,
            default_branch=snapshot.default_branch,
            config_sha256=hashlib.sha256(original_config).hexdigest(),
            spec_path=self.config.ideas.spec_path,
            progress_path=self.config.ideas.progress_path,
            archive_prefix=f"{ARCHIVE_ROOT}/{snapshot.spec_hash[:12]}",
            issues=issues,
        )

    def apply(
        self,
        repository: str,
        approval_id: str,
        *,
        operation_lock_held: bool = False,
    ) -> GraduationResult:
        plan = self.plan(repository)
        if approval_id != plan.approval_id:
            raise GraduationError(
                "approval does not match the current spec/config/default branch; review a new dry run"
            )
        owner = f"graduate-{os.getpid()}"
        acquired_lock = False
        if operation_lock_held and self.store is None:
            raise GraduationError("an externally held graduation lock requires the durable store")
        if self.store and not operation_lock_held:
            acquired_lock = self.store.acquire_operation_lock(GRADUATION_LOCK, owner, seconds=7200)
            if not acquired_lock:
                raise GraduationError("another graduation operation is active")
        try:
            guard_hack_campaign(self.config, repository, self.store)
            original_config = self.config_path.read_bytes()
            if hashlib.sha256(original_config).hexdigest() != plan.config_sha256:
                raise GraduationError("configuration changed during graduation; review a new dry run")
            updated_config = graduated_config_bytes(self.config_path, self.config, repository)

            # Validate the complete candidate before any GitHub mutation.
            descriptor, candidate_name = tempfile.mkstemp(prefix="graduation-config-", suffix=".toml")
            try:
                with os.fdopen(descriptor, "wb") as candidate:
                    candidate.write(updated_config)
                new_config = load_config(candidate_name)
            finally:
                Path(candidate_name).unlink(missing_ok=True)

            _preflight_atomic_write(self.config_path)
            retired: list[str] = []
            previous_preview_state: str | None = None
            preview_allowlist_updated = False
            config_updated = False
            preview_queue = PreviewQueue(new_config.service.preview_dir, new_config.service.workspace_dir)
            try:
                # Claim the approved configuration before any external issue
                # mutation. This compare-and-replace is the tier transition's
                # linearization point; later rollback is conditional too, so a
                # subsequent operator edit is never overwritten.
                _atomic_write(self.config_path, updated_config, expected=original_config)
                config_updated = True

                if self.store:
                    retired = self.store.supersede_active_idea_runs(
                        repository,
                        "repository graduated to Tier 1",
                    )
                    project = self.store.get_idea_project(repository)
                    if project:
                        previous_preview_state = project.preview_state
                        self.store.update_idea_preview(repository, "stopped")

                preview_queue.publish_allowlist(new_config.ideas.repositories)
                preview_allowlist_updated = True
                if self.config_path.read_bytes() != updated_config:
                    raise GraduationError("configuration changed during graduation; review a new dry run")
                issue_urls = self.backend.ensure_issues(repository, plan.issues)
                archive_commit = self.backend.archive_idea_files(
                    repository,
                    expected_commit=plan.base_commit,
                    default_branch=plan.default_branch,
                    spec_path=plan.spec_path,
                    progress_path=plan.progress_path,
                    archive_prefix=plan.archive_prefix,
                )
            except Exception as exc:
                rollback_errors: list[str] = []
                if config_updated:
                    try:
                        _atomic_write(self.config_path, original_config, expected=updated_config)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"configuration: {type(rollback_exc).__name__}: {rollback_exc}")
                if preview_allowlist_updated:
                    try:
                        preview_queue.publish_allowlist(self.config.ideas.repositories)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"preview allowlist: {type(rollback_exc).__name__}: {rollback_exc}")
                if self.store and previous_preview_state is not None:
                    try:
                        self.store.update_idea_preview(repository, previous_preview_state)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"preview state: {type(rollback_exc).__name__}: {rollback_exc}")
                if self.store and retired:
                    try:
                        self.store.restore_graduated_idea_runs(
                            repository,
                            retired,
                            "graduation failed before archival; work requeued",
                        )
                    except Exception as rollback_exc:
                        rollback_errors.append(f"Ideas runs: {type(rollback_exc).__name__}: {rollback_exc}")
                if rollback_errors:
                    detail = "; ".join(redact(value, 1000) for value in rollback_errors)
                    raise GraduationError(
                        f"graduation failed and local rollback was incomplete: {detail}"
                    ) from exc
                raise

            return GraduationResult(plan, issue_urls, archive_commit, tuple(retired))
        finally:
            if self.store and acquired_lock:
                self.store.release_operation_lock(GRADUATION_LOCK, owner)


def render_plan(plan: GraduationPlan, *, config_path: Path) -> str:
    lines = [
        "Graduation dry run (no GitHub or configuration changes made)",
        f"repository: {plan.repository}",
        f"default branch: {plan.default_branch}@{plan.base_commit}",
        f"archive: {plan.archive_prefix}/",
        f"allowlist: ideas -> Tier 1 in {config_path}",
        f"unfinished issues: {len(plan.issues)}",
    ]
    for index, issue in enumerate(plan.issues, 1):
        lines.extend(["", f"[{index}] {issue.title}", f"status: {issue.status}", issue.body])
    lines.extend(
        [
            "",
            f"approval: {plan.approval_id}",
            f"Apply exactly this plan with: sudo agentctl --config {config_path} graduate {plan.repository} "
            f"--approve {plan.approval_id}",
        ]
    )
    return "\n".join(lines)
