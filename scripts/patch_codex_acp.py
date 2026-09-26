#!/usr/bin/env python3
"""Compatibility for the pinned ACP 1.1.4 / Codex 0.144.4 fast-tier alias."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

PATCHES = (
    ('sessionMetadata.currentServiceTier === "fast"',
     '["fast", "priority"].includes(sessionMetadata.currentServiceTier)', 2),
    ('return fastModeEnabled && currentModelSupportsFast ? "fast" : null;',
     'if (fastModeEnabled && !currentModelSupportsFast) {\n'
     '    throw RequestError.invalidRequest("Fast mode is unavailable for the selected Codex model");\n'
     '  }\n  return fastModeEnabled && currentModelSupportsFast ? "fast" : null;', 1),
)


def patch(package: Path) -> None:
    if json.loads((package / "package.json").read_text())["version"] != "1.1.4":
        raise ValueError("Review fast-tier compatibility before changing the pinned codex-acp version")
    target = package / "dist/index.js"
    original = target.read_text()
    updated = original
    for old, new, count in PATCHES:
        if updated.count(new) == count:
            continue
        if updated.count(old) != count or new in updated:
            raise ValueError("Unexpected codex-acp source; refusing a partial compatibility patch")
        updated = updated.replace(old, new)
    if updated != original:
        target.write_text(updated)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    patch(parser.parse_args().package)
