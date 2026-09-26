# Obsidian projects through Syncthing

The project starts as one ordinary Markdown note. Syncthing transports the
vault; Symphony reads the VM's local copy, owns Git operations, and writes
results back. `ideasync` is not part of this path.

## One-time VM setup

Keep `/vault/shared/media/obsidian` as the homelab vault source. Pair the
Symphony VM with the existing Syncthing folder and choose `/obsidian` as its
local folder path, with send-and-receive enabled. This creates an independent
VM copy; do not mount the entire homelab share into the model worker.
Syncthing pairing is an operator action; the installer does not alter existing
folders or devices.

Set the following in `/etc/openhands-symphony/config.toml`:

```toml
[github]
allowed_repositories = []
private_only = true

[vault]
enabled = true
path = "/obsidian"
projects_dir = "1. Projects & Tasks"
owner = "rafael-alani"
provider = "codex"
quiet_seconds = 30
port_start = 10000
port_end = 10999
manage_checkboxes = true
```

Keep the service/provider/scheduler blocks from `examples/config.toml`.
Remove the placeholder `[repositories."CHANGE_ME/CHANGE_ME"]` section. Existing
GitHub projects can remain in `github.allowed_repositories`; note-managed repos
are registered automatically. Authenticate GitHub as `openhands-symphony`
with permission to create private repositories under the configured owner,
and authenticate the selected provider as `openhands-agent`.

Run `sudo agentctl update` after deploying this source, or run
`sudo ./install.sh --update` from this checkout. The installer creates missing
vault directories and grants the service access through a systemd override.
For an already-existing vault, arrange read/write permissions for both
Syncthing and `openhands-symphony`; the installer preserves existing ownership
and modes. A shared group with inherited group-write permissions is suitable.
Rerun the installer after changing `vault.path`.

For the optional dedicated VM transport, enable `[vault]` before the first
vault directory is created, then run:

```bash
sudo /opt/openhands-symphony/scripts/install_syncthing.sh
```

This installs Syncthing from its official `stable-v2` APT channel as the
credential-free `symphony-sync` account. The new vault belongs to the shared
`symphony-vault` group; existing ownership is preserved and inaccessible
existing folders cause setup to stop. The GUI/API is loopback-only, and a
fresh installation disables discovery, relays, and router mapping. Pair the
existing Infra device explicitly at `tcp://192.168.0.110:22000`, reuse its
Obsidian folder ID, and choose send-and-receive with `ignorePerms = true` on
the VM copy so remote Unix modes cannot revoke the local shared-group grant.
Leave the Infra folder's existing device list and permissions intact.

The service is `symphony-syncthing.service`; its sensitive device keys and
database are under `/var/lib/symphony-sync`. Preserve that directory with
the VM recovery inputs. A fresh installation creates GUI user
`symphony-admin` with a generated password retained only in the root-readable
`/etc/openhands-symphony/syncthing-gui-password`. Use an SSH tunnel for the
loopback GUI and retrieve that password in your own authenticated VM terminal.
The Symphony installer restores its optional vault
group membership on updates. It also installs `InaccessiblePaths` overrides
for the agent, browser, and preview services, hiding source notes even if
their Unix modes later become more permissive. Restart the Symphony stack
after changing these overrides, and verify the installed service namespaces.

Verify before adding a real project:

```bash
sudo -iu openhands-symphony agentctl doctor
sudo -iu openhands-symphony agentctl reconcile
sudo -iu openhands-symphony agentctl status
```

The default preview port range is blocked from non-loopback ingress by the
VM firewall. If choosing another range, update the firewall accordingly.

## Start a project

Create any `.md` file anywhere under `1. Projects & Tasks`, for example
`Ideas/Dinner planner.md`:

```markdown
---
symphony: idea
tags: [project, cooking]
---
# Dinner planner

Let me enter ingredients and suggest a dinner I can cook in 30 minutes.
```

Only `symphony` is required. Existing YAML properties, lists, dates, and the
note's prose are accepted. `##` sections are optional; an internal `Project`
section is generated if none exist. Ordinary unmarked notes are ignored.

### Folder note, brief, checklist, and Waypoint

Use the folder's main note as the only project entry point. Put a brief and an
ordinary Markdown checklist above Waypoint. Linked subfiles need no special YAML:

```markdown
---
symphony: idea
---
# Dinner planner

Build a simple app for deciding what to cook. Keep everything local.

- [ ] [[Meal suggestions]]
- [ ] [[Shopping list]] — replace the original free-text list with checkboxes

%% Waypoint %%
```

A Markdown table also works: `| [ ] | [[Meal suggestions]] | Keep it simple |`.
Use one linked Markdown file per task row. Both `[[wiki links]]` (including
aliases) and `[normal links](Feature.md)` work. Paths may be project-relative,
vault-relative, or an unambiguous short wiki name. Linked files must stay inside
the main note's folder. Links to headings/blocks, other project notes, symlinks,
missing files, and ambiguous names pause the project with an actionable error.

Only explicitly listed files are included. Waypoint's generated index is kept
unchanged and excluded from the specification; it does not enroll every linked
note as work. Child links are not followed recursively. A main note without a
linked checklist still works as a single-file project.

An empty main note, including one containing only Waypoint, waits with an
actionable error in `_symphony/STATUS.md`. It does not create a repository or
start an agent with an empty brief. Add prose or an explicit checklist link
outside Waypoint; the next settled scan accepts the project automatically.

Editing the brief, a checklist row's instructions, or a listed subfile creates a
new combined specification after all those files settle. Reordering a Waypoint
index, changing its links, and ticking status boxes do not trigger a new build.
Changes or Syncthing conflicts in listed files also prevent an older run from
publishing stale work. A maximum of 100 subfiles and 2 MB of total input applies.

