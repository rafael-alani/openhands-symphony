# Ideas tier (Tier 2): spec-file-driven, low-friction mode

Status: the original Phases 0–4 are implemented locally. The current note
intake and reversible mode switching supersede the Mac-side sync and archival
transition described below; see [Obsidian workflow](obsidian.md). This document
retains the original implementation history, not the current setup instructions.
This records the original four-phase plan for the ideas tier; the hackathon
overlay lives in [hack-tier.md](hack-tier.md). Implementation prompts:
[ideas-kickoff-prompt.md](ideas-kickoff-prompt.md) for the first slice
(contract + `ideasync`), then
[ideas-phase2-prompt.md](ideas-phase2-prompt.md) for the Symphony
automation.

## The two tiers

| | Tier 1 — GitHub mode (exists today) | Tier 2 — Ideas mode (this plan) |
|---|---|---|
| Specification surface | GitHub issue + labels | One Markdown file in the repo |
| Result surface | Draft PR + canonical status comment | Agent-written companion Markdown file with screenshots |
| Human gate | Always: draft PR, human merges | None: agent commits directly to `main` |
| Intended repos | Real projects under active development | Throwaway / idea-stage private repos |
| Where the user works | GitHub UI | One Obsidian vault on their own PC |

An idea that survives graduates to Tier 1 by moving its remaining wishes into
issues and removing the ideas-tier frontmatter flag. The tiers are mutually
exclusive per repository: a repo is either issue-driven or spec-file-driven,
never both, so the two intakes cannot race each other.

## File contract

Everything lives under `idea/` at the repository root.

```
idea/
  SPEC.md            # user-owned. The wishlist. Agent never writes this file.
  PROGRESS.md        # agent-owned. Full mirror of SPEC.md plus terse result
                     # blocks and screenshots. User never writes this file.
  assets/            # agent-owned. Screenshots referenced from PROGRESS.md.
.symphony/
  idea.toml          # runtime contract: provider choice, and the preview
                     # start command (argument array, never a shell string),
                     # port, and health path.
```

`SPEC.md` frontmatter (also what the vault sync script routes on):

```yaml
---
symphony: idea
repo: rafael-alani/some-idea        # which repo this file belongs to
---
```

Rules that make everything downstream simple:

- **Single-writer per file.** The user is the only writer of `SPEC.md`; the
  agent is the only writer of `PROGRESS.md` and `assets/`. Because ownership
  is split per file, every file only ever flows in one direction, and the
  sync problem stops being two-way sync at all (see below).
- `SPEC.md` is free-form prose under `##` headers. No required structure
  beyond the frontmatter; headers are the unit the agent reports against.
- `PROGRESS.md` is a full mirror of `SPEC.md` — the user's prose reproduced
  byte-for-byte — with a terse result block inserted under each `##` header:
  a status word (`done` / `partial` / `not started` / `question`), one or
  two sentences, and a screenshot. This is the original wish ("my document,
  but with its additions") without breaking the single-writer rule, because
  the mirror is regenerated from the accepted spec, never hand-merged.
- `.symphony/idea.toml` keeps runtime configuration out of the Obsidian
  document. The agent may propose it while bootstrapping a repo; Symphony
  validates its command, port, and paths before execution. A repo without a
  truthful preview contract reports `question` instead of guessing on every
  run.
- The declared preview port is stable for the life of the ideas project. The
  start argv may use `{port}`, and every preview also receives `HOST=127.0.0.1`
  and `PORT=<effective-port>`. Honoring one of those mechanisms lets the
  persistent manager probe a candidate on a temporary port without interrupting
  the last-good release.
- Screenshots go in `idea/assets/`, referenced by relative link so both
  GitHub and Obsidian render them. PNG, capped at ~1280 px wide and
  compressed, to keep repo size sane. The agent overwrites the screenshot
  for a feature on each run rather than accumulating history — git history
  is the history.

## Sync: one vault, a dumb script, git as the only transport

One dedicated Obsidian vault (e.g. `~/IdeasVault/`) holds the `SPEC.md` and
`PROGRESS.md` of every ideas-tier repo, named per repo:

```
IdeasVault/
  some-idea/
    SPEC.md
    PROGRESS.md
    assets/...
  another-idea/
    SPEC.md
    ...
```

A small tool on the user's PC (`ideasync`, run by a launchd/systemd timer
every couple of minutes, plus a manual command) operates on **dedicated
managed clones under its own data directory — never on development
checkouts**. Automation that pulls, commits, and pushes on a timer must not
be able to switch branches, stage unrelated work, or race the user's own
git state. One pass per repo:

1. Take a per-repo lock (timer and manual runs must not overlap), fetch,
   and fast-forward the managed clone. If it cannot fast-forward, stop and
   surface the problem — never reset, never force.
