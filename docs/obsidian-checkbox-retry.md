# Checkbox retries and preserved run history

This release builds on the installed launch-permission fix `ef6f8b1` and retains
its installed-command verification and rollback checks. Database schema 8 and
`store.py` are unchanged. Checkbox commands use the existing metadata table;
accepted retries create new rows in the existing run/event tables.

## User workflow

With checkbox management enabled, a checked task means its attempt finished.
The linked outcome distinguishes Completed, Failed, Needs guidance, and Partial.
Unchecking requests another try for that task. Completed tasks remain context;
unfinished tasks not selected for that retry are explicitly deferred. Multiple
unchecks accepted together share one new run, and a request during an active run
waits until it finishes. Normal unchecked input never becomes a retry command.

The bridge detects a checked-to-unchecked edge only for the same content version,
persists the request, and validates the source again at intake. Rechecking before
acceptance cancels the request. Changed/removed input cancels a stale command.
Paused projects, GitHub-mode projects, disabled checkbox management, retained
leases, and source conflicts prevent retry intake. A transaction consumes the
request and creates one new run at the latest accepted Git head with zero new
attempts. Older run IDs, counters, events, checks, worktrees, and published commits
are preserved. The new run goes through the normal authentication, lease,
implementation, validation, preview, and publication gates.

If the requested recheck produces no changes, publication may reuse only the
exact commit that was just validated. A concurrent Git advance or a differing
local head prevents that shortcut. The new run still records its own attempts
and a no-change result in history.

## Reports and preservation

The project folder note retains its compact task/status links. The generated
local `STATUS.md` and `PROGRESS.md` place Run history at the bottom, newest first,
with Markdown headings for runs, attempts, and checks. Earlier immutable GitHub
result links remain available there. Existing opaque provider configuration
tails are summarized rather than copied into notes; original diagnostics remain
in appdata and server logs. The Git-owned progress mirror and model input do not
contain the generated vault history.

No model edits Obsidian files. Checklist/status writes retain the existing exact
source guards, checksums, atomic exchange, and original-byte preservation.
The new control ledger is checkpointed after a guarded checkbox write. Automatic
ticks and old completion recovery cannot recheck an accepted pending retry.

## Verification and rollout

Tests cover repeated scans, restarts, simultaneous intake, multiple unchecks,
task scope, canceled/stale requests, active-run deferral, mode changes, disabled
checkbox management, failure versus completion, upgrade behavior, and retained
history. The real local-Git scheduler integration unchecks a completed task,
creates and publishes a fresh run without changing the specification, checks it
again, and verifies that prior runs and model-input exclusions remain intact.

Stage the matching source/wheel and run the Linux suite and read-only update
preflight. The user approves installation through the existing SSH/sudo command.
Afterward, inspect normal reconciliation and generated reports. A live retry
acceptance check requires the user's own checkbox change; do not edit a vault
note or reset a production run manually to exercise it.