With `manage_checkboxes = true` (default), Symphony ticks listed files after a
published run passes setup, configured validation, and its preview checks. It
reopens an affected box when the file or its row instructions change. A failed,
question, or partially validated run does not tick pending work. Checking a box
means that version was processed successfully, not that every behavior has an
independent feature test. Initially checked entries are taken as your existing
completion notes; the worker receives them as context. Source prose, other
checkboxes, formatting, and the Waypoint index remain yours. Original bytes are
retained before each wrapper edit, and concurrent writes defer the update.

Checkboxes are status, not an approval queue or a retry button. Every listed
subfile is in scope; write optional future ideas elsewhere. To request a change,
edit the brief or subfile. With `manage_checkboxes = false`, Symphony still tracks
content changes but leaves checkbox characters to you. Removing a checklist row
stops tracking that file; explicitly describe any desired feature deletion.

The agent receives the current combined spec, its diff, previous progress,
checkbox state, and current code. The main brief and row instructions define
current intent. Explicit replacement instructions override the named older
behavior; unresolved contradictions request guidance in generated progress.
This is model-guided interpretation, not a deterministic semantic conflict detector.

Check a note without creating a repository, modifying files, or running a model:

```bash
agentctl vault-check '/obsidian/1. Projects & Tasks/Dinner planner/Dinner planner.md' --vault-root /obsidian
```

The prepared [homelab acceptance test](obsidian-test.md) includes a folder-note fixture.

After the quiet period Symphony:

1. Creates a private repository named from the note plus a stable suffix.
2. Adds its `repo: owner/name` property to the note, preserving the other
   properties and prose. It retains the original note in protected appdata
   before this one-time edit and defers if it sees a concurrent edit.
3. Generates `idea/SPEC.md`, a bootable initial runtime contract, a setup-script
   stub, and dependency ignores internally. You do not author those files.
4. Implements the note, runs setup/build, checks boot/health, captures previews,
   and publishes code directly to the repository's default branch.
5. Writes `_symphony/STATUS.md` and per-repository progress/screenshots under
   `_symphony/owner--repository/` in the vault. Syncthing returns these to your
   devices. Only the wrapper updates repository metadata and checklist status
   characters; the agent never rewrites your source notes.

An optional `repo: rafael-alani/existing-private-repo` selects an existing repo
or names a new one. A repository outside `vault.owner` is rejected. Two notes
cannot control the same repository. Retain the generated `repo` property when
renaming/moving a note so its repository identity remains stable.

## Switch workflows in either direction

Change just the YAML value:

| Value | Behavior |
|---|---|
| `symphony: idea` | Note changes drive direct prototype publication. GitHub issues are ignored. |
| `symphony: github` | Labeled GitHub issues drive draft PRs. Note content is retained but does not trigger code changes. |
| `symphony: paused` | Neither source starts new work. |

In-flight work finishes before the mode changes. New claims stop during the
transition, and both job types check the same durable routing state. Issues,
PRs, branches, code, and queued work remain available. Switching to GitHub
creates the routing labels, but does not manufacture issues or label them
ready. Switching back resumes note-driven work in the same repository.

Removing the marker or moving/deleting the note outside the configured subtree
pauses the project; it does not delete the repository. Invalid YAML, duplicate
routes, and unresolved Syncthing conflict files also pause intake. Resolve the
problem and the next reconciliation restores the requested mode.

The legacy `agentctl graduate` command performs explicit archival for older
Git-spec projects. It refuses note-managed projects and points to this
reversible switch instead.

## Permissions and dependencies

Claude and Codex implementation use full provider permission modes by default:
Claude `bypassPermissions` and Codex `agent-full-access`. This allows unattended
commands and networking within the dedicated VM worker account. These are
provider permissions, not root access to the hypervisor or homelab. Independent
review retains read-only/plan mode. An explicit `permission_mode = "restricted"`
restores the previous provider sandbox choices. Antigravity stays an optional,
restricted experimental provider.

The model maintains `.openhands/setup.sh` with reproducible dependency and build
steps (`npm ci`, `uv sync`, etc.). Symphony runs it after implementation, and
persistent preview deployment runs it again in a fresh release as the
credential-free preview account. That account has outbound networking and a
persistent home for package caches; it receives no provider/GitHub credentials.
Dependencies and build outputs can remain ignored by Git. Setup or health
failure retains the previous healthy preview. Preview commands bind
`127.0.0.1` and honor the supplied `PORT` or `{port}` argument.

Open the preview through an SSH forward using the port in `_symphony/STATUS.md`:

```bash
ssh -N -L 10000:127.0.0.1:10000 your-symphony-vm
```

Then open `http://127.0.0.1:10000` locally. Substitute the project's assigned port.

## Recovery and verification limits

Back up Symphony's SQLite state, configuration, original-note copies, and
credentials together with the existing application-state recovery inputs.
Syncthing propagates edits/deletions and is not an independent backup. Git
retains published specs and code; note `repo` properties allow re-registration
without creating a second repository. Preserve SQLite to retain job history
and assigned preview ports.

Tests cover discovery, repository bootstrapping, linked checklists and tables,
checkbox completion/reopening, source-edit races, missing
notes, conflicts, offline recovery, mode handover with active/queued work, and
network dependency preparation for a fresh preview. GitHub calls use fakes in
these tests. See the [homelab acceptance record](obsidian-test.md) for separate
live Syncthing, provider, GitHub, and browser evidence. Hackathon mode remains
unimplemented.