2. Copy repo → vault: `PROGRESS.md` + `assets/` (agent-owned, inbound
   only), compared by content hash, not modification time.
3. Copy vault → repo: `SPEC.md` (user-owned, outbound only), after a short
   quiet period (~30 s since the last edit) so half-typed sentences don't
   trigger agent runs. Frontmatter `repo:` decides where the file belongs,
   so the tool discovers idea files anywhere in the vault, and the vault
   never needs the rest of the repository loaded into Obsidian.
4. If `SPEC.md` changed: stage exactly `idea/SPEC.md` (never `git add
   --all`), commit with a predictable message, push. If the push loses a
   race, rebase the one narrow spec commit once; on conflict, keep the
   vault file, stop, and report.
5. Write sync health to `_ideasync/STATUS.md` inside the vault and raise an
   OS notification on failure — silent background failure would destroy
   trust in the whole workflow.

Because each file has exactly one writer and one direction, there is no
conflict resolution, no mtime comparison, no merge logic. The only conflict
possible is the user editing `SPEC.md` on two machines between syncs, which
is an ordinary git conflict in the user's own file — surfaced, not silently
resolved (a single designated writing machine or Obsidian Sync solves that
separately).

Between the user's PC and the VM running Symphony, **git is the sync
channel**. No new transport: the VM already clones and pushes repos; the
script only bridges vault ↔ local clone on the user's machine.

## What the agent does per run (Tier 2 lifecycle)

Trigger: the Symphony scheduler (reusing the existing reconciler loop)
notices that `idea/SPEC.md` on `main` has a new content hash for an
ideas-tier-allowlisted repo. Same jobs/leases/backoff tables, new intake
source. The webhook path can also trigger it on push for low latency.

One run =

1. Claim lease, create isolated worktree (existing machinery, unchanged).
2. Diff current `SPEC.md` against the hash recorded at the last completed
   run; the diff plus `PROGRESS.md` is the prompt context. No issue
   splitting, no planning ceremony.
3. Implement the changed/new wishes directly in the worktree.
4. Run the repo's quality gate if one exists (`.openhands/quality-gate.sh`,
   existing convention) — advisory in Tier 2, not blocking.
5. Launch the app using the preview contract in `.symphony/idea.toml`,
   drive it with the already-installed `browser-harness` CDP CLI, and
   capture one screenshot per affected `##` header into `idea/assets/`.
   The preview boot + health check is the one mandatory gate: code that
   cannot start never publishes.
6. Rewrite `PROGRESS.md`: statuses, one-liners, screenshot links. If a wish
   is ambiguous or blocked, its status becomes `question` with one focused
   question — that replaces Tier 1's needs-guidance comment, and the user
   answers by editing `SPEC.md`.
7. Commit code, progress, and assets together (accepted spec hash in the
   commit message) and push to `main`, fast-forward only. Then the preview
   manager deploys the published commit. No PR, no status comment, no
   labels.

Bounded loops, provider selection, and per-run budgets are reused from
Tier 1 config. `guard_code_mutation`'s live-issue checks are replaced by an
ideas publication guard, checked immediately before the push:

- `SPEC.md` still matches the hash this run accepted — a newer spec marks
  the run **superseded**: cancel when safe, queue only the newest revision,
  never publish stale work;
- the agent left `SPEC.md` byte-for-byte untouched;
- the remote default branch has not moved incompatibly — a plain
  non-fast-forward failure is a normal race handled by one rebase/retry,
  never by force;
- the repo is still private and only in the ideas allowlist.

The original design used a unique `(repository, spec_hash)` run key. The current
implementation coalesces repeated observations of the current hash and permits
a new run when an older version is restored after an intervening change; see
[architecture](architecture.md). This retains idempotency without ignoring reverts.

Durable state is an `IdeaProject` per repo (latest observed spec hash,
latest completed hash, last good preview commit, preview state) and one
`IdeaRun` per newly accepted specification observation:

```text
discovered -> queued -> running -> published
                   |         |-> question
                   |         |-> failed
                   |         |-> superseded
                   +---------+-> queued (bounded retry)
```

No fake issue numbers anywhere in this model — ideas runs are their own
entity over the shared execution primitives.

## Live preview: a separate capability from screenshots

"Run the live version of the app" is not satisfied by the agent briefly
booting the app inside its worktree — that process dies with the run and
its path is not a stable endpoint. A small preview manager on the VM
provides:

- one stable preview checkout and loopback port per idea repo;
- runs as a credential-free account with resource and process limits — no
  provider, GitHub, or orchestrator secrets;
- start command and health check taken from `.symphony/idea.toml`;
- deploys only a published commit that passed the pre-publication boot
  check, and keeps the **last good commit running** when a new one fails —
  a bad build can never take down the demo;
