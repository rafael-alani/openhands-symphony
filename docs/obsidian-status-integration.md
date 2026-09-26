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

## Production acceptance, 2026-09-26

The server's isolated Linux suite passed all 283 tests and its deployment
compatibility preflight passed. After the user's sudo approval, read-only
verification at 10:40–10:42 UTC confirmed installed revision
`ac8563ce16266dd8732759fb7185454dda677138`, with all 40 installed Python
source files matching the staged release. Symphony, Canvas, and Syncthing were
active; Symphony and the published app health endpoints returned `status: ok`.
Codex defaults remained `xhigh` and `normal`.

Normal reconciliation completed at 10:40:59 UTC and generated the annotations
without a manual reconcile, note edit, state edit, or retry. The original Immich
folder note contains Completed and Result links on its General Idea row, plus
Repository, Current status, and Latest result links in its summary. Every local
target and task heading resolves. GitHub confirmed the immutable result report
exists at publication `3b192490154b7daff80ba220797477f5ef219e42`.

Programmatic removal of verified generated spans reconstructs the original
228-byte source note exactly (SHA-256
`884df09e3074eb346305cb60db8b2f86ccf16ebf6105d99fff1cddf7f53e4936`). The
668-byte linked brief and Waypoint block also match their pre-update hashes.
The annotation checksums and marker pairing are valid.

The per-repository status records the existing published run
`c25da71e-7637-4040-bddf-45002f7c3b4d`, last updated at 07:56:47 UTC, with
preview state healthy. The accepted specification remains blob
`c48804d07c48a0585d751c048b3c7c0a911d0e33`, the repository head remains
`bd63dce273c114d06afe884026fdd7a550e31b59`, and no new provider conversation
or prompt was recorded after this deployment. No new implementation or
status-only Git commit was generated for the project.
