# Status links in the source project note

This release builds on the installed setup-recovery fix `d51a25f` and preserves
its reasoning controls, runtime versions, database code, and schema 8.

The Python vault bridge now adds a status link beside each explicit checklist
item and a repository summary in its original folder note. A completed task
links to the progress report at the exact recorded publication commit. The
per-repository `_symphony/owner--repository/STATUS.md` reports current Symphony
state, preview state, run details, and individual task state. These are recorded
orchestrator states, not a claim about all GitHub checks or production health.

No model writes these annotations or receives them in its specification. The
existing systemd vault exclusion still applies to the model worker. Waypoint
continues to own its generated index; Symphony annotates the explicit task list
outside that index and leaves the index unchanged.

## Preservation and verification

Generated spans carry SHA-256 checksums. Malformed, duplicated, or edited spans
pause processing without overwriting them. Removing valid spans reconstructs
the exact source text. Tests cover list and table rows, BOM and CRLF, notes
without a final newline, repeated reconciliation, provider-input exclusion,
status transitions, changed requirements, incomplete validation, manually
checked tasks, and concurrent edits. Original source bytes are retained before
every write. The atomic exchange cleanup also retains any edit caught during a
rollback race before disposing of the temporary file.

The existing note-to-scheduler-to-Git test verifies that adding status links
does not launch a second provider run or change the accepted specification.
No live note edits or manual retries are used for acceptance.

## Deployment

Build the matching wheel, stage the committed release on VM101, and run the
application updater with `--check --expected-commit <full SHA>`. After that
read-only compatibility preflight, the user approves installation with an SSH
TTY and sudo using the staged updater path. The updater retains source/runtime,
configuration, and an integrity-checked database copy, then installs the wheel.

After approval, let normal reconciliation generate the links. Verify installed
source, service health, task/result/repository link targets, and the Immich
project's retained published state. Programmatically strip only verified status
spans and compare the original note and linked brief hashes against the captured
pre-update hashes. Confirm the accepted specification and publication commit
are unchanged, with no extra implementation run. Record actual acceptance
results after the user completes the update.

Before staging, the local suite passed all 283 tests; Ruff and whitespace checks
passed. Pre-update hashes of the live Immich folder note, linked brief, and
Waypoint block were captured read-only for the post-approval comparison. The
release makes no manual change to those notes or to any run state.
