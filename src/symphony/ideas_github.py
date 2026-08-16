from __future__ import annotations

import base64
from typing import Protocol

from .github import GhCLIBackend, GitHubError
from .ideas_contract import safe_path
from .intake import validate_repository_name
from .models import IdeaSnapshot

RUNTIME_PATH = ".symphony/idea.toml"


class IdeasGitHubBackend(Protocol):
    def get_snapshot(self, repository: str, spec_path: str, progress_path: str) -> IdeaSnapshot: ...


class GhIdeasBackend:
    def __init__(self, allowlist: tuple[str, ...], *, private_only: bool = True):
        self.allowlist = set(allowlist)
        self.private_only = private_only

    def _allowed(self, repository: str) -> None:
        validate_repository_name(repository)
        if repository not in self.allowlist:
            raise GitHubError(f"repository is not ideas-allowlisted: {repository}")

    @staticmethod
    def _blob(repository: str, sha: str) -> bytes:
        payload = GhCLIBackend._run(["api", f"repos/{repository}/git/blobs/{sha}"], json_output=True)
        try:
            return base64.b64decode(str(payload["content"]).replace("\n", ""), validate=True)
        except (KeyError, ValueError) as exc:
            raise GitHubError(f"GitHub returned an invalid blob for {repository}@{sha}") from exc

    def get_snapshot(self, repository: str, spec_path: str, progress_path: str) -> IdeaSnapshot:
        self._allowed(repository)
        spec_path = safe_path(spec_path)
        progress_path = safe_path(progress_path)
        repo = GhCLIBackend._run(["api", f"repos/{repository}"], json_output=True)
        private = bool(repo.get("private", False))
        if self.private_only and not private:
            raise GitHubError(f"public ideas repositories are disabled: {repository}")
        default_branch = str(repo.get("default_branch") or "main")
        commit = GhCLIBackend._run(["api", f"repos/{repository}/commits/{default_branch}"], json_output=True)
        base_commit = str(commit.get("sha") or "")
        if not base_commit:
            raise GitHubError(f"unable to resolve {repository}'s default branch")
        tree = GhCLIBackend._run(
            ["api", f"repos/{repository}/git/trees/{base_commit}?recursive=1"], json_output=True
        )
        if bool(tree.get("truncated")):
            raise GitHubError(f"repository tree is too large to locate the idea contract safely: {repository}")
        entries = {
            str(row.get("path")): str(row.get("sha"))
            for row in tree.get("tree", [])
            if isinstance(row, dict) and row.get("type") == "blob"
        }
        spec_hash = entries.get(spec_path)
        runtime_hash = entries.get(RUNTIME_PATH)
        if not spec_hash:
            raise GitHubError(f"idea spec is missing from {repository}: {spec_path}")
        if not runtime_hash:
            raise GitHubError(f"idea runtime contract is missing from {repository}: {RUNTIME_PATH}")
        progress_hash = entries.get(progress_path)
        return IdeaSnapshot(
            repository=repository,
            spec_hash=spec_hash,
            spec_content=self._blob(repository, spec_hash),
            runtime_content=self._blob(repository, runtime_hash),
            previous_progress=self._blob(repository, progress_hash) if progress_hash else b"",
            base_commit=base_commit,
            default_branch=default_branch,
            private=private,
        )
