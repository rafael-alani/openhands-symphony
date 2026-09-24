# Hack tier (Tier 3): time-boxed high-parallelism mode

Status: implemented locally; subscription-backed multi-lane VM acceptance has
not yet been run for this implementation. Companion to [ideas-tier.md](ideas-tier.md).
This document retains the original design. See [operation](hack-operations.md)
for the shipped configuration/board contract and [completion audit](implementation-audit.md)
for validation and remaining external prerequisites.

## Where it sits

Tiers 1 and 2 differ by project maturity (late vs early). Tier 3 differs by
**time pressure**, which is an orthogonal axis. It is therefore not a third
permanent repo category but a **time-boxed overlay**: a repo is flipped into
hack mode for a bounded window and automatically reverts to its home tier
when the window expires.

```bash
agentctl hack start <repo> --hours 24   # flip into hack mode, hard expiry
agentctl hack stop  <repo>              # early revert
```

Auto-expiry is a safety property, not a convenience: hack mode removes the
human merge gate and raises concurrency, and hackathons end. A repo must
not be able to linger gateless because someone forgot to turn it off.

While a campaign is active, the repo's normal Tier 1 or Tier 2 intake is
**suspended** — issue agents, idea agents, and hack agents must never
mutate the same repository concurrently. Suspension happens at
`hack start` and normal intake resumes at expiry or `hack stop`.

## What actually blocks parallelism today

Throwing more agents at one repo is not blocked by compute. Three specific
Symphony design choices serialize work, each deliberate for Tier 1:

1. **One implementation lease per repository.** The concurrency key is
   `owner/repo`, so a second agent cannot even claim work in the same repo.
2. **Draft PR + human merge.** Integration latency is however long the
   human takes; with N agents the human becomes the pipeline stall.
3. **Intake granularity.** Tier 1 issues are hand-authored (slow to write);
   Tier 2's single spec file is processed as one serialized job per change.

Tier 3 replaces exactly these three things and reuses everything else
(worktrees, providers, browser harness, SQLite store, backoff, reports).

## Replacement 1: lanes instead of one lease

The unit of parallelism is a **lane**: a named partition of the repo with a
declared file footprint (e.g. `api` = `server/**`, `ui` = `app/**`,
`infra` = top-level config). The concurrency key becomes
`owner/repo/<lane>` — N lanes, N concurrent implementation leases, while
each lane stays internally serial so an agent never races another in the
same files. Merge conflicts — the real enemy of parallel agents — are
prevented by construction rather than resolved after the fact.

Cross-lane needs are expressed as **contracts first**: a task that spans
lanes is split into "define the interface/stub" (one lane, runs first) and
per-lane implementations against the frozen stub (parallel, after).

## Replacement 2: an integrator instead of a human gate

Implementation agents never touch `main` — and neither does the integrator
until the very end. All integration happens on a **campaign branch**
(`hack/<date>-<slug>`), because hack mode is an overlay that may be applied
to a real Tier 1 repo, where continuously rewriting `main` with ungated
merges would be unacceptable. A single **integrator** lease per repo (the
only per-repo serialization left) runs a continuous merge loop:

1. Pick the oldest finished lane branch.
2. Rebase onto the campaign branch; trivial conflicts resolved, non-trivial
   ones bounce the task back to its lane with a conflict note (fixing is
   lane work, not integrator work).
3. Run the **fast gate**: build + boot + a smoke ping. Not the full
   quality gate — hack mode's bar is "the demo still runs", checked in
   seconds, not minutes.
4. Fast-forward the campaign branch, delete the lane branch, mark the task
   done.

This is a merge queue with a boot check as the only gate. The campaign
branch — not `main` — is what stays permanently demoable, and it feeds the
live demo through the Tier 2 preview manager pointed at the campaign
branch. The full quality gate can run as a background advisory job whose
failures become board tasks instead of blocking merges.

**Publication is a single event at the end of the campaign**, and the
policy follows the repo's home tier: a Tier 1 repo gets one final PR from
the campaign branch (a human merges, as always); a disposable Tier 2 repo
may be configured for one guarded final push to `main`. Individual lane
branches never reach `main` in either case.

## Replacement 3: a board and a dispatcher

Intake follows the ideas-tier file convention, extended to task
granularity, with the same single-writer split:

```
hack/
  BOARD.md     # user-owned: tasks as checkbox lines grouped under
               # ## lane headers, ordered by priority. The steering wheel.
  STATUS.md    # agent-owned: per-task state (queued/running/merged/
               # blocked/question), assigned lane, one-line notes.
  assets/      # agent-owned: milestone screenshots only.
```

A **dispatcher** step (cheap model, runs on every BOARD.md change) turns
new/edited lines into jobs: assigns each to a lane, orders within lanes,
splits cross-lane items into contract-then-implementations. This is the
splitting agent Tier 2 deliberately skipped — correctly skipped there,
because splitting only pays when the pieces run in parallel. Tier 3 is
precisely that case, and it earns its compute here.

