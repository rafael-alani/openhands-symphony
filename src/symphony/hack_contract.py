from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


class HackContractError(ValueError):
    pass


@dataclass(frozen=True)
class BoardTask:
    key: str
    lane: str
    prompt: str
    footprint: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    checked: bool = False
    priority: int = 0

    @property
    def title(self) -> str:
        return self.prompt


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_CHECKBOX = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.+?)\s*$")
_DEPENDENCIES = re.compile(r"\s*<!--\s*depends(?:-on)?:\s*([^>]+?)\s*-->\s*$")


def _safe_relative(value: str, *, glob: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise HackContractError("footprints and paths must be non-empty repository-relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts or value == ".":
        raise HackContractError("path escapes the repository or accesses git metadata")
    if glob and ("[" in value or "]" in value):
        raise HackContractError("lane globs support *, ** and ?; character classes are not supported")
    return path.as_posix()


def _tokens(pattern: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\*\*|.", pattern))


def _glob_regex(pattern: str) -> re.Pattern[str]:
    tokens = _tokens(pattern)
    pieces = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "**" and index + 1 < len(tokens) and tokens[index + 1] == "/":
            pieces.append("(?:.*/)?")
            index += 2
            continue
        pieces.append({"**": ".*", "*": "[^/]*", "?": "[^/]"}.get(token, re.escape(token)))
        index += 1
    return re.compile("".join(pieces) + r"\Z")


def _globs_overlap(first: str, second: str) -> bool:
    """Intersect two small glob automata, conservatively including **/ zero directories."""
    left, right = _tokens(first), _tokens(second)
    pending = [(0, 0)]
    visited = set()
    while pending:
        a, b = pending.pop()
        if (a, b) in visited:
            continue
        visited.add((a, b))
        if a == len(left) and b == len(right):
            return True
        for tokens, current, other, swap in ((left, a, b, False), (right, b, a, True)):
            if current < len(tokens) and tokens[current] in {"*", "**"}:
                advances = [current + 1]
                if tokens[current] == "**" and current + 1 < len(tokens) and tokens[current + 1] == "/":
                    advances.append(current + 2)
                pending.extend((other, step) if swap else (step, other) for step in advances)
        if a == len(left) or b == len(right):
            continue
        x, y = left[a], right[b]
        wild = {"*", "**", "?"}
        compatible = x == y or (x in wild and y in wild)
        compatible |= x in wild and (y != "/" or x == "**")
        compatible |= y in wild and (x != "/" or y == "**")
        if compatible:
            pending.append((a if x in {"*", "**"} else a + 1, b if y in {"*", "**"} else b + 1))
    return False


def parse_lanes(content: bytes | str | Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Read [lanes.NAME] paths=[...] or a mapping of lane names to path lists."""
    try:
        raw = content if isinstance(content, Mapping) else tomllib.loads(
            content.decode("utf-8") if isinstance(content, bytes) else content
        )
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise HackContractError("hack lane configuration must be valid UTF-8 TOML") from exc
    raw = raw.get("lanes", raw)
    if not isinstance(raw, Mapping) or not raw:
        raise HackContractError("hack configuration must declare at least one lane")
    result: dict[str, tuple[str, ...]] = {}
    for name, definition in raw.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in {"scaffold", "polish", "integrator", "dispatcher"}:
            raise HackContractError(f"invalid or reserved lane name: {name}")
        paths = definition.get("paths", definition.get("footprint")) if isinstance(definition, Mapping) else definition
        if not isinstance(paths, (list, tuple)) or not paths or not all(isinstance(path, str) for path in paths):
            raise HackContractError(f"lane {name} must declare a non-empty paths array")
        footprint = tuple(_safe_relative(path, glob=True) for path in paths)
        for previous, previous_paths in result.items():
            if any(_globs_overlap(a, b) for a in footprint for b in previous_paths):
                raise HackContractError(f"lanes {previous} and {name} have overlapping footprints")
        result[name] = footprint
    return result


def parse_board(content: bytes | str, lanes: Mapping[str, Any]) -> list[BoardTask]:
    """Parse ordered checkbox tasks under ## lane headers without rewriting the board."""
    try:
        text = content.decode("utf-8") if isinstance(content, bytes) else content
    except UnicodeDecodeError as exc:
        raise HackContractError("hack/BOARD.md must be valid UTF-8") from exc
    footprints = parse_lanes(lanes)
    tasks: list[BoardTask] = []
    known: set[str] = set()
    lane = None
    for line in text.splitlines():
        if line.startswith("## "):
            lane = line[3:].strip()
            continue
        match = _CHECKBOX.match(line)
        if not match:
            continue
        if lane not in footprints:
            raise HackContractError(f"board task must belong to a configured lane: {lane}")
        checked, prompt = match.groups()
        dependencies = _DEPENDENCIES.search(prompt)
        depends_on = ()
        if dependencies:
            depends_on = tuple(value.strip() for value in dependencies.group(1).split(",") if value.strip())
            if not all(_NAME.fullmatch(value) for value in depends_on):
                raise HackContractError("dependency IDs must be safe task identifiers")
            prompt = prompt[:dependencies.start()].strip()
        identifier = re.match(r"^\[([^\]]+)\]\s+(.+)$", prompt)
        if identifier:
            key, prompt = identifier.groups()
            if not _NAME.fullmatch(key):
                raise HackContractError(f"invalid board task ID: {key}")
        else:
            key = hashlib.sha256(f"{lane}\0{prompt}".encode()).hexdigest()[:20]
        if not prompt or key in known:
            raise HackContractError(f"empty or duplicate board task: {key}")
        if key in depends_on:
            raise HackContractError(f"task {key} depends on itself")
        known.add(key)
        tasks.append(BoardTask(key, lane, prompt, footprints[lane], depends_on, checked.lower() == "x", len(tasks)))
    graph = {task.key: task.depends_on for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise HackContractError("board task dependencies contain a cycle")
        if key in visited:
            return
        visiting.add(key)
        for dependency in graph.get(key, ()):
            if dependency not in graph:
                raise HackContractError(f"unknown board dependency: {dependency}")
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in graph:
        visit(key)
    return tasks


def allowed_paths(paths: Iterable[str], footprint: Iterable[str]) -> bool:
    try:
        patterns = [_glob_regex(_safe_relative(pattern, glob=True)) for pattern in footprint]
        return all(any(pattern.fullmatch(_safe_relative(path)) for pattern in patterns) for path in paths)
    except HackContractError:
        return False


def validate_footprint(paths: Iterable[str], footprint: Iterable[str]) -> None:
    patterns = tuple(footprint)
    rejected = [path for path in paths if not allowed_paths([path], patterns)]
    if rejected:
        raise HackContractError("lane commit touches paths outside its footprint: " + ", ".join(rejected))
