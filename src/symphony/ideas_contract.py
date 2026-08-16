from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .intake import validate_repository_name

HEADER_PATTERN = re.compile(rb"(?m)^##[ \t]+([^\r\n]+)[ \t]*(?:\r?\n|$)")


class IdeaContractError(ValueError):
    pass


@dataclass(frozen=True)
class IdeaRuntime:
    provider: str
    start: tuple[str, ...]
    port: int
    health_path: str
    startup_timeout_seconds: int


def validate_spec(spec: bytes, repository: str) -> None:
    try:
        text = spec.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IdeaContractError("idea spec must be valid UTF-8") from exc
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise IdeaContractError("idea spec must begin with exact YAML frontmatter delimiters")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise IdeaContractError("idea spec frontmatter is not closed") from exc
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line or line.lstrip().startswith("#") or ":" not in line:
            raise IdeaContractError("idea spec frontmatter must contain only symphony and repo scalars")
        key, value = (part.strip() for part in line.split(":", 1))
        if key not in {"symphony", "repo"} or key in values or not value:
            raise IdeaContractError("idea spec frontmatter must contain exactly one symphony and repo key")
        values[key] = value
    if values != {"symphony": "idea", "repo": repository}:
        raise IdeaContractError("idea spec frontmatter must set symphony: idea and match the repository")
    try:
        validate_repository_name(values["repo"])
    except ValueError as exc:
        raise IdeaContractError(str(exc)) from exc
    if not HEADER_PATTERN.search(spec):
        raise IdeaContractError("idea spec must contain at least one ## section")


def parse_runtime(content: bytes) -> IdeaRuntime:
    try:
        raw = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise IdeaContractError(".symphony/idea.toml is not valid UTF-8 TOML") from exc
    provider = raw.get("provider")
    preview = raw.get("preview")
    if not isinstance(provider, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", provider):
        raise IdeaContractError("idea runtime provider must be a safe non-empty name")
    if not isinstance(preview, dict):
        raise IdeaContractError("idea runtime must contain a [preview] table")
    start = preview.get("start")
    port = preview.get("port")
    health_path = preview.get("health_path")
    timeout = preview.get("startup_timeout_seconds")
    if not isinstance(start, list) or not start or not all(isinstance(value, str) and value for value in start):
        raise IdeaContractError("preview.start must be a non-empty argument array")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise IdeaContractError("preview.port must be an integer between 1 and 65535")
    if (
        not isinstance(health_path, str)
        or not health_path.startswith("/")
        or health_path.startswith("//")
        or "?" in health_path
        or "#" in health_path
    ):
        raise IdeaContractError("preview.health_path must be an absolute path without a query or fragment")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise IdeaContractError("preview.startup_timeout_seconds must be a positive integer")
    return IdeaRuntime(provider, tuple(start), port, health_path, timeout)


def git_blob_hash(content: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def safe_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise IdeaContractError("idea path escapes the repository")
    return path.as_posix()
