# Hack campaign operation

Hack mode is a temporary overlay on a private GitHub- or Ideas-managed repository.
The repository keeps its home mode. The orchestrator owns the campaign branch;
normal issue and note intake waits until the campaign closes.

## Enable and start

Add an explicit repository allowlist to the service configuration:

```toml
[hack]
enabled = true
repositories = ["owner/project"]
provider = "codex"
max_parallel = 4
max_tasks = 100
reserve_slots = 1
task_timeout_seconds = 1800
fast_gate_timeout_seconds = 300
polish_seconds = 300
milestone_every = 5
publish_ideas = false
```

Raise `scheduler.global_concurrency` and the selected provider's concurrency
limit to the capacity the VM and subscription can sustain. At least one global
slot is reserved for controlled GitHub work. The maximum lane concurrency is
six; a provider limit of one still permits only one model turn at a time.

Deploy with `sudo agentctl update` after updating the checkout, then start:

```bash
agentctl hack start owner/project --hours 24
agentctl hack status owner/project
```

The repository must already be in an effective GitHub or Ideas home mode and
have no live execution lease. Starting again returns the existing campaign.
Each campaign has a hard deadline, at most seven days. Its scaffold runs alone
before any lane work. The scaffold creates or verifies the shared skeleton,
interface stubs, lane configuration, `.openhands/hack-gate.sh`, and
`.symphony/idea.toml` preview contract.

## Direct the board

Keep the user-owned `hack/BOARD.md` on the repository's default branch. Edit
and push that file from your clone; do not push to the integrator-owned
`hack/...` campaign branch. A structured board can name lanes and dependencies:

```markdown
# Demo board

## api
- [ ] [contract] Define the response types in server/contracts.py.
- [ ] [endpoint] Implement the response endpoint. <!-- depends: contract -->

## ui
- [ ] [screen] Build the screen against the frozen response shape. <!-- depends: contract -->
```

Lane boundaries live in `hack/LANES.toml` (or `.symphony/hack.toml`):

```toml
[lanes.api]
paths = ["server/**"]

[lanes.ui]
paths = ["app/**"]
```

The scaffold can create this file on the campaign branch. Existing explicit
boundaries take precedence over invented ones. Footprints must be disjoint;
the wrapper rejects a worker's commit if it touches another lane. A task
depending on an interface only starts after that interface task is integrated.

Board order controls the priority of unclaimed work. Running assignments are
frozen. Removing or checking an unclaimed row cancels that work; failed tasks
remain blocked until the director edits them. There is no automatic retry loop.
Use stable `[task-id]` prefixes when steering an existing task.

Each new board version queues a model dispatcher under the same provider and
attempt budget. It assigns work to the frozen lanes and can split cross-lane
requests into an interface task followed by dependent implementations. Its
generated plan is validated for lane membership, disjoint footprints, unique
task IDs and an acyclic dependency graph before claims resume. The original
board remains user-owned. A newer board supersedes obsolete unclaimed plans.
Checked tasks used as dependencies must already have an integrated result;
otherwise the board receives an actionable error.

## Integration, previews, and closure

The integrator serially rebases finished lane work onto the campaign head,
runs the fast gate as the credential-free validator, boots the candidate and
checks its health, then advances the campaign branch. Conflicts and failed
gates return to the board as blocked work. Model workers never publish.

Passing campaign commits feed the existing persistent preview manager.
Milestone screenshots are stored under `hack/assets/`. `hack/STATUS.md`
records work states and the closing result; `agentctl hack status` also shows
durable state and provider conversation IDs.
The latest status is also written to `reports/hack/CAMPAIGN_ID/STATUS.md` and,
for vault projects, `_symphony/owner--repo/hack/STATUS.md`. Completed demos stay
available while Hack remains enabled and the repository stays explicitly
allowlisted; removing that authorization stops the preview.

```bash
agentctl hack stop owner/project
```

An early stop prevents new lane work, drains claimed work, and queues one solo
polish pass if time remains. Near expiry, the same closing sequence starts
automatically. The hard deadline prevents new model turns and further lane
integration and cancels unfinished turns. The closing publication preserves
the last successfully integrated demo. Unconfirmed cancellation keeps the
repository fenced until recovery confirms the old worker has stopped.

The default is one final draft PR for either home mode. `publish_ideas = true`
permits a final fast-forward push only for an exclusively Ideas-managed private
repository whose default branch still matches the accepted starting commit.
Concurrent default-branch edits result in a final PR. GitHub home repositories
always require a human merge. Normal home-mode intake resumes after publication.

Campaign and task records survive restarts in the same SQLite database as
normal jobs. Expired turns are canceled before their leases are released.
Integrator operations use a separate durable lock, and the scheduler keeps
integration checks off its claim loop so urgent GitHub work can use its reserve.
