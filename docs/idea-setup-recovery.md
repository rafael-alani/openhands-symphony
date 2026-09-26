# Empty briefs and setup repair

This application-only fix builds on deployed revision
`a6e36fb6db0cf78fb845221c48eb00b7f5e5fc5a`. It retains database schema 8,
runtime pins, provider controls, and extra-high/normal defaults.

The original Immich note contained only a Waypoint index. Symphony stripped
that index, accepted an empty specification, and started a model. The operator
then added an explicit checklist entry; intake accepted the actual brief.
The earlier run repeatedly reported `queued, setup-failed`.

The retry defect is reproducible: after a model writes a broken setup script,
post-implementation setup queues another attempt. Pre-implementation setup
then rejects the same retained script before the model can repair it. Because
no new model attempt starts, the attempt counter never reaches its limit.

The fix rejects empty notes after excluding Waypoint, before any repository
creation or model launch, and reports how to add an explicit brief. Correcting
the note resumes intake through normal reconciliation. After a model attempt,
failed setup is supplied as redacted repair evidence to the next bounded model
attempt. Setup still has to pass after repair before any application publication.
Initial setup failures and exceptions before a new provider turn fail visibly
instead of entering an unbounded queue loop.

## Evidence and limits

On 2026-09-26, all seven initial regression cases failed against the deployed
baseline. The updated suite passes 260 tests locally, including repair after
restarting the coordinator with a persisted `setup-failed` run, exhausted repair
attempts, rejected provider launches, diagnostic redaction, and empty-note
correction through normal intake. Ruff and whitespace checks pass.

Before this fix was installed, read-only inspection found that the real Immich
project had already published commit
`3b192490154b7daff80ba220797477f5ef219e42` at 07:56:45 UTC. Its generated
progress reports successful setup, quality-gate, and application-health checks.
Therefore this release cannot be credited with unsticking that already-completed
run. Its setup failure output was not readable through the available SSH account;
the exact underlying dependency/build failure is not established here.

No project note, generated repository, run state, retry counter, or production
database is manually repaired by this change. Tests use disposable local
repositories and fake providers, without consuming model quota.

## Deployment and verification

Stage the reviewed Git revision and its matching wheel in a new checkout on
VM101. Use `scripts/update_application.py --check --expected-commit <full SHA>`
for the read-only compatibility preflight. The user then runs that same updater
with `sudo`, omitting `--check`. It checks idle leases, retains the previous
software/configuration and an integrity-checked database copy, and installs only
compatible application code. It does not reset project state or force a retry.

After the user completes the update, verify `DEPLOYED_COMMIT`, compare installed
Python sources with the staged release, and inspect service health and the next
normal reconciliation. Confirm the Immich project remains published, its accepted
spec includes `General Idea.md`, and its checklist completion is retained. Do not
restart a completed run or introduce another live project solely to demonstrate
the regression. Linux preflight and post-update production checks should be
recorded separately; they are not implied by the local test results above.
