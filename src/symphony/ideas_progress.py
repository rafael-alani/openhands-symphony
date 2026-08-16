from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .ideas_contract import HEADER_PATTERN
from .intake import short_slug

STATUSES = {"done", "partial", "not started", "question"}
RESULT_PATTERN = re.compile(
    rb"\r?\n> \*\*(done|partial|not started|question)\*\*\r?\n"
    rb">\r?\n"
    rb"> ([^\r\n]*)\r?\n"
    rb"(?:>\r?\n> !\[([^\]]*)\]\(([^)]+)\)\r?\n)?"
)


@dataclass(frozen=True)
class IdeaSection:
    index: int
    title: str
    slug: str
    header_end: int
    body: bytes


@dataclass(frozen=True)
class SectionResult:
    status: str
    summary: str
    screenshot: str = ""
    alt: str = ""


def sections(spec: bytes) -> tuple[IdeaSection, ...]:
    matches = list(HEADER_PATTERN.finditer(spec))
    seen: dict[str, int] = {}
    result: list[IdeaSection] = []
    for index, match in enumerate(matches):
        title = match.group(1).decode("utf-8").strip()
        base = short_slug(title)
        seen[base] = seen.get(base, 0) + 1
        slug = base if seen[base] == 1 else f"{base}-{seen[base]}"
        end = matches[index + 1].start() if index + 1 < len(matches) else len(spec)
        result.append(IdeaSection(index, title, slug, match.end(), spec[match.end() : end]))
    return tuple(result)


def affected_sections(previous_spec: bytes | None, current_spec: bytes) -> tuple[IdeaSection, ...]:
    current = sections(current_spec)
    if previous_spec is None:
        return current
    previous = sections(previous_spec)
    previous_bodies = {(section.title, section.slug): section.body for section in previous}
    return tuple(
        section
        for section in current
        if previous_bodies.get((section.title, section.slug)) != section.body
    )


def previous_results(progress: bytes) -> dict[str, SectionResult]:
    values: dict[str, SectionResult] = {}
    for section in sections(progress):
        match = RESULT_PATTERN.match(progress, section.header_end)
        if not match:
            continue
        values[section.slug] = SectionResult(
            status=match.group(1).decode(),
            summary=match.group(2).decode("utf-8"),
            alt=(match.group(3) or b"").decode("utf-8"),
            screenshot=(match.group(4) or b"").decode("utf-8"),
        )
    return values


def render_progress(spec: bytes, results: dict[str, SectionResult]) -> bytes:
    additions: list[tuple[int, bytes]] = []
    for section in sections(spec):
        result = results[section.slug]
        if result.status not in STATUSES:
            raise ValueError(f"invalid idea result status: {result.status}")
        newline = b"\r\n" if b"\r\n" in spec[max(0, section.header_end - 4) : section.header_end] else b"\n"
        summary = " ".join(result.summary.split())
        block = newline + f"> **{result.status}**".encode() + newline + b">" + newline
        block += b"> " + summary.encode("utf-8") + newline
        if result.screenshot:
            alt = result.alt or section.title
            block += b">" + newline + f"> ![{alt}]({result.screenshot})".encode() + newline
        additions.append((section.header_end, block))
    output = bytearray(spec)
    for offset, block in reversed(additions):
        output[offset:offset] = block
    reconstructed = bytearray(output)
    shifted = [
        (offset + sum(len(previous) for previous_offset, previous in additions if previous_offset < offset), block)
        for offset, block in additions
    ]
    for offset, block in reversed(shifted):
        del reconstructed[offset : offset + len(block)]
    if bytes(reconstructed) != spec:
        raise ValueError("generated progress does not reconstruct the accepted spec")
    return bytes(output)


def screenshot_path(section: IdeaSection) -> str:
    return f"idea/assets/{section.slug}.png"


def content_fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
