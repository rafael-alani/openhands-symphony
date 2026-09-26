"""Syncthing-backed note intake. The vault is the UI; Git remains code transport."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from .agent_settings import AgentSettings
from .config import Config, RepositoryConfig
from .github import GhCLIBackend, GitHubError
from .ideas_contract import git_blob_hash
from .ideas_github import GhIdeasBackend
from .intake import validate_repository_name
from .labels import LABEL_CONTRACT
from .store import Store
from .validation import redact
from .vault_project import VaultError, compile_project

MODES = {"idea", "github", "paused"}


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader: UniqueLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise VaultError("frontmatter requires unique string keys")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@dataclass(frozen=True)
class Note:
    path: Path
    raw: bytes
    header_end: int
    mode: str
    repository: str | None
    body: str
    settings: AgentSettings = AgentSettings()

    def spec_header(self, repository: str) -> str:
        overrides = "".join(f"{key}: {value}\n" for key, value in self.settings.values().items())
        return f"---\nsymphony: idea\nrepo: {repository}\n{overrides}---\n\n"

    def spec(self, repository: str) -> bytes:
        body = self.body
        if not re.search(r"(?m)^##[ \t]+\S", body):
            body = "## Project\n\n" + body
        return (self.spec_header(repository) + body).encode()


def read_note(path: Path) -> Note | None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
        raise VaultError("note must be a regular Markdown file smaller than 2 MB")
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not match or not re.search(r"(?m)^symphony\s*:", match[1]):
        return None
    try:
        fields = yaml.load(match[1], Loader=UniqueLoader)
    except yaml.YAMLError as exc:
        raise VaultError("invalid YAML frontmatter") from exc
    mode = fields.get("symphony")
    if not isinstance(mode, str) or mode not in MODES:
        raise VaultError("symphony must be idea, github, or paused")
    repository = fields.get("repo")
    if repository is not None:
        if not isinstance(repository, str):
            raise VaultError("repo must be owner/name")
        validate_repository_name(repository)
    try:
        settings = AgentSettings.parse(fields)
    except ValueError as exc:
        raise VaultError(str(exc)) from exc
    return Note(path, raw, match.end(), mode, repository, text[match.end():], settings)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise VaultError("refusing to replace a symlink")
    descriptor, temporary = tempfile.mkstemp(prefix=".symphony-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Syncthing's account must be able to read generated files.
        os.chmod(temporary, 0o664)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def attach_repository(note: Note, repository: str, backup_dir: Path) -> Note:
    """Add only the stable repo property; preserve original bytes before a guarded update."""
    if note.repository:
        return note
    opening = re.match(rb"\A(?:\xef\xbb\xbf)?---(\r?\n)", note.raw)
    if opening is None:
        raise VaultError("note lost its frontmatter delimiter")
    newline = opening[1]
    start = opening.end()
    updated = note.raw[:start] + b"repo: " + repository.encode() + newline + note.raw[start:]
    return replace_note(note, updated, backup_dir)


def replace_note(note: Note, updated: bytes, backup_dir: Path) -> Note:
    """Guarded wrapper-only edit: repository routing or checklist characters, never prose."""
    if note.path.read_bytes() != note.raw:
        raise VaultError("note changed during update; retry after it settles")
    backup = backup_dir / f"{hashlib.sha256(note.raw).hexdigest()}.md"
    if not backup.exists():
        _atomic_write(backup, note.raw)
    # Atomic exchange retains the exact inode being replaced, including a
    # Syncthing edit racing the final read. Keep its bytes before discarding it.
    from .graduation import _atomic_exchange

    descriptor, temporary = tempfile.mkstemp(prefix=".symphony-register-", dir=note.path.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        staged.chmod(note.path.stat().st_mode & 0o777)
        _atomic_exchange(note.path, staged)
        observed = staged.read_bytes()
        if observed != note.raw:
            _atomic_write(backup_dir / f"{hashlib.sha256(observed).hexdigest()}.md", observed)
            if note.path.read_bytes() == updated:
                _atomic_exchange(note.path, staged)
            raise VaultError("note changed during update; concurrent bytes preserved")
    finally:
        staged.unlink(missing_ok=True)
    result = read_note(note.path)
    if result is None:
        raise VaultError("registered note lost its routing header")
    return result


class GhVaultBackend:
    @staticmethod
    def api(method: str, path: str, payload: dict[str, Any] | None = None) -> dict:
        environment = os.environ.copy()
        environment.setdefault("GH_CONFIG_DIR", "/var/lib/openhands-symphony/github")
        command = ["gh", "api", "--method", method, path]
        if payload is not None:
            command += ["--input", "-"]
        process = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                                 env=environment, text=True, capture_output=True, timeout=120, check=False)
        if process.returncode:
            raise GitHubError(redact(process.stderr or process.stdout, 3000))
        result = json.loads(process.stdout)
        if not isinstance(result, dict):
            raise GitHubError("GitHub returned a non-object response")
        return result

    def ensure_repository(self, repository: str, *, managed: bool) -> None:
        description = f"Symphony vault project: {repository}"
        try:
            metadata = self.api("GET", f"repos/{repository}")
        except GitHubError as exc:
            if "HTTP 404" not in str(exc):
                raise
            GhCLIBackend._run(["repo", "create", repository, "--private", "--add-readme",
                               "--description", description])
            metadata = self.api("GET", f"repos/{repository}")
        if not metadata.get("private") or metadata.get("archived"):
            raise VaultError("vault projects require an active private repository")
        if managed and metadata.get("description") != description:
            raise VaultError("automatic repository name already belongs to another project; set repo explicitly")

    def _tree(self, repository: str) -> tuple[str, str, str, dict[str, dict]]:
        metadata = self.api("GET", f"repos/{repository}")
        if not metadata.get("private"):
            raise VaultError("repository is no longer private")
        branch = str(metadata["default_branch"])
        head = self.api("GET", f"repos/{repository}/commits/{quote(branch, safe='')}")["sha"]
        tree = self.api("GET", f"repos/{repository}/git/trees/{head}?recursive=1")
        if tree.get("truncated"):
            raise VaultError("repository tree is truncated")
        return branch, head, tree["sha"], {entry["path"]: entry for entry in tree["tree"]}

    def sync_spec(self, repository: str, spec: bytes, provider: str, port: int, spec_path: str) -> None:
        """One atomic, non-forced commit; retry only a concurrent branch advance."""
        runtime = (f'provider = "{provider}"\n[preview]\n'
                   'start = ["python3", ".symphony/preview.py", "{port}"]\n'
                   f'port = {port}\nhealth_path = "/"\nstartup_timeout_seconds = 60\n').encode()
        bootstrap = {
            ".symphony/idea.toml": runtime,
            ".symphony/preview.py": b'import http.server, sys\nhttp.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), http.server.SimpleHTTPRequestHandler).serve_forever()\n',
            ".openhands/setup.sh": b"#!/bin/sh\nset -eu\n# Replace with reproducible dependency installation and build commands.\n",
            ".gitignore": b"node_modules/\n.venv/\n__pycache__/\n.env\n.env.*\n!.env.example\n",
        }
        for attempt in range(2):
            branch, head, base_tree, entries = self._tree(repository)
            changes = {path: content for path, content in bootstrap.items() if path not in entries}
            if entries.get(spec_path, {}).get("sha") != git_blob_hash(spec):
                changes[spec_path] = spec
            if not changes:
                return
            tree_entries = []
            for path, content in changes.items():
                blob = self.api("POST", f"repos/{repository}/git/blobs",
                                {"content": base64.b64encode(content).decode(), "encoding": "base64"})
                tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
            tree = self.api("POST", f"repos/{repository}/git/trees", {"base_tree": base_tree, "tree": tree_entries})
            commit = self.api("POST", f"repos/{repository}/git/commits", {
                "message": "symphony: accept Obsidian project note", "tree": tree["sha"], "parents": [head],
            })
            try:
                self.api("PATCH", f"repos/{repository}/git/refs/heads/{quote(branch, safe='')}",
                         {"sha": commit["sha"], "force": False})
                return
            except GitHubError as exc:
                if attempt or not any(code in str(exc) for code in ("HTTP 409", "HTTP 422")):
                    raise

    def ensure_labels(self, repository: str) -> None:
        GhCLIBackend((repository,)).ensure_contract_labels(repository, LABEL_CONTRACT)

    def progress(self, repository: str, progress_path: str) -> dict[str, bytes]:
        _, _, _, entries = self._tree(repository)
        selected = {path: entry for path, entry in entries.items()
                    if path == progress_path or path.startswith("idea/assets/")}
        result = {}
        for path, entry in selected.items():
            if entry.get("type") != "blob" or entry.get("mode") == "120000":
                continue
            if entry.get("size", 0) > 5_000_000:
                continue
            relative = "PROGRESS.md" if path == progress_path else path.removeprefix("idea/")
            result[relative] = GhIdeasBackend._blob(repository, entry["sha"])
        return result


class VaultBridge:
    def __init__(self, config: Config, store: Store, backend: GhVaultBackend | None = None):
        self.base_config = config
        self.store = store
        self.backend = backend or GhVaultBackend()
        self.owner = f"vault-{uuid.uuid4()}"
        self.last_errors: list[str] = []

    def effective_config(self) -> Config:
        config = self.base_config
        issue_repos = set(config.github.allowed_repositories)
        idea_repos = set(config.ideas.repositories)
        repositories = dict(config.repositories)
        for project in self.store.vault_projects():
            repository = project["repository"]
            issue_repos.discard(repository)
            idea_repos.discard(repository)
            # Keep the old mode while a live run drains, so its guarded publication can finish.
            if project["mode"] == "github":
                issue_repos.add(repository)
            elif project["mode"] == "idea":
                idea_repos.add(repository)
            existing = repositories.get(repository, RepositoryConfig())
            repositories[repository] = replace(existing, setup_script=existing.setup_script or ".openhands/setup.sh")
        return replace(config, github=replace(config.github, allowed_repositories=tuple(sorted(issue_repos))),
                       ideas=replace(config.ideas, repositories=tuple(sorted(idea_repos))), repositories=repositories)

    def _repository(self, note: Note) -> str:
        if note.repository:
            repository = note.repository
        else:
            existing = next((p for p in self.store.vault_projects() if p["note_path"] == str(note.path)), None)
            if existing:
                return existing["repository"]
            slug = re.sub(r"[^a-z0-9]+", "-", note.path.stem.lower()).strip("-")[:60] or "project"
            relative = note.path.relative_to(self.base_config.vault.path).as_posix()
            suffix = hashlib.sha256(relative.encode()).hexdigest()[:8]
            repository = f"{self.base_config.vault.owner}/{slug}-{suffix}"
        if repository.split("/", 1)[0].casefold() != self.base_config.vault.owner.casefold():
            raise VaultError("note repository must belong to vault.owner")
        return repository.casefold()

    def _safe_output(self, relative: Path, content: bytes) -> None:
        root = self.base_config.vault.path.resolve()
        target = root / "_symphony" / relative
        if not target.resolve().is_relative_to(root) or any(parent.is_symlink() for parent in (target, *target.parents) if parent != root):
            raise VaultError("generated output escapes the vault")
        if not target.is_file() or target.read_bytes() != content:
            # The orchestrator runs with umask 0077 for credentials and state.
            # Only newly created generated-output directories need shared
            # traversal/write access for the separate Syncthing identity.
            directory = root
            for part in target.relative_to(root).parts[:-1]:
                directory /= part
                if not directory.exists():
                    directory.mkdir(mode=0o2770)
                    directory.chmod(0o2770)
            _atomic_write(target, content)

    def reconcile(self) -> list[str]:
        if not self.store.acquire_operation_lock("vault-intake", self.owner, 3600):
            return ["another vault reconciliation is active"]
        try:
            return self._reconcile()
        except Exception as exc:
            detail = redact(f"vault reconciliation failed: {type(exc).__name__}: {exc}", 2000)
            self.last_errors = [detail]
            for project in self.store.vault_projects():
                self.store.request_vault_mode(project["repository"], "paused", status="error", error=detail)
                self.store.activate_vault_mode(project["repository"])
            return self.last_errors
        finally:
            self.store.release_operation_lock("vault-intake", self.owner)

    def _reconcile(self) -> list[str]:
        config = self.base_config
        root = config.vault.path / config.vault.projects_dir
        self.last_errors = []
        notes: dict[str, list[Note]] = {}
        if not root.is_dir() or not root.resolve().is_relative_to(config.vault.path.resolve()):
            for project in self.store.vault_projects():
                self.store.request_vault_mode(project["repository"], "paused", status="error", error="vault unavailable")
                self.store.activate_vault_mode(project["repository"])
            return ["vault project directory is unavailable; registered projects paused"]
        paths = sorted(root.rglob("*.md"))
        conflicts = {path.with_name(re.sub(r"\.sync-conflict-[^.]+", "", path.name))
                     for path in paths if ".sync-conflict-" in path.name}
        for path in paths:
            relative = path.relative_to(root)
            if any(part.startswith(".") for part in relative.parts) or ".sync-conflict-" in path.name:
                continue
            if path in conflicts:
                self.last_errors.append(f"{relative}: resolve the Syncthing conflict before running")
                continue
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    raise VaultError("note escapes the project directory")
                note = read_note(path)
                if note is None:
                    continue
                repository = self._repository(note)
                notes.setdefault(repository, []).append(note)
            except Exception as exc:
                self.last_errors.append(f"{relative}: {redact(str(exc), 1000)}")
        seen: set[str] = set()
        for repository, candidates in notes.items():
            seen.add(repository)
            try:
                if len(candidates) != 1:
                    raise VaultError("multiple project notes route to the same repository")
                note = candidates[0]
                project = self.store.register_vault_project(
                    repository, str(note.path), managed=note.repository is None,
                    port_start=config.vault.port_start, port_end=config.vault.port_end,
                )
                # Block queued work while a note is changing or a mode transition is pending.
                self.store.request_vault_mode(repository, note.mode)
                snapshot = compile_project(note, repository, config.vault.path) if note.mode == "idea" else None
                if (snapshot is not None and not snapshot.settled(config.vault.quiet_seconds, time.time())) or (
                    snapshot is None and time.time() - note.path.stat().st_mtime < config.vault.quiet_seconds
                ):
                    continue
                if self.store.repository_has_lease(repository) and project["mode"] != note.mode:
                    continue
                if note.mode != "paused":
                    self.backend.ensure_repository(repository, managed=bool(project["managed"]))
                    note = attach_repository(note, repository, config.service.state_dir / "vault-note-originals")
                    if note.mode == "idea":
                        snapshot = compile_project(note, repository, config.vault.path)
                        values = self.store.observe_vault_checklist(repository, [
                            (item.key, item.content_hash, item.checked) for item in snapshot.files
                        ])
                        # Recover completion even if the service stopped immediately after Git publication.
                        completed = self.store.last_completed_idea_run(repository)
                        if completed and completed.state == "published" and completed.spec_content == snapshot.spec:
                            validations = [v for v in self.store.idea_validations(completed.id) if v["attempt"] == completed.attempt]
                            if all(v["exit_code"] == 0 and not v["timed_out"] for v in validations):
                                self.store.complete_vault_checklist(repository, [
                                    (item.key, item.content_hash) for item in snapshot.files
                                ], completed.published_commit or "")
                                values = {item.key: True for item in snapshot.files}
                        snapshot.verify()
                        if config.vault.manage_checkboxes and snapshot.files:
                            updated = snapshot.checkbox_bytes(values)
                            if updated != note.raw:
                                before = snapshot.spec
                                note = replace_note(note, updated, config.service.state_dir / "vault-note-originals")
                                snapshot = compile_project(note, repository, config.vault.path)
                                if snapshot.spec != before:
                                    raise VaultError("checklist file changed while updating status; waiting for the next pass")
                        self.backend.sync_spec(repository, snapshot.spec, config.vault.provider,
                                               project["port"], config.ideas.spec_path)
                        snapshot.verify()
                    elif project["mode"] != "github":
                        self.backend.ensure_labels(repository)
                    if note.path.read_bytes() != note.raw:
                        raise VaultError("note changed during synchronization; waiting for the next pass")
                self.store.activate_vault_mode(repository)
                if note.mode != "paused":
                    for relative, content in self.backend.progress(repository, config.ideas.progress_path).items():
                        self._safe_output(Path(repository.replace("/", "--")) / relative, content)
            except Exception as exc:
                detail = redact(str(exc), 1500)
                self.last_errors.append(f"{repository}: {detail}")
                self.store.request_vault_mode(repository, "paused", status="error", error=detail)
                self.store.activate_vault_mode(repository)
        for project in self.store.vault_projects():
            if project["repository"] not in seen:
                self.store.request_vault_mode(project["repository"], "paused", error="note missing, unmarked, or invalid")
                self.store.activate_vault_mode(project["repository"])
        lines = ["# Symphony projects", "", "Syncthing carries notes and generated results. Existing repositories and issues are retained.", ""]
        for project in self.store.vault_projects():
            repo = project["repository"]
            lines.append(f"- [{repo}](https://github.com/{repo}): {project['mode']} → {project['desired_mode']} ({project['status']}); preview port {project['port']}")
            lines.append(f"  [Progress]({repo.replace('/', '--')}/PROGRESS.md)")
            runs = [run for run in self.store.list_idea_runs() if run.repository == repo]
            if runs:
                run = runs[-1]
                lines.append(f"  Latest run: {run.state}, {run.phase}. {run.question}")
            if project["error"]:
                lines.append(f"  {project['error']}")
        lines.extend(["", *self.last_errors])
        self._safe_output(Path("STATUS.md"), ("\n".join(lines) + "\n").encode())
        return self.last_errors

    def guard_spec(self, repository: str, spec: bytes) -> None:
        project = next((p for p in self.store.vault_projects() if p["repository"] == repository), None)
        if project is None:
            return
        note = read_note(Path(project["note_path"]))
        if note is None or compile_project(note, repository, self.base_config.vault.path).spec != spec:
            raise VaultError("source note or checklist file changed or disappeared before publication")

    def checklist_context(self, repository: str, spec: bytes) -> str:
        project = next((p for p in self.store.vault_projects() if p["repository"] == repository), None)
        if project is None:
            return ""
        note = read_note(Path(project["note_path"]))
        if note is None:
            raise VaultError("project note disappeared before implementation")
        snapshot = compile_project(note, repository, self.base_config.vault.path)
        if snapshot.spec != spec:
            raise VaultError("project or checklist file changed before implementation")
        if not snapshot.files:
            return ""
        statuses = self.store.observe_vault_checklist(repository, [
            (item.key, item.content_hash, item.checked) for item in snapshot.files
        ])
        lines = ["<project-checklist>",
                 "The main Project brief and its row instructions define the current intent. "
                 "Unchecked files are pending work; checked files describe previously completed context. "
                 "The checkboxes in the compiled spec are normalized; use the actual statuses below. "
                 "Implement pending changes against the current code without rebuilding completed features. "
                 "An explicit replacement in the brief or a pending change supersedes the named older behavior. "
                 "Do not infer priority from timestamps, filenames, or checklist order. "
                 "Removing a checklist row stops tracking that file; it does not by itself request deleting a feature. "
                 "If requirements still conflict, return needs-guidance with one focused question before publication. "
                 "Do not edit checkboxes or source notes yourself; the wrapper manages progress."]
        lines.extend(f"- [{'x' if statuses[item.key] else ' '}] {item.key}" for item in snapshot.files)
        lines.append("</project-checklist>")
        return "\n".join(lines)
