# Kickoff prompt: ideas tier, second slice (Symphony automation)

Historical implementation prompt. The current Obsidian checklist and repeated
specification behavior are documented in [obsidian.md](obsidian.md) and
[architecture.md](architecture.md); they supersede this prompt's unique-hash rule.

Paste the block below into a fresh agent session in this repository to
implement Phase 2 of [ideas-tier.md](ideas-tier.md): shared-primitive
extraction plus the sibling ideas coordinator. Run it only after the
first slice ([ideas-kickoff-prompt.md](ideas-kickoff-prompt.md)) has
shipped and the contract has survived at least a week of manual runs —
this slice refactors the working Tier 1 service, so the file contract
must be settled first.

---

Read `docs/ideas-tier.md` — the plan of record — plus
`docs/architecture.md` and `docs/state-machine.md` before writing any
code. Where this prompt is more specific than those documents, this
prompt wins; on anything none of them cover, choose the boring, safe
option and note the choice.

Implement the ideas tier inside Symphony in two strictly ordered stages.
Do not start Stage B until Stage A is complete and green.

## Stage A: extract shared execution primitives

Today the data model and coordinator are coupled to issue numbers,
labels, comments, branches, and PRs. Extract source-neutral primitives —
repository leases, provider capacity/backoff, worktree management,
validation execution, and report writing — so a second coordinator can
sit beside the issue coordinator on top of them.

Hard rules for this stage:

- Zero behavior change to Tier 1. Existing status comments, guards, PRs,
  labels, recovery, and CLI output must be byte-for-byte equivalent.
- The entire existing test suite passes unmodified, except where a test
  imports a moved symbol — mechanical import updates only.
- SQLite schema changes ship as migrations that are idempotent on rerun
  and safe on an existing production database; add migration tests.
- No fake issue numbers anywhere in the new layer, and no ideas-specific
  logic yet — this stage is pure extraction.

## Stage B: the ideas coordinator

### Configuration boundary

A separate `[ideas]` allowlist in `config.toml` (repositories, private
only, spec/progress paths defaulting to `idea/SPEC.md` and
`idea/PROGRESS.md`). Config validation rejects any repository present in
both the Tier 1 and ideas allowlists. Direct default-branch pushes are
possible only for the ideas allowlist.

### Intake

- Accept a GitHub `push` webhook only for an allowlisted private idea
  repo, and only when the spec path may have changed.
- Reconciliation reads the default-branch spec blob hash for every idea
  repo, recovering missed deliveries.
- Frontmatter (`symphony: idea`, `repo:` matching the repository) is
  strictly validated before queueing.
- A unique `(repository, spec_hash)` run key makes webhook redelivery
  and reconciliation idempotent. If a newer spec revision appears while
  an older run is active, mark the old run superseded, cancel it when
  safe, and queue only the newest revision.

### State

`IdeaProject` per repo (latest observed spec hash, latest completed
hash, last good preview commit, preview state) and one `IdeaRun` per
accepted spec hash with states
`discovered -> queued -> running -> published | question | failed |
superseded`, plus bounded retry back to `queued`. Both tiers share the
same global and per-provider concurrency limits.

### One run

1. Claim the repository lease; fetch the exact accepted default-branch
   commit; create an isolated worktree; record spec hash and base commit.
2. Prompt the provider with: the full current spec, the diff from the
   last completed spec hash, the previous `PROGRESS.md`, and the repo's
   instructions. Ask for the smallest useful implementation of the
   changed wishes. Never ask it to create issues, plan, self-review, or
   touch GitHub.
3. Run configured checks as advisory (`partial` in the progress file).
   The one mandatory gate: boot the app via `.symphony/idea.toml` and
   pass its health check. Code that cannot start never publishes.
4. Drive the preview with the installed `browser-harness` CDP CLI;
   capture one screenshot per affected `##` section into `idea/assets/`
   (PNG, ≤1280 px wide, compressed, overwrite per section).
5. Regenerate `PROGRESS.md`: the accepted `SPEC.md` reproduced
   byte-for-byte with one terse result block per `##` header (status
   word, one or two sentences, screenshot link).
6. Ambiguity, secrets, destructive migrations, external side effects, or
   product decisions produce one `question` block and no unsafe work.
   The user answers by editing `SPEC.md`; the next revision resumes the
   loop.

### Publication guard (replaces `guard_code_mutation` for this tier)

Immediately before pushing, verify: the repo is private and only in the
ideas allowlist; remote `SPEC.md` still matches the accepted hash (else
supersede, never publish stale work); the agent left `SPEC.md`
byte-for-byte untouched (else fail the run); the remote branch is
fast-forward compatible (a plain non-fast-forward is a normal race —
one rebase/retry, never force). Then commit code, progress, and assets
together with the spec hash in the commit message and push to the
default branch.

### Operations

Extend `agentctl status` and `agentctl doctor` to report idea projects:
latest hashes, run state, last publication, pending questions. Reports
land in the existing report directory alongside Tier 1 reports.

## Out of scope

The persistent preview manager (`ideasync open` target, last-good
rollback service) is Phase 3; the in-run boot check above is enough for
this slice. Graduation is Phase 4. `ideasync` itself ships in the first
slice and is not touched here.

## Definition of done

- Stage A: full pre-existing suite green with only mechanical import
  changes; migration tests pass on both fresh and existing databases.
- Stage B acceptance tests: duplicate webhook deliveries create one run;
  a newer spec supersedes an active run without publishing stale work;
  a run that modifies `SPEC.md` fails without pushing; a non-fast-forward
  race retries once and loses no work; a `question` round-trips through
  `PROGRESS.md` and the next spec edit requeues; a repo in both
  allowlists is rejected at config load; Tier 1 and ideas runs respect
  shared concurrency limits under contention.
- Ruff clean across old and new code.

Work through the stages in order, commit in logical units, and stop when
Stage B's acceptance tests pass — do not begin the preview manager.
