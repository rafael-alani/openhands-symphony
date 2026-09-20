from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ideasync.errors import ContractError

_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_REPOSITORY = re.compile(
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?)"
)


@dataclass(frozen=True)
class Frontmatter:
    repository: str
    values: dict[str, str]


@dataclass(frozen=True)
class PreviewContract:
    provider: str
    start: tuple[str, ...]
    port: int
    health_path: str
    startup_timeout_seconds: int


def validate_repository(value: str) -> str:
    if _REPOSITORY.fullmatch(value) is None:
        raise ContractError(f"repository must be exactly owner/name, got {value!r}")
    return value


def _parse_scalar(raw: str, *, path: Path, key: str) -> str:
    value = raw.strip()
    comment = value.find(" #")
    if comment >= 0:
        value = value[:comment].rstrip()
    if not value:
        raise ContractError(f"{path}: frontmatter value for {key!r} is empty")
    if value.startswith(("&", "*")) or "${" in value:
        raise ContractError(f"{path}: aliases and substitutions are not allowed in frontmatter")
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ContractError(f"{path}: invalid quoted value for {key!r}") from exc
        if not isinstance(decoded, str):
            raise ContractError(f"{path}: {key!r} must be a string")
        return decoded
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ContractError(f"{path}: invalid quoted value for {key!r}")
        return value[1:-1].replace("''", "'")
    if value.endswith(("'", '"')) or any(token in value for token in ("[", "]", "{", "}")):
        raise ContractError(f"{path}: only simple string values are allowed in frontmatter")
    return value


def parse_frontmatter(data: bytes, path: Path) -> Frontmatter:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractError(f"{path}: file is not valid UTF-8") from exc
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ContractError(f"{path}: frontmatter must be the first block and start with ---")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise ContractError(f"{path}: frontmatter is missing its closing ---") from exc
    values: dict[str, str] = {}
    for number, line in enumerate(lines[1:closing], start=2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in line:
            raise ContractError(f"{path}:{number}: expected a simple key: value entry")
        raw_key, raw_value = line.split(":", 1)
        key = raw_key.strip()
        if _KEY.fullmatch(key) is None or raw_key != key:
            raise ContractError(f"{path}:{number}: invalid frontmatter key {raw_key!r}")
        if key in values:
            raise ContractError(f"{path}:{number}: duplicate frontmatter key {key!r}")
        values[key] = _parse_scalar(raw_value, path=path, key=key)
    if values.get("symphony") != "idea":
        raise ContractError(f"{path}: frontmatter must contain exactly `symphony: idea`")
    if "repo" not in values:
        raise ContractError(f"{path}: frontmatter is missing `repo: owner/name`")
    repository = validate_repository(values["repo"])
    return Frontmatter(repository=repository, values=values)


def read_frontmatter(path: Path) -> Frontmatter:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{path}: expected a regular file, not a symlink")
    return parse_frontmatter(path.read_bytes(), path)


def read_preview_contract(path: Path) -> PreviewContract:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{path}: preview contract is missing or is not a regular file")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ContractError(f"{path}: invalid TOML: {exc}") from exc
    provider = raw.get("provider")
    preview = raw.get("preview")
    if not isinstance(provider, str) or re.fullmatch(r"[A-Za-z0-9_.-]+", provider) is None:
        raise ContractError(f"{path}: provider must be a safe non-empty name")
    if not isinstance(preview, dict):
        raise ContractError(f"{path}: [preview] table is required")
    start = preview.get("start")
    port = preview.get("port")
    health_path = preview.get("health_path")
    timeout = preview.get("startup_timeout_seconds")
    if not isinstance(start, list) or not start or any(not isinstance(arg, str) or not arg for arg in start):
        raise ContractError(f"{path}: preview.start must be a non-empty argument array")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ContractError(f"{path}: preview.port must be an integer from 1 through 65535")
    if (
        not isinstance(health_path, str)
        or not health_path.startswith("/")
        or health_path.startswith("//")
        or "?" in health_path
        or "#" in health_path
    ):
        raise ContractError(f"{path}: preview.health_path must be an absolute path without a query or fragment")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ContractError(f"{path}: preview.startup_timeout_seconds must be a positive integer")
    return PreviewContract(
        provider=provider,
        start=tuple(start),
        port=port,
        health_path=health_path,
        startup_timeout_seconds=timeout,
    )
