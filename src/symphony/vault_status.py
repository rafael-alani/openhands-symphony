"""Deterministic status projection. Never ask a model to rewrite source notes."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import quote

from .models import IdeaRun, IdeaRunState
from .validation import redact
from .vault_markup import VaultError, annotation, annotations, strip_annotations
from .vault_project import ProjectSnapshot


def markdown(value: str) -> str:
    value = " ".join(redact(value, 2000).split())
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", value)


def task_anchor(key: str) -> str:
    return "Task " + hashlib.sha256(key.encode()).hexdigest()[:12]


def relative_link(note: Path, target: Path) -> str:
    return quote(Path(os.path.relpath(target, note.parent)).as_posix(), safe="/")


def project_status(project: dict, run: IdeaRun | None, spec: bytes | None) -> str:
    if project["error"]:
        return "Needs attention"
    if project["mode"] == "paused":
        return "Paused"
    if project["mode"] == "github":
        return "GitHub issue workflow"
    if run is None or (spec is not None and run.spec_content != spec):
        return "Pending intake"
    return {
        "discovered": "Pending", "queued": "Queued", "running": "Running",
        "published": "Published", "question": "Needs guidance", "failed": "Failed",
        "superseded": "Superseded",
    }[run.state]


def render_input_error(project: dict, sources: list[str]) -> bytes:
    """Keep existing status links useful when the source note cannot be written."""
    repository = project["repository"]
    lines = [f"# {repository}", "", "**Symphony status: Needs attention**", "",
             markdown(project["error"]), "", f"[Repository](https://github.com/{repository})"]
    for source in sources:
        lines.extend(["", f"## {task_anchor(source)}", "", f"**{markdown(source)}**",
                      "", "Waiting for valid project input. Any existing result describes an earlier accepted version."])
    return ("\n".join(lines) + "\n").encode()


def render_status(snapshot: ProjectSnapshot, project: dict, run: IdeaRun | None,
                  entries: dict[str, dict], vault: Path, progress_path: str,
                  preview_state: str, *, has_progress: bool) -> tuple[bytes, bytes]:
    """Return annotated source and a per-repository status document, without I/O."""
    note, repository = snapshot.note, project["repository"]
    repo_url = f"https://github.com/{repository}"
    folder = vault / "_symphony" / repository.replace("/", "--")
    status_link = relative_link(note.path, folder / "STATUS.md")
    progress_link = relative_link(note.path, folder / "PROGRESS.md")
    current = project_status(project, run, snapshot.spec)
    links = f"[Repository]({repo_url}) · [Current status]({status_link})"
    if has_progress:
        links += f" · [Latest result]({progress_link})"
    overview = f"\n\n> **Symphony: {current}** · {links}\n"
    lines = [f"# {repository}", "", f"- Repository: [GitHub]({repo_url})",
             f"- Symphony status: **{current}**", f"- Workflow: {project['mode']}",
             f"- Preview status: {markdown(preview_state)}"]
    if project["mode"] == "github":
        lines.append(f"- [Issues]({repo_url}/issues) · [Pull requests]({repo_url}/pulls)")
    if run:
        lines.extend([f"- Latest run: `{run.id}` — {run.state}, {markdown(run.phase)}",
                      f"- Run updated: {run.updated_at}"])
        if run.question:
            lines.append(f"- Run message: {markdown(run.question)}")
    if project["error"]:
        lines.append(f"- Intake message: {markdown(project['error'])}")
    if has_progress:
        lines.append("- [Latest result](PROGRESS.md)")

    task_annotations = {}
    matches = run is not None and run.spec_content == snapshot.spec
    for item in snapshot.files:
        entry = entries.get(item.key, {})
        same_content = entry.get("content_hash") == item.content_hash
        done = same_content and entry.get("done")
        commit = entry.get("completed_commit") if done else None
        if done:
            state = "Completed" if commit else "Checked"
        elif matches and run.state == IdeaRunState.PUBLISHED:
            state = "Partial — validation needs attention"
        else:
            state = current if matches or project["mode"] != "idea" or project["error"] else "Pending"
        anchor = task_anchor(item.key)
        link = f"{status_link}#{quote(anchor, safe='')}"
        suffix = f" — [{state}]({link})"
        result_url = ""
        if commit and re.fullmatch(r"[a-f0-9]{40}", commit):
            result_url = f"{repo_url}/blob/{commit}/{quote(progress_path, safe='/')}"
            suffix += f" · [Result]({result_url})"
        task_annotations[item.key] = suffix
        lines.extend(["", f"## {anchor}", "", f"**{markdown(item.key)}** — {state}"])
        if result_url:
            lines.append(f"[Published result]({result_url}) · [Commit]({repo_url}/commit/{commit})")
        elif matches and has_progress and run.state in {IdeaRunState.PUBLISHED, IdeaRunState.QUESTION}:
            lines.append("[Run result](PROGRESS.md)")
        elif done:
            lines.append("Checked in the source checklist; no Symphony publication is recorded for this version.")

    # Remove only verified generated spans. All remaining characters, including
    # Waypoint, frontmatter, user text, BOM and newline convention, are retained.
    text = note.raw.decode("utf-8-sig")
    spans = annotations(text)
    clean = strip_annotations(text)
    for item in reversed(snapshot.files):
        offset = item.marker_offset - sum(span.end - span.start for span in spans if span.end <= item.marker_offset)
        end = clean.find("\n", offset)
        if end < 0:
            end = len(clean)
        if end and clean[end - 1] == "\r":
            end -= 1
        start = clean.rfind("\n", 0, offset) + 1
        row = clean[start:end]
        if row.lstrip().startswith("|") and row.rstrip().endswith("|"):
            end = start + len(row.rstrip()) - 1
        clean = clean[:end] + annotation("task", task_annotations[item.key]) + clean[end:]
    newline = "\r\n" if "\r\n" in text else "\n"
    clean += annotation("status", overview.replace("\n", newline))
    if strip_annotations(clean) != strip_annotations(text):
        raise VaultError("status rendering would alter source characters; note preserved")
    raw = (b"\xef\xbb\xbf" if note.raw.startswith(b"\xef\xbb\xbf") else b"") + clean.encode()
    return raw, ("\n".join(lines) + "\n").encode()
