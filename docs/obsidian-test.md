# Homelab acceptance test: folder note and checklist

This is a prepared test, not evidence that a live model run has passed.
Use the supplied `examples/obsidian-test/Symphony workflow test/` folder.
It starts paused, so copying it into a synced vault cannot launch work.

## Deployment preparation, 2026-09-20

Live inspection again found VM101 running the older Symphony service without
Syncthing or `/obsidian`. Infra Syncthing is healthy on v2.1.5, sharing the
existing `mdshx-oxig9` folder with the laptop. Its index reports 17,306 files,
no outstanding transfers, and no folder errors. PVE reports healthy pools and
the scheduled appdata job completed successfully; that job does not establish
backup coverage for VM101 or the shared vault.

The current source is staged at `/home/afa/symphony-acceptance-20260920`.
All 220 Symphony tests, 21 legacy bridge tests, and lint passed there.
`tests-20260920.xml` and `bridge-20260920.xml` retain the results. The three
additional tests cover the new installer filesystem grants. The original
upload manifest predates these installer additions and must not be treated as
the final deployment manifest.

`scripts/install_syncthing.sh` is prepared for the official stable-v2 APT
channel and a dedicated transport account. `configure_vault.py` now prepares
the shared vault group and explicit worker namespace exclusions. Their live
installation and permissions checks are still pending. Interactive VM sudo
authentication was requested; no root deployment session was available at
this checkpoint. The installed service, pairing, and canonical notes remain
unchanged. GitHub service-account and provider acceptance are still pending.

## Predeployment verification, 2026-09-19

The local Symphony suite passed 217 tests; the legacy bridge suite passed 21.
The same source was staged without sudo at
`/home/afa/symphony-preflight-20260919-igi_py2_` on VM101. Its isolated Python
3.12 environment, installed from `uv.lock`, passed all 217 Symphony tests,
21 legacy bridge tests, and lint. The XML results are `preflight-tests.xml`
and `preflight-ideasync.xml` in that staging directory. `SOURCE_MANIFEST.json`
records the base commit and SHA-256 of every packaged source file, including
local changes; this is a source snapshot, not a claim that GitHub has been updated.

Coverage includes real local Git publication, second edits to linked files,
checkbox updates without repeat runs, exact specification reverts, SQLite
migration with active leases and history retained, atomic note edits on Linux,
and fresh preview dependency downloads. Provider/GitHub calls in these tests
are fakes, so they do not establish real account authentication or product quality.
No production service, canonical vault note, device pairing, or repository was
changed by the staged test run. The installed VM service remains on its old source.

## Observed prerequisites, 2026-09-19

- PVE `192.168.0.250`: pools healthy; `/vault/shared/media/obsidian` is on
  `vault/shared`, owned by `101000:101000`, mode `750`.
- CT110 Syncthing is healthy. Its existing Obsidian folder is `mdshx-oxig9`,
  send-and-receive, with two configured devices. No pairing was changed.
- VM101 at `192.168.0.126` is running Ubuntu with hostname `symphony` and an
  active older Symphony stack. `/opt/openhands-symphony` is a source copy,
  not a Git checkout. No `/obsidian` directory or Syncthing executable was found.
- SSH as `afa` works. `sudo -n true` requires interactive authentication;
  root SSH is not available. Do the privileged deployment from an authenticated
  VM terminal or arrange task-scoped administrator access. Do not send passwords
  in chat or replace the host's SSH/firewall configuration for this test.
- No QEMU guest agent is configured. It is not required for this workflow;
  no VM reboot or guest-option change was made.

## Deployment preparation

1. Preserve the installed source, current configuration, and consistent SQLite
   state before the update. Keep provider and GitHub credentials on the VM.
   Stop/drain active agent work before installing. Retain the prior source and
   state as the test rollback point; do not delete any repository or vault note.
2. Deploy a reviewed commit containing the new code and update from that source
   with `sudo ./install.sh --update`. The source must include `vault.py`,
   `vault_project.py`, and `scripts/configure_vault.py`; an older GitHub checkout
   without the local changes will not support this workflow.
3. Follow [Obsidian setup](obsidian.md): enable `[vault]`, set owner/provider,
   and configure a VM-local Syncthing copy at `/obsidian`. Pair it with the
   existing Infra folder, keeping both send-and-receive. Provide the service
   and Syncthing shared read/write access without recursively changing the
   canonical homelab vault's ownership. Rerun the installer after enabling
   vault intake so its filesystem override is installed.
4. Verify `agentctl doctor` as the Symphony service account, GitHub private-repo
   creation authority, provider authentication, preview manager health, and a
   harmless Syncthing round trip before enabling the test note. Do not start a
   second bridge (`ideasync`) against this same project.

## Acceptance sequence

Copy the fixture folder into `1. Projects & Tasks` using Obsidian/Syncthing.
Keep its root `symphony: paused` until the following check succeeds on the VM:

```bash
sudo -iu openhands-symphony agentctl vault-check \
  '/obsidian/1. Projects & Tasks/Symphony workflow test/Symphony workflow test.md' \
  --vault-root /obsidian
```

It should report exactly one checklist file, `Counter.md`. `Reset.md` appears
in Waypoint but must not enter the specification yet.

1. Change `symphony: paused` to `symphony: idea` in Obsidian. Confirm one private
   repository is created and its `repo` property returns to the same main note.
2. Wait for progress and the preview. Confirm the Add button works, the initial
   dependency install/build succeeded, the quality gate passed, and Counter's
   checkbox becomes checked. Record the run ID, commit, and assigned preview port.
3. Edit only `Counter.md`: require the counter to increment by two instead of
   one, updating its acceptance test requirement. Confirm its checkbox reopens,
   a new run implements the change, and the same repository/preview are reused.
   Restore the exact prior Counter contents and confirm another run returns the
   behavior to incrementing by one; previously seen content must not be skipped.
4. Change only Waypoint's generated index. Confirm this produces no new run.
5. Add `| [ ] | [[Reset]] | Add reset while preserving the counter. |` to the
   table. Confirm Reset is now implemented and checked without duplicating the app.
6. Put a conflicting instruction in the main brief and explain explicitly
   which behavior should replace the old one. Confirm the intended replacement.
   Separately try a deliberately unresolved contradiction and verify that the
   real provider asks for guidance; semantic interpretation is not established
   by unit tests or a fake provider.
7. Switch to `github`. Confirm active work drains, note edits stop launching
   implementations, and routing labels exist. Create a disposable issue and
   label it ready for the configured provider; verify a draft PR, without merging.
8. Switch back to `idea`, make an explicit note change, and verify note intake
   resumes while existing issues/PRs remain. Finish at `paused` to stop new work.

Use an SSH forward to inspect the assigned preview, for example:

```bash
ssh -N -L 10000:127.0.0.1:10000 afa@192.168.0.126
```

Substitute the actual assigned port. Retain the test project, commits, progress,
and screenshots as evidence. Pausing is sufficient after testing; deletion and
cleanup are separate actions. Syncthing is transport, not a backup.
