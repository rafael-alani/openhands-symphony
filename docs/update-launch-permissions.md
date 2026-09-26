# Application updater launch-permission regression

The application update installed a Git checkout whose ACP wrappers were tracked
as non-executable files. Unlike the full installer, the updater did not explicitly
restore script mode 0755. Its recursive `chmod a+rX` makes directories traversable
and preserves existing execution bits, but does not add execution permission to
a regular file that has none. The installed Codex wrapper was mode 0664.

On 2026-09-26, the new Immich idea run exhausted three attempts at
11:42:04, 11:42:41, and 11:43:19 UTC. Each Agent Server launch failed with
`Permission denied` for `/opt/openhands-symphony/scripts/codex_acp_wrapper.sh`.
No Codex session started. The earlier published preview remained healthy.
The settings preflight had launched a temporary adapter directly with Node,
so it did not exercise the installed wrapper or detect this deployment defect.

## Fix and release gate

The updater now restores mode 0755 on installed shell/Python scripts, matching
the full installer. Codex and Claude wrappers also carry executable modes in
Git. Software synchronization compares checksums so deployment and rollback
restore the right bytes even when file sizes and timestamps match.

After installing software and patching the adapter, but before recording the
release or restarting Symphony, the updater checks each enabled provider's
configured executable as `openhands-agent`. It then directly launches the
configured Codex command as that account and verifies authenticated ACP
initialization and session settings for extra-high/normal and low/fast.
This post-install probe sends no model prompts. It never invokes a shell to
bypass a missing executable bit. Worker-access or handshake failures enter the
existing software/configuration rollback and restore the prior service/timer
states. Evidence is retained as `installed-launch-probe.jsonl` alongside the
root-only deployment rollback.

The original temporary-adapter preflight still runs before the service is
stopped. The new probe establishes that the installed path can actually launch;
it does not establish available model quota or successful task implementation.

## Regression coverage

Tests reproduce a non-executable checkout, synchronize it into an installation,
observe the original execution failure, and verify successful direct execution
after the permission repair, including a second update. Other tests exercise the
real probe subprocess against deterministic ACP responses, reject missing
execute permissions and unapplied settings, and assert that no model prompt is
sent. Updater transaction tests cover a successful release plus worker-access
and handshake failures, checking exact software/configuration restoration,
unchanged database contents, retained rollback evidence, and service restart
ordering.

## Deployment and acceptance

Stage the committed source and matching wheel on VM101, run Linux tests and
`scripts/update_application.py --check --expected-commit <SHA>`, and obtain the
user's normal SSH/sudo approval for installation. After installation, verify the
installed modes, successful post-install probe, deployed revision, and service
health read-only.

This release does not reset exhausted task attempts or force reconciliation.
The failed Immich task therefore remains failed until an explicit supported
retry is implemented or the workflow receives a new specification. No vault
notes, task results, or application data are edited by this update.

## Release validation, 2026-09-26

Release `ef6f8b1d5a67d3efeac87cd595e7f242d1278931` is published on
`codex/fix-update-launch-permissions` and staged with its matching wheel at
`/home/afa/symphony-launch-ef6f8b1` on VM101. All 296 tests passed locally
(28.68 seconds) and on Linux (11.06 seconds), including 13 new regression
cases. Ruff and whitespace checks passed. The Linux updater's read-only
compatibility preflight passed against installed release `ac8563c`.

Installation and the real worker's post-install authenticated launch probe
remain pending the user's SSH/sudo approval. Production files and failed task
state have not been repaired or reset while staging this release.