- reached via `ideasync open <repo>`, which opens an SSH local forward —
  never a publicly listening port, consistent with the existing
  non-exposure boundary.

The preview SSH destination is stored by `ideasync init --preview-host`; a
per-invocation `--host` remains available as an override. `ideasync add` pins
the repository's declared preview port locally so a rolled-back release remains
reachable even if a later commit attempts to change the contract.

## Deliberate omissions (and why)

- **No issue-splitting agent.** Correct call to skip it: issues buy
  traceability, parallel assignment, and review granularity, none of which
  matter pre-validation, and generating them spends compute on making the
  project *look* managed rather than building it. The spec-file diff
  already answers "what changed"; `PROGRESS.md` already answers "what's
  done". Issues re-enter at graduation to Tier 1.
- **No review pass, no draft PR.** Idea-stage output is judged by running
  the app, not by reading the diff.
- **No new UI.** Obsidian is the UI.

## Safety boundary

Tier 2 lets an agent push to `main` with no human gate. That is only
acceptable because the blast radius is explicitly fenced:

- A separate `ideas_repositories` allowlist in `config.toml`, disjoint from
  the Tier 1 allowlist. Private repos only, same as today.
- The Tier 1 rule that repository code is trusted because repos are private
  and allowlisted carries over unchanged.
- A repo graduates by moving allowlists; nothing is ever in both.

## Implementation phases

**Phase 0 — contract only (an afternoon).** Write the file contract above
into a template repo, plus a standalone manual run prompt for one
ideas-mode run. No code. Exit criteria: the two documents are
understandable without instructions; regenerating `PROGRESS.md` cannot
change `SPEC.md`; a second spec edit feels materially easier than writing
a GitHub issue; the app remains reachable after the agent finishes.

**Phase 1 — `ideasync` (the real friction-killer, do this first).** The
tool as described: Python package + CLI (`init`, `add`, `sync`, `status`,
`doctor`, dry-run) with a macOS launchd installer first and the scheduler
kept behind an interface for later systemd support. Tests use temporary
bare git repositories. At this point the loop already works end-to-end
with the agent step performed manually (point any coding agent at a
checkout and the run prompt) — the cheapest way to validate the contract
before touching Symphony. Exit criteria: two idea repos sync in both
directions for days without touching a development clone; losing network
preserves local spec edits and recovers later; simulated concurrent
remote changes never cause a force, a silent overwrite, or unrelated
staging; timer and manual sync running together is harmless.

**Phase 2 — extract shared primitives, then a sibling coordinator.** The
current data model and coordinator are tightly coupled to issue numbers,
labels, comments, branches, and PRs, so ideas mode must not be bolted on
as exceptions inside that flow (and must not fake issue numbers). First
extract source-neutral primitives — leases, provider capacity, worktrees,
validation, reports — with zero behavior change to Tier 1 (the existing
test suite stays green). Then build the ideas coordinator beside the issue
coordinator on top of them: intake by spec hash (push webhook +
reconciliation), the Tier 2 prompt, durable run state with the superseded
transition, the publication guard. Both tiers share the same global and
per-provider concurrency limits, so an ideas backlog cannot starve real
work.

**Phase 3 — preview manager.** The persistent preview service described
above: health checks, last-good rollback, resource limits, cleanup, and
`ideasync open`. Implemented with fail-closed allowlisting, immutable
per-repository ports, candidate probing before cutover, bounded artifacts on
success and failure, and a configured SSH tunnel target.

**Phase 4 — graduation command.** `agentctl graduate <repo>` is always a
read-only dry run. It prints one proposed, unrouted GitHub issue for every
unfinished current `SPEC.md` section and an approval ID fingerprinting the
spec, default-branch commit, and Symphony configuration. Applying that exact
ID stops Symphony while the tier boundary moves, creates or reuses the issues,
atomically archives the active spec, progress, screenshots, and runtime
contract under `archive/ideas/<spec-blob>/`, moves the repository from the
Ideas allowlist to the Tier 1 allowlist, retires queued Ideas work, refreshes
the preview allowlist, and restarts Symphony. A changed spec, branch, or config
invalidates approval and requires a new dry run. The generated issues have no
intake labels; the user approves each one independently by adding
`agent:ready` and one `agent:*` provider label. The next vault sync recognizes
the archived contract as a non-failing retired repository and never recreates
the active spec; `ideasync remove <repo>` then deregisters it locally while
preserving the vault and retaining the managed clone for recovery.

## Open questions

- Repo size growth from screenshots; revisit git-lfs or an orphan branch
  for `assets/` only if it becomes a real problem.
- Preview isolation depth: a hardened credential-free systemd service
  first; container-backed previews become a per-repo option only if real
  project types demand it.
