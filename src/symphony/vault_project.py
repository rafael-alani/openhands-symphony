"""Compile a folder note and its explicit checklist, independently of Obsidian."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote

if TYPE_CHECKING:
    from .vault import Note


class VaultError(ValueError):
    pass


WAYPOINT = re.compile(r"%%\s*Begin Waypoint\s*%%.*?%%\s*End Waypoint\s*%%", re.S | re.I)
WAYPOINT_MARKER = re.compile(r"%%\s*(?:Waypoint|Landmark)\s*%%", re.I)
CHECKBOX = re.compile(r"\[([ xX])\]")
LINK = re.compile(r"!?\[\[([^\]\n]+)\]\]|(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)")
MAX_BYTES = 2_000_000
MAX_FILES = 100


def without_waypoints(text: str) -> str:
    if len(re.findall(r"%%\s*Begin Waypoint\s*%%", text, re.I)) != len(WAYPOINT.findall(text)):
        raise VaultError("Waypoint index is incomplete; waiting for Obsidian to finish writing")
    return WAYPOINT_MARKER.sub("", WAYPOINT.sub("", text)).rstrip()


def visible_checklist(text: str) -> str:
    """Mask generated indexes and code examples without moving character offsets."""
    without_waypoints(text)  # Validate delimiters before treating any links as instructions.
    masked = WAYPOINT.sub(lambda m: "".join("\n" if c == "\n" else " " for c in m[0]), text)
    lines = masked.splitlines(keepends=True)
    fence = ""
    for index, line in enumerate(lines):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence or marker:
            lines[index] = "".join("\n" if c == "\n" else " " for c in line)
            if marker:
                if not fence:
                    fence = marker[1]
                elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                    fence = ""
    return "".join(lines)


def _safe_file(path: Path, project: Path) -> Path:
    absolute = path.absolute()
    if not absolute.is_relative_to(project) or not absolute.resolve().is_relative_to(project):
        raise VaultError("checklist link escapes the project folder")
    relative = absolute.relative_to(project)
    if any(p in {"..", "_symphony"} or p.startswith(".") for p in relative.parts):
        raise VaultError("checklist links cannot use hidden, generated, or parent paths")
    if any(p.is_symlink() for p in (absolute, *absolute.parents) if p.is_relative_to(project)):
        raise VaultError("checklist links cannot follow symlinks")
    if absolute.suffix.lower() != ".md" or not absolute.is_file():
        raise VaultError(f"missing Markdown checklist file: {relative}")
    if absolute.stat().st_size > MAX_BYTES:
        raise VaultError(f"checklist file exceeds 2 MB: {relative}")
    conflicts = list(absolute.parent.glob(f"{absolute.stem}.sync-conflict-*.md"))
    if conflicts or ".sync-conflict-" in absolute.name:
        raise VaultError(f"resolve the Syncthing conflict for {relative}")
    return absolute


def _resolve(target: str, note: Note, vault: Path, *, wiki: bool) -> Path:
    target = target.replace("\\|", "|")
    if wiki:
        target = target.split("|", 1)[0]
    target = unquote(target.strip().removeprefix("<").removesuffix(">"))
    if "#" in target or "^" in target:
        raise VaultError("checklist entries must link a whole Markdown file, without a heading or block anchor")
    if not target or ":" in target or "\\" in target or Path(target).is_absolute() or ".." in Path(target).parts:
        raise VaultError("checklist entry must be a local Markdown file inside the project folder")
    if not Path(target).suffix:
        target += ".md"
    project = note.path.parent.absolute()
    # Obsidian may save a vault-relative path, a project-relative path, or a short name.
    candidates = {p.absolute() for p in (project / target, vault / target) if p.exists()}
    if not candidates and wiki and "/" not in target:
        candidates = set(project.rglob(target))
    if len(candidates) > 1:
        raise VaultError(f"ambiguous checklist link; use a project-relative path: {target}")
    selected = _safe_file(next(iter(candidates), project / target), project)
    if selected == note.path.absolute():
        raise VaultError("project checklist cannot include its own main note")
    return selected


@dataclass(frozen=True)
class ChecklistFile:
    key: str
    path: Path
    raw: bytes
    content_hash: str
    checked: bool
    marker_offset: int  # Character offset of the checkbox value in the decoded main note.
    row: str
    content: str


@dataclass(frozen=True)
class ProjectSnapshot:
    note: Note
    spec: bytes
    files: tuple[ChecklistFile, ...]

    def settled(self, quiet_seconds: int, now: float) -> bool:
        return all(now - path.stat().st_mtime >= quiet_seconds for path in (self.note.path, *(f.path for f in self.files)))

    def verify(self) -> None:
        for path, raw in ((self.note.path, self.note.raw), *((f.path, f.raw) for f in self.files)):
            _safe_file(path, self.note.path.parent.absolute())
            if path.read_bytes() != raw:
                raise VaultError("project note or checklist file changed during synchronization")

    def checkbox_bytes(self, values: dict[str, bool]) -> bytes:
        text = self.note.raw.decode("utf-8-sig")
        for item in reversed(self.files):
            offset = item.marker_offset
            text = text[:offset] + ("x" if values[item.key] else " ") + text[offset + 1:]
        return (b"\xef\xbb\xbf" if self.note.raw.startswith(b"\xef\xbb\xbf") else b"") + text.encode()


def compile_project(note: Note, repository: str, vault: Path) -> ProjectSnapshot:
    visible = visible_checklist(note.body)
    files = []
    offset = note.header_end
    seen = set()
    total = len(note.raw)
    normalized = list(note.body)
    for line in visible.splitlines(keepends=True):
        # Task lists and Markdown table rows are supported. Ordinary prose links are not intake.
        check = CHECKBOX.search(line) if re.match(r"^\s*(?:[-*+]\s+\[|\|)", line) else None
        links = list(LINK.finditer(line)) if check else []
        if check and links:
            if len(links) != 1:
                raise VaultError("use exactly one linked subfile per checklist row")
            link = links[0]
            path = _resolve(link[1] or link[2], note, vault, wiki=link[1] is not None)
            key = path.relative_to(note.path.parent.absolute()).as_posix()
            if key in seen:
                raise VaultError(f"duplicate checklist entry: {key}")
            seen.add(key)
            raw = path.read_bytes()
            total += len(raw)
            if total > MAX_BYTES or len(files) >= MAX_FILES:
                raise VaultError("project checklist exceeds 100 files or 2 MB; split the project")
            child = raw.decode("utf-8-sig")
            header = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", child, re.S)
            if header:
                if re.search(r"(?m)^symphony\s*:", header[1]):
                    raise VaultError(f"checklist file is another project: {key}")
                child = child[header.end():]
            content = without_waypoints(child).replace("\r\n", "\n")
            original_line = note.body[offset - note.header_end:offset - note.header_end + len(line)]
            row = original_line[:check.start(1)] + " " + original_line[check.end(1):]
            row = row.strip().replace("\r\n", "\n")
            digest = hashlib.sha256((row + "\n" + content).encode()).hexdigest()
            files.append(ChecklistFile(key, path, raw, digest, check[1].lower() == "x",
                                       offset + check.start(1), row, content))
            normalized[offset - note.header_end + check.start(1)] = " "
        offset += len(line)
    body = without_waypoints("".join(normalized)).replace("\r\n", "\n")
    if not files:
        if not body.strip():
            raise VaultError(
                "project brief is empty; add a brief or a checklist link such as "
                "- [ ] [[General Idea]] outside Waypoint before running"
            )
        # Preserve the existing single-note contract when no Waypoint is present.
        body = note.body if not WAYPOINT.search(note.body) and not WAYPOINT_MARKER.search(note.body) else body
        return ProjectSnapshot(note, replace(note, body=body).spec(repository), ())
    # One progress section per subfile, regardless of the headings inside that file.
    def demote(text: str) -> str:
        return re.sub(r"(?m)^(#{1,6})[ \t]", r"##\1 ", text)

    parts = [note.spec_header(repository) + f"## Project brief\n\n{demote(body)}"]
    for item in files:
        parts.append(f"## File: {item.key}\n\nChecklist instruction: {item.row}\n\n{demote(item.content)}")
    return ProjectSnapshot(note, ("\n\n".join(parts) + "\n").encode(), tuple(files))
