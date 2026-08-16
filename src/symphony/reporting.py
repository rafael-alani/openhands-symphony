from __future__ import annotations

import os
from pathlib import Path

from .validation import redact


class RunReportArtifacts:
    """Source-neutral durable report file writer."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def write(self, run_id: str, markdown: str, json_payload: str) -> tuple[Path, Path]:
        directory = self.root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        json_path = directory / "run.json"
        markdown_path = directory / "run.md"
        json_path.write_text(redact(json_payload, 5_000_000) + "\n")
        markdown_path.write_text(markdown)
        os.chmod(json_path, 0o600)
        os.chmod(markdown_path, 0o600)
        return markdown_path, json_path
