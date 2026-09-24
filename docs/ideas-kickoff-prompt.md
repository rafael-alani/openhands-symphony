# Kickoff prompt: ideas tier, first slice

Paste the block below into a fresh agent session in this repository to
start implementation. It covers Phase 0 + the core of Phase 1 from
[ideas-tier.md](ideas-tier.md) — deliberately nothing else.

---

Read `docs/ideas-tier.md` — the plan of record for the ideas tier —
before writing any code. Where this prompt is more specific than that
document, this prompt wins; on anything neither covers, choose the
boring, safe option and note the choice.

Implement only the first slice of the ideas tier: the file contract and
the `ideasync` tool. Hard scope limits:

- Do NOT modify anything under `src/symphony/`, `install.sh`, `systemd/`,
  or existing tests. Tier 1 behavior must be byte-for-byte unchanged; the
  existing test suite must still pass at the end.
- Do NOT implement the Symphony ideas coordinator, webhook intake, preview
  manager, or graduation. Those are later phases.
- Do NOT add Obsidian plugins or touch any real vault; everything must be
  testable against temporary directories.

## Deliverable 1: the idea-repo template

Create `examples/idea-template/` containing:

- `idea/SPEC.md` — realistic example spec with the required frontmatter
  (`symphony: idea`, `repo:`) and two or three `##` feature sections.
- `idea/PROGRESS.md` — a hand-written example of the generated mirror:
  the SPEC prose reproduced exactly, with a terse result block (status
  word, one or two sentences, screenshot link) under each header.
- `.symphony/idea.toml` — example preview contract: `provider`, and
  `[preview]` with `start` as an argument array, `port`, `health_path`,
  `startup_timeout_seconds`. Use `{port}` as the port argument or make the
  app honor `HOST` and `PORT`; the declared port is stable after the first
  healthy release.
- `README.md` — one short page explaining the contract and the
  single-writer rule.

## Deliverable 2: the manual run prompt

Create `docs/ideas-run-prompt.md`: the prompt a human pastes into any
coding agent to perform ONE ideas-mode run by hand (implement changed
wishes, boot the preview, capture screenshots to `idea/assets/`,
regenerate `PROGRESS.md` preserving `SPEC.md` byte-for-byte, commit).
This is how the contract gets validated for a week before Symphony
automates it, so it must stand alone without this conversation.

## Deliverable 3: `ideasync`

A Python package with CLI, in a new top-level `ideasync/` directory with
its own `pyproject.toml` (do not entangle it with the symphony package —
it runs on the user's Mac, not the VM). Match the repo's existing tooling:
uv, ruff, pytest.

Commands: `init`, `add <owner/repo>`, `sync [repo]`, `status`, `doctor`,
all with `--dry-run` where mutation is possible. `open` may be a stub
printing the SSH forward command.

Behavior (the sync section of `docs/ideas-tier.md` plus this list is
normative):

- Dedicated managed clones under the tool's own data directory. Never
  operate on a user's development checkout.
- Per-repo lock so timer and manual runs cannot overlap.
- Fetch + fast-forward only; on any non-fast-forward state: stop, report,
  never reset, never force.
- Inbound copy (repo → vault): `idea/PROGRESS.md` + `idea/assets/`,
  compared by content hash, atomic writes.
- Outbound copy (vault → repo): `SPEC.md` only, routed by frontmatter
  `repo:`, after a configurable quiet period (default 30 s).
- Commit stages exactly `idea/SPEC.md` — never `git add --all` — with a
  predictable message; push; on a lost push race, rebase the single spec
  commit once, and on conflict keep the vault file, stop, and report.
- Sync health written to `_ideasync/STATUS.md` in the vault; failures
  also raise a macOS notification (`osascript`), never fail silently.
- Structured local log; frontmatter validated strictly before any copy.
- A launchd installer (`ideasync install-schedule`) generating a plist
  for a two-minute interval, plus an uninstaller. Keep the scheduler
  behind an interface so systemd user timers can be added later.

Tests: pytest, using temporary bare git repositories as remotes and tmp
dirs as vault/clones. Must cover: both copy directions, hash-based
no-op detection, quiet period, exact staging, push-race rebase, conflict
stop-and-report, lock contention, dry-run causing zero mutation, and
frontmatter rejection.

## Definition of done

- All new tests pass; the pre-existing suite passes untouched; ruff is
  clean across old and new code.
- A scripted end-to-end demo (documented in `ideasync/README.md`) works:
  create a bare repo from the template, `ideasync add`, edit the vault
  spec, `sync` pushes it; simulate an agent commit of `PROGRESS.md` +
  assets in the remote, `sync` pulls it into the vault.
- No force pushes, no silent conflict resolution, no writes outside the
  vault, the managed clones, and the tool's data directory.

Work through the deliverables in order, commit in logical units, and stop
after this slice — do not begin Phase 2 (it has its own prompt in
`docs/ideas-phase2-prompt.md`).
