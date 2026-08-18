# ideasync

`ideasync` is the Mac-side bridge for ideas-tier repositories. It never operates on a development checkout. Each configured `owner/repo` gets a dedicated clone below the tool's data directory; Git is the only transport between that clone and the remote.

The file directions are deliberately unequal:

- `SPEC.md`: vault → managed clone → remote, after the quiet period.
- `PROGRESS.md` and `assets/`: remote → managed clone → vault.

`ideasync add` performs the one bootstrap exception needed for a new idea: if the vault has no `SPEC.md` routed to that repository, it seeds one from the remote. It never replaces an existing vault spec. Every later sync treats the vault copy as authoritative.

## Install and configure

Python 3.12 or newer, `git`, and [uv](https://docs.astral.sh/uv/) are required.

```bash
cd ideasync
uv tool install .
ideasync init --vault "$HOME/IdeasVault"
ideasync add example-owner/pantry-pilot
ideasync doctor
ideasync sync --dry-run
ideasync sync
ideasync status
```

The default tool data directory is `~/Library/Application Support/ideasync`. Override it with the global `--data-dir PATH` option or `IDEASYNC_DATA_DIR`. `init --quiet-period-seconds N` changes the default 30-second debounce. `add --remote URL owner/repo` supports a non-GitHub or local test remote while routing still uses the exact frontmatter `repo: owner/repo`.

`sync owner/repo` limits a pass to one configured repository; bare `sync` processes all of them. A per-repository non-blocking lock makes overlapping timer/manual passes harmless. Fetches only fast-forward the managed clone. A spec commit stages exactly `idea/SPEC.md`, uses the fixed message `ideasync: update idea spec`, and never force-pushes. A lost push race gets one rebase and one retry. A conflict stops with the vault bytes preserved in the managed clone.

Every non-dry sync writes `_ideasync/STATUS.md` in the vault and appends JSON Lines to `<data-dir>/logs/ideasync.jsonl`. Failures also request a macOS notification through `osascript`. Dry runs create no lock, fetch nothing, write no log/status, and change no file.

## Schedule and live preview

```bash
ideasync install-schedule --dry-run
ideasync install-schedule
ideasync uninstall-schedule
ideasync open example-owner/pantry-pilot --host your-ideas-vm
```

The launchd implementation uses `StartInterval = 120`. Its plist stays below `<data-dir>/launchd/` and is bootstrapped into the current GUI domain, keeping all ideasync-owned files inside the data directory. Re-run `install-schedule` after a new login because no file is placed in `~/Library/LaunchAgents`. The scheduler is behind a small interface so a systemd user-timer implementation can be added later.

`open` reads the repository's current preview port, establishes an `ExitOnForwardFailure` SSH tunnel bound only to local loopback, opens the browser, and keeps the tunnel attached to the terminal until Ctrl-C. Use `--local-port` when the declared port is already occupied, `--no-browser` when only the tunnel is wanted, or `--dry-run` to inspect the exact argv without connecting.

## Development

```bash
UV_CACHE_DIR=/tmp/ideasync-uv uv sync --extra test
UV_CACHE_DIR=/tmp/ideasync-uv uv run --extra test pytest
UV_CACHE_DIR=/tmp/ideasync-uv uv run --extra test ruff check .
```

## Scripted end-to-end demo

Run this from the `ideasync/` directory. It creates only a temporary vault, tool data directory, ordinary agent clone, and bare remote. The example repository is copied from `examples/idea-template`; no real vault or development checkout is touched.

```bash
set -eu
export DEMO_ROOT="$(mktemp -d)"
export UV_CACHE_DIR="$DEMO_ROOT/uv-cache"
cp -R ../examples/idea-template "$DEMO_ROOT/seed"

git -C "$DEMO_ROOT/seed" init -b main
git -C "$DEMO_ROOT/seed" config user.name "Demo User"
git -C "$DEMO_ROOT/seed" config user.email "demo@example.invalid"
git -C "$DEMO_ROOT/seed" add .symphony/idea.toml README.md idea/PROGRESS.md idea/SPEC.md
git -C "$DEMO_ROOT/seed" commit -m "seed idea template"
git init --bare --initial-branch=main "$DEMO_ROOT/remote.git"
git -C "$DEMO_ROOT/seed" remote add origin "$DEMO_ROOT/remote.git"
git -C "$DEMO_ROOT/seed" push -u origin main

uv run ideasync --data-dir "$DEMO_ROOT/data" init \
  --vault "$DEMO_ROOT/vault" --quiet-period-seconds 0
uv run ideasync --data-dir "$DEMO_ROOT/data" add \
  example-owner/pantry-pilot --remote "$DEMO_ROOT/remote.git"

printf '\n## Pick a cooking time\n\nLet me choose between a 15, 30, or 60 minute dinner.\n' \
  >> "$DEMO_ROOT/vault/pantry-pilot/SPEC.md"
uv run ideasync --data-dir "$DEMO_ROOT/data" sync example-owner/pantry-pilot
git --git-dir="$DEMO_ROOT/remote.git" show main:idea/SPEC.md | grep "Pick a cooking time"

git clone "$DEMO_ROOT/remote.git" "$DEMO_ROOT/agent"
git -C "$DEMO_ROOT/agent" config user.name "Demo Agent"
git -C "$DEMO_ROOT/agent" config user.email "agent@example.invalid"

python3 - <<'PY'
import base64
import os
import re
from pathlib import Path

root = Path(os.environ["DEMO_ROOT"])
idea = root / "agent" / "idea"
spec = (idea / "SPEC.md").read_bytes()
lines = spec.splitlines(keepends=True)
output = []
screenshots = []
i = 0
while i < len(lines):
    line = lines[i]
    output.append(line)
    if line.startswith(b"## "):
        heading = line[3:].decode().strip()
        slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
        if i + 1 < len(lines) and lines[i + 1].strip() == b"":
            i += 1
            output.append(lines[i])
        output.append(
            f"> **done**\n>\n> The demo agent verified {heading.lower()}.\n>\n"
            f"> ![{heading}](assets/{slug}.png)\n\n".encode()
        )
        screenshots.append(slug)
    i += 1
(idea / "PROGRESS.md").write_bytes(b"".join(output))
(idea / "assets").mkdir(exist_ok=True)
png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
for slug in screenshots:
    (idea / "assets" / f"{slug}.png").write_bytes(png)
PY

git -C "$DEMO_ROOT/agent" add idea/PROGRESS.md \
  idea/assets/capture-what-i-have.png \
  idea/assets/suggest-dinner.png \
  idea/assets/make-the-result-easy-to-follow.png \
  idea/assets/pick-a-cooking-time.png
git -C "$DEMO_ROOT/agent" commit -m "agent: update progress and screenshots"
git -C "$DEMO_ROOT/agent" push origin main

uv run ideasync --data-dir "$DEMO_ROOT/data" sync example-owner/pantry-pilot
cmp "$DEMO_ROOT/agent/idea/PROGRESS.md" "$DEMO_ROOT/vault/pantry-pilot/PROGRESS.md"
cmp "$DEMO_ROOT/agent/idea/assets/pick-a-cooking-time.png" \
  "$DEMO_ROOT/vault/pantry-pilot/assets/pick-a-cooking-time.png"
uv run ideasync --data-dir "$DEMO_ROOT/data" status
echo "demo passed; temporary files remain at $DEMO_ROOT"
```