The human's role shifts from reviewer to **director**: watch the demo,
edit `BOARD.md` (add tasks, reorder, strike things out), push. During a
hackathon you are at the keyboard, so the vault-sync script's
minutes-scale latency is bypassed — edit the file in the clone directly,
or in Canvas; the vault sync still works and simply matters less.

Steering has one freeze rule: **board edits apply to unclaimed work
only**. Running tasks finish under the assignment they started with, and
the dispatcher re-partitions only idle lanes — editing a document mid-run
must not silently redirect half a dozen active agents.

## Run lifecycle

1. **Scaffold (solo).** `hack start` queues one mandatory solo job before
   any fan-out: skeleton, lane boundaries proposed into `STATUS.md`,
   stubs, the fast gate script, a bootable hello-world. Parallelism only
   works against a shared skeleton; fanning out into an empty repo
   guarantees N conflicting opinions about project layout.
2. **Fan-out.** Dispatcher fills lanes from `BOARD.md`; one agent per lane
   up to the provider/global budget; integrator loop starts.
3. **Steer.** Director edits `BOARD.md` continuously. `blocked` and
   `question` statuses surface in `STATUS.md`; the director answers by
   editing the task line, which requeues it.
4. **Polish.** As the window nears its end (or on `hack stop`), fan-out
   stops, the integrator drains the queue, and one final solo pass runs on
   the campaign branch: cross-feature bugs, visual consistency, dead code,
   and the demo path. In a short campaign this single pass is worth more
   than any amount of per-task review would have been.
5. **Publish.** One publication per campaign — final PR or guarded push to
   `main`, per the home-tier policy above — plus final milestone
   screenshots and a closing summary in `STATUS.md` of what merged and
   what died on the board. Normal intake resumes and the repo reverts to
   its home tier.

## Speed calibration

- `max_attempts = 1`, short provider timeouts. A failed task is bounced to
  the board as `blocked` with the error tail, not retried — during a
  hackathon the director decides what is worth a second attempt, and a
  retry loop silently burning 30 minutes is worse than a visible failure.
- Screenshots at milestones (integrator, every M merges or on demand), not
  per task — per-task capture is Tier 2 pacing.
- No review passes, no status comments, no labels, no per-task PRs.
- Capacity is reserved, not seized: a campaign gets a temporary burst
  allocation while Tier 1 keeps at least one slot for urgent controlled
  work. Ideas runs queue at lower priority **only for the hours the
  campaign is active** — a spec edit during a hackathon is picked up as
  soon as the campaign releases its slots, and outside campaigns the
  ideas tier is a first-class consumer of capacity, not background work.
  Everything stops at the deadline or budget limit.
- Cap a campaign at 4–6 implementation agents. Beyond that, coordination,
  merge conflicts, provider limits, and test execution overwhelm the
  benefit unless the codebase is already highly modular — more lanes is
  not more speed.
- Resource honesty: N parallel agents need the top row of the README
  sizing table (12–16 vCPU, 32–64 GB). Hack mode is where that spend is
  justified *because* it is time-boxed; the global provider concurrency
  budget remains the ceiling.

## Safety boundary

- Same fencing as Tier 2: private, explicitly enabled repos only — plus
  the hard expiry, since this mode is gateless *and* high-throughput.
- Lanes are enforced, not advisory: the wrapper rejects a lane commit
  touching paths outside its footprint (the existing worktree-confinement
  check, narrowed to the lane's globs). Without enforcement, footprints
  drift and the no-conflict guarantee silently dies.
- The integrator is the only writer of the campaign branch; implementation
  agents get push access to their lane branch only; `main` is untouched
  until the single end-of-campaign publication.

## Implementation phases

**Phase A — lanes + integrator.** Concurrency key change
(`owner/repo/<lane>`), lane-footprint enforcement in the wrapper, the
integrator loop with the fast gate. This is the heart; everything else is
dressing.

**Phase B — board intake + dispatcher.** `hack/BOARD.md` watching,
dispatcher prompt, `STATUS.md` writer. Until then, lanes can be fed by
hand-written per-lane jobs, which is enough to validate Phase A.

**Phase C — mode lifecycle.** `agentctl hack start/stop`, expiry timer,
scaffold job, revert-to-home-tier, closing summary.

## Open questions

- Whether the integrator should auto-revert a merge that breaks the fast
  gate (merge-then-test vs test-then-merge). Test-then-merge is assumed
  above; it serializes integration but keeps the campaign branch
  demoable, which is the point of having one.
- Provider mix: pinning fast/cheap models for lane work and a stronger
  model for the dispatcher + integrator conflict notes is likely the
  right spend profile, and the per-provider budgets already exist.
