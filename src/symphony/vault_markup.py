"""Checksummed, wrapper-owned annotations; stripping them recovers source bytes."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


class VaultError(ValueError):
    pass


BLOCK = re.compile(
    r"<!-- symphony-(?P<kind>task|status):(?P<digest>[a-f0-9]{64}) -->"
    r"(?P<content>.*?)<!-- /symphony-(?P=kind) -->", re.S,
)
MARKER = re.compile(r"<!--\s*/?symphony-(?:task|status)\b")


@dataclass(frozen=True)
class Annotation:
    start: int
    end: int
    kind: str


def annotations(text: str) -> tuple[Annotation, ...]:
    matches = list(BLOCK.finditer(text))
    if len(MARKER.findall(text)) != 2 * len(matches):
        raise VaultError("Symphony status markers are incomplete; preserve the note and resolve the edit before syncing")
    result = []
    for match in matches:
        content = match["content"]
        if hashlib.sha256(content.encode()).hexdigest() != match["digest"]:
            raise VaultError("Symphony status annotation was edited; preserve your edits before removing its marked block")
        if match["kind"] == "task" and ("\n" in content or "\r" in content):
            raise VaultError("Symphony task status must stay on its checklist row")
        result.append(Annotation(match.start(), match.end(), match["kind"]))
    if sum(item.kind == "status" for item in result) > 1:
        raise VaultError("multiple Symphony status blocks; resolve the duplicate without discarding note content")
    return tuple(result)


def strip_annotations(text: str, *, mask: bool = False) -> str:
    for item in reversed(annotations(text)):
        replacement = "".join("\n" if c == "\n" else " " for c in text[item.start:item.end]) if mask else ""
        text = text[:item.start] + replacement + text[item.end:]
    return text


def source_slice(text: str, start: int, end: int) -> str:
    """Read a source line even when a generated block crosses its boundary."""
    value = text[start:end]
    for item in reversed(annotations(text)):
        left, right = max(start, item.start), min(end, item.end)
        if left < right:
            value = value[:left - start] + value[right - start:]
    return value


def annotation(kind: str, content: str) -> str:
    if kind not in {"task", "status"} or MARKER.search(content):
        raise VaultError("invalid generated status annotation")
    digest = hashlib.sha256(content.encode()).hexdigest()
    return f"<!-- symphony-{kind}:{digest} -->{content}<!-- /symphony-{kind} -->"
