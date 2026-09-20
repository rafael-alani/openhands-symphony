# Homelab acceptance test: folder note and checklist

The supplied `examples/obsidian-test/Symphony workflow test/` folder starts
paused so copying it into a synced vault cannot launch work. The sequence below
is also the repeatable acceptance procedure.

## Live deployment, 2026-09-20

VM101 now runs Syncthing v2.1.5 from the official stable-v2 APT channel, paired
with Infra's existing `mdshx-oxig9` folder at `/obsidian`. The existing laptop
pairing and canonical vault permissions were preserved. Initial synchronization
and a bidirectional test-file round trip passed with zero pending files/bytes or
folder errors. The laptop was disconnected during this server-to-server test.

The deployed source is the pushed `codex/obsidian-syncthing-acceptance` branch.
`/opt/openhands-symphony/DEPLOYED_COMMIT` records the exact installed revision;
the Git checkout is `/home/afa/symphony-deploy-source`. GitHub authentication
as `rafael-alani` and Codex/Claude authentication were verified without copying
or exposing credentials. All 227 Symphony tests, 21 legacy bridge tests, and
lint passed on the VM in `/home/afa/symphony-acceptance-20260920`; XML results
are retained as `tests-20260920.xml` and `bridge-20260920.xml`.

The test uses private repository
[`rafael-alani/symphony-workflow-test-812e73e7`](https://github.com/rafael-alani/symphony-workflow-test-812e73e7)
and preview port 10000. The first real Codex run published a working counter;
mouse and keyboard activation were verified in the browser and its two tests
passed independently. Its initial gate path was not discoverable by Symphony;
the prompt now explicitly requires `.openhands/quality-gate.sh`. The subsequent
subfile edit reopened its checkbox, published increments of two, passed the
wrapper's discovered quality gate, refreshed the same preview, and checked the
box again. Restoring the byte-identical original Counter note created another
run and returned the browser behavior to increments of one. A Waypoint-only
change during that run did not add another run; the settled checkpoint still
contained exactly three runs. Adding Reset to the table and explicitly overriding
the old increment rule in the main brief produced a fourth run: Add changed
0 → 3, keyboard Reset returned 3 → 0, validation passed, and both boxes checked.

The deliberately unresolved four-versus-five requirement produced the question
“Should every Add increment the counter by four or by five?” The published diff
contained only `idea/SPEC.md` and `idea/PROGRESS.md`; application code was
unchanged, the pending Counter box stayed open, and Reset stayed checked.
Switching to GitHub created the contract labels and left the total at five
idea runs while notes were edited and the contradiction was resolved locally.

GitHub issue #1 produced [draft PR #2](https://github.com/rafael-alani/symphony-workflow-test-812e73e7/pull/2)
with a real setup/build pass and four passing application tests under the
credential-free wrapper. Returning to note mode was requested during that job;
the old mode remained effective while work finished, then note intake resumed.
The sixth idea run added “Notes resumed,” passed the quality gate, retained
Add 3 and Reset, and refreshed port 10000. Both interactions and the new caption
were checked in the browser. The PR remains open and unmerged; no GitHub CI
workflow is configured for this disposable repository.

The sample is left `symphony: paused`. Pausing also stops its managed preview;
set it back to `idea` when ready for further note work. Syncthing and
the Symphony stack remain enabled. The test notes, repository, draft PR,
progress, screenshots, worktrees, and rollback inputs are retained.

### Published acceptance history

| Check | Run ID | Published commit | Result |
| --- | --- | --- | --- |
| Initial counter | `61d94abd-803f-4cb8-be57-b0aa649ec364` | `ec759cf0903b74f1bc7a4397876a6caa20e9270a` | Browser mouse/keyboard and two tests passed; missing gate discovery corrected next |
| Linked file adds two | `f5d3524d-6b2f-46a2-8e5e-e674cce45eba` | `5f3f590e477bc345faaa69d7814509ff42a03c94` | Gate, preview, reopen/check passed |
| Exact old-version restore | `968ad8ee-03b1-4a7d-a37f-c314e5cb4eeb` | `523d19bd1991833b4dc8b19b120dc108ce274231` | Adds one; Waypoint caused no extra run |
| Reset and main precedence | `10e194a3-1b85-4073-8027-16f33fd84fea` | `2a8ab29e33994464ad5c700f7b1a2440869dae7d` | Adds three, resets to zero, both boxes checked |
| Unresolved contradiction | `b28fe0ec-64d1-43ff-92ea-53790eba08c1` | `44936e44c9b3114217a5325626c85176a233958d` | Question only; no application-code changes or completion tick |
| GitHub issue | `84896bac-97a4-4ff4-b55d-e45cec866dca` | `67fe443066226dc346f389f0d58ea68a2c515675` | Draft PR #2; four tests and build passed |
| Notes resumed | `7cf7e120-cfa1-4e63-9880-5440f26dfbfc` | `94dc8134caeeca2438153baf28bb896c810725d7` | Caption, Add 3, Reset, gate and preview passed |

Machine-readable checkpoints are in
[evidence/obsidian-acceptance-20260920.json](evidence/obsidian-acceptance-20260920.json).

Live failures found and corrected during acceptance:

- Installer output inherited root's restrictive umask, making generated
  runtimes unreadable. Installation now sets the intended software umask and
  repairs only generated runtime access.
- Stale backend pins attempted to start Automation against a newer database.
  The original database was preserved; Agent Server 1.49.1 and Automation 1.13.1
  match the existing schema. Doctor now checks Automation readiness explicitly.
- Validator setup ran before sharing a new idea worktree. Sharing now precedes
  setup; failures before a provider attempt no longer loop indefinitely.
- A private partial clone fetched new blobs during `git worktree add` using the
  credential-free validator environment. This Git operation now uses the
  orchestrator environment with hooks disabled.
- Generated progress directories now inherit the shared vault group correctly.
  The localhost Syncthing control API requires authentication as well as
  filesystem isolation, and recovered attempts clear obsolete error messages.

The agent, validator, and preview users cannot read or traverse `/obsidian`.
Positive-PID checks inside each relevant live service namespace also confirmed
source-note access is denied. Only Symphony's wrapper handles source checkboxes
and repository metadata; the provider receives the compiled specification.

Rollback inputs are retained under `/root/symphony-rollback-20260920`, including
original source/runtime/configuration and a consistent, integrity-checked SQLite
snapshot. Infra's pre-pairing config is retained under Syncthing's appdata
`recovery/symphony-20260920`. The scheduled PVE appdata job succeeded, but it
establishes neither VM101 nor shared-vault backup coverage. No guest reboot,
router change, or shared-data permission rewrite was performed.

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
