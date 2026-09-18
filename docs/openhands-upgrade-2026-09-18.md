# OpenHands 1.20.0 on VM101

VM101 (`symphony`, Ubuntu 26.04 LTS, `192.168.0.126`) is the existing
OpenHands host. On 2026-09-18 it was powered off. Starting it restored the
installed Canvas 1.4.0 and Symphony systemd services; the existing UI returned
HTTP 200. It has 6 virtual CPUs, a 32 GiB maximum allocation with ballooning,
and a 256 GiB virtual disk. The VM disk currently resides on HDD-backed
`/vault/data`; this is separate from the canonical Docker appdata dataset.

## Deployment choice

Keep the dedicated VM and the existing unprivileged native systemd service.
This retains provider logins, account separation, headless Chromium and
Symphony's existing API integration. A desktop session is unnecessary: Canvas
serves a web UI and Chromium already runs headlessly. Docker is also supported
by upstream and would work inside a VM, but migrating this established native
installation adds work without helping this upgrade. Do not give the agent
Proxmox credentials or broad production storage mounts.

## Version selection and verification

The official npm registry and GitHub release identify Canvas **1.20.0** as
current on 2026-09-18. Its package manifest requires Node **>=24**, even though
some upstream prose still mentions Node 22. The release's own defaults select
Agent Server **1.49.1** and automation **1.13.1**. Node **24.21.0** is the current
24 LTS release verified against the official Node distribution metadata.

An isolated installation under `/home/afa/openhands-upgrade-1.20.0` on VM101
used a disposable home and a random API key, without production credentials.
The real HTTP API and deterministic ACP mock proved:

- two conversations finish in separate workspaces;
- an interrupted conversation reaches `paused` and resumes to `finished`;
- the server reports SDK/tools/workspace/server version 1.49.1;
- the Canvas UI and automation health endpoints respond;
- the conversation search API supports the upgrade's idle preflight.

The focused provider, doctor and installation test suite passed (27 tests).
This is **not** evidence of a production state migration, provider OAuth
validity, or a real model-backed run. The system installation remained at
1.4.0 when these notes were written because `afa` requires interactive sudo
and direct root SSH is unavailable. No production backup has been claimed.

## Apply the scoped upgrade

Review `scripts/upgrade_canvas.sh`, then run it as root with `--apply`.
Without `--apply`, it only prints the plan. It upgrades Canvas and its runtime,
leaving Symphony code, provider CLIs, worktrees and browser state untouched.

It downloads pinned packages first, refuses active or unknown conversation
states, pauses the orchestrator/reconciler, stops Canvas, and archives Canvas's
`.openhands` state and the exact affected configuration. The archive is compared
with the source, extracted into a root-only restore-check directory and
compared again. This provides a local rollback test before migration.
The old Canvas installation, archive, extraction and generated rollback script
remain under `/opt` and `/var/backups/openhands-symphony/canvas-upgrade-TIMESTAMP`.
These are local VM rollback artifacts, not an off-host backup or PBS coverage.

The script retains the current service identity and filesystem limits, changes
only the runtime pins, and atomically extends the existing service firewall to
cover its frontend and internal runtime ports. It checks the new Agent Server,
UI and automation health before resuming the previously active scheduler.
If readiness fails after replacement, it leaves Canvas and schedulers stopped
and prints the exact rollback command. Rollback preserves failed new state
under a timestamped name before restoring the old state; it deletes no history.

The script first requires the three affected source files to match their deployed
copies, then updates their version/service/firewall pins together. Preserve these
three source edits when reconciling the upgrade commit into the VM checkout;
discarding them would let a later `agentctl update` restore the old versions.
The separate local development checkout contains unrelated uncommitted work and
was not edited for this upgrade.

## Access and remaining acceptance

```bash
ssh -N -L 8000:127.0.0.1:8000 afa@192.168.0.126
```

Open `http://127.0.0.1:8000`. The firewall restricts non-loopback ingress to
3001, 8000, 8787, 9222, 18000, 18001 and 19000. There are no router or public
proxy changes. Verify existing conversation history and a small authenticated
provider run after the production upgrade. VM101 remains outside dothomelab's
one-command guest rebuild and appdata PBS backup contract.

Sources: [release](https://github.com/OpenHands/OpenHands/releases/tag/v1.20.0),
[package manifest](https://github.com/OpenHands/OpenHands/blob/v1.20.0/package.json),
[runtime defaults](https://github.com/OpenHands/OpenHands/blob/v1.20.0/config/defaults.json),
[official VM guide](https://github.com/OpenHands/OpenHands/blob/v1.20.0/docs/SELF_HOSTING.md).
