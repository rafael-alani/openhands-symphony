# Configuration reference

The default path is `/etc/openhands-symphony/config.toml`; override it with `SYMPHONY_CONFIG` or `agentctl --config`.

## `[service]`

- `state_dir`: SQLite and durable orchestrator runtime state.
- `workspace_dir`: repository caches and per-run worktrees.
- `report_dir`: separate Markdown/JSON reports.
- `preview_dir`: immutable deployment handoff, stable releases, logs, and preview status; keep the installed `/var/lib/openhands-preview` default unless the preview systemd unit is overridden to match.
- `log_dir`: service logs.
- `listen_host`, `listen_port`: webhook/health listener; keep loopback.
- `webhook_secret_file`: GitHub HMAC secret. Never place the secret in TOML.
- `agent_server_url`: local Canvas ingress.
- `agent_server_api_key_file`: root-managed Canvas `LOCAL_BACKEND_API_KEY` environment file.
- `global_agent_instruction`: operator-editable suffix; intentionally empty by default.
- `validation_user`: credential-free local account for setup and quality gates; keep the installed `openhands-validator` default.

## `[github]`

- `allowed_repositories`: exact `owner/repository` list; may be empty when vault intake is enabled.
- `private_only`: defaults true.
- `auth_mode`: `gh` in this release; future GitHub App adapter slot.
- `generated_pr_label`: label applied to created PRs.
- `bot_login`: optional expected bot identity for operational auditing.

## `[ideas]`

- `repositories`: exact private `owner/repository` allowlist for spec-driven ideas runs.
- `private_only`: required to remain `true`; ideas runs may push only to private repositories.
- `spec_path`: user-owned spec path, default `idea/SPEC.md`.
- `progress_path`: agent-owned progress path, default `idea/PROGRESS.md`.

The ideas and Tier 1 allowlists must be disjoint. Only repositories in the effective Ideas allowlist (static configuration plus notes currently in Ideas mode) may receive direct default-branch publications; Tier 1 continues to publish generated branches and draft PRs. Both tiers share the scheduler's global and per-provider concurrency limits and repository leases.

Every successful ideas publication is archived from the exact pushed commit and handed to the credential-free preview service. The service reads `.symphony/idea.toml`, optionally runs the repository's configured `setup_script`, starts the app on its declared loopback port, and advances `last_good_preview_commit` only after health succeeds. Preview ports must therefore be unique across active ideas repositories and cannot change after the first healthy release. Preview commands should use an exact `{port}` argv placeholder or honor the supplied `HOST=127.0.0.1` and `PORT` environment variables; this lets the manager probe a candidate without stopping the last-good process.

## `[vault]`

- `enabled`: enable direct Syncthing note intake; false by default for existing installations.
- `path`: VM-local synced vault root, `/obsidian` by default.
- `manage_checkboxes`: default `true`; tick successful checklist files and reopen them on content changes. Set `false` to retain manual checkbox editing.
- `projects_dir`: relative note subtree, `1. Projects & Tasks` by default.
- `owner`: GitHub account/organization permitted for automatic private repositories.
- `provider`: enabled implementation provider used to bootstrap new project contracts.
- `quiet_seconds`: debounce after the latest main-note or listed-subfile write (default 30).
- `port_start`, `port_end`: dedicated stable preview port allocation range (10000–10999).

Each marked note authorizes its repository, so no per-project static allowlist
entry is needed. SQLite retains the note identity, effective/requested mode,
and preview port. Modes switch only after repository leases drain. Claims
check this durable routing state even if another service process has stale
configuration. Source-note edits are checked again before an Ideas publication.
See [the workflow guide](obsidian.md) for the YAML and Syncthing setup.

### Graduating a legacy Ideas repository

This archival operation is retained for old Git-spec installations. Note-managed projects switch reversibly through `symphony: github` and do not use graduation. Legacy graduation is deliberately two-step. First run the read-only preview as the
Symphony account and inspect every proposed issue:

```bash
sudo -iu openhands-symphony agentctl graduate owner/repository
```

The output ends with an approval ID and the exact apply command. Run that
command with `sudo` because applying graduation atomically replaces the
root-owned configuration and briefly stops/restarts the Symphony target:

```bash
sudo agentctl graduate owner/repository --approve <plan-id>
```

The approval is rejected if the spec, default branch, or configuration changed
since the preview. Apply creates idempotently marked, unrouted issues for only
the unfinished current sections, archives the active Ideas contract under
`archive/ideas/<spec-blob>/`, moves the repository between the disjoint
allowlists, retires outstanding Ideas runs, and removes it from the live-preview
allowlist. Add `agent:ready` and exactly one `agent:*` label only to the carried
issues you approve for Tier 1 work. Graduation is globally serialized across
repositories because every run edits the same configuration and service target.
Reused marked issues are reopened, refreshed to the approved text, and stripped
of intake labels. Before the archive commit advances the default branch, a
failure to retire work, refresh preview authorization, or replace configuration
aborts the operation and restores the prior local Ideas state.

On the vault machine, the next scheduled `ideasync sync` recognizes the
graduation archive and reports the repository as `retired` without recreating
`idea/SPEC.md`. Deregister it explicitly after reviewing that state; the vault
is preserved and the managed clone is moved to recoverable local storage:

```bash
ideasync remove owner/repository --dry-run
ideasync remove owner/repository
```

## `[scheduler]`

Polling/reconciliation intervals, lease/heartbeat durations, global concurrency, attempt/correction/review bounds, validation timeout, and backoff bounds. `[scheduler.provider_concurrency]` caps each provider independently. Setting a provider to zero makes it unavailable to claims.

## `[hack]`

Campaigns are disabled by default and require their own exact `repositories`
allowlist, including repositories created from vault notes. `provider` must be
enabled and have positive configured capacity. `max_parallel` permits 1–6 lanes;
`max_tasks` bounds discretionary model attempts, including board dispatch and
director-requested retries. The mandatory scaffold and final polish are bounded
separately. `reserve_slots` preserves at least one global slot for GitHub work.
Global capacity must exceed that reservation.

`task_timeout_seconds` bounds model turns, `fast_gate_timeout_seconds` bounds
setup/build checks, `polish_seconds` starts closing before the hard expiry, and
`milestone_every` controls screenshots. `publish_ideas` defaults false: a single
final draft PR is the default for either home mode. Setting it true permits only
a guarded final Ideas publication; GitHub home repositories still require a PR.
See [campaign operations](hack-operations.md) for the board and lane contract.

## `[providers.<name>]`

- `enabled`
- `adapter`
- `acp_command`: argv array for an ACP stdio server.
- `auth_command`: argv-only authentication probe.
- `auth_marker_file`: non-secret marker written only after the official worker-side probe succeeds.
- `timeout_seconds`
- `manual_command`
- `permission_mode`: `full` (default for Claude/Codex) or `restricted`; review sessions always select read-only/plan mode. Antigravity retains its restricted experimental adapter.

Commands are arrays on purpose; shell strings are accepted by the parser for convenience but arrays are recommended.

The supported adapter name is `openhands-acp`. Claude and Codex point at sanitized ACP wrappers. Antigravity points at the custom ACP bridge, which invokes the official `agy --print` command, but is disabled in the shipped example until a subscription-backed Ubuntu smoke run verifies it.

## `[repositories."owner/repo"]`

- `concurrency_scope`: `repository` by default; `configured` uses one explicit fixed key; `label` selects an allowlisted monorepo project.
- `concurrency_key`: required only for a fixed `configured` scope.
- `concurrency_labels`: in `label` mode, a map such as `{ "project:frontend" = "frontend" }`; exactly one mapped label is required and the resulting key is namespaced to the repository.
- `validation_commands`: optional operator-pinned argv arrays for all required format/lint/type/test/build gates.
- `setup_script`: optional repository-relative setup script; empty by default while architecture is being established.
- `instruction`: optional repository-specific suffix; empty by default.
- `approval_policy`: currently `safe-code-only`.

The shipped example leaves both fields empty. When no operator-pinned commands or repository gate exist, the implementation prompt tells the first suitable architecture issue to add a truthful, non-interactive `.openhands/quality-gate.sh` based on the actual project. The wrapper executes that proposed gate before pushing the bootstrap draft PR. If the agent cannot determine meaningful checks, it must request guidance; if it omits the gate, bounded correction runs and PR creation remains blocked. Once merged, the repository-owned gate becomes the default for subsequent issues. Operators can use `validation_commands` when they need immutable out-of-repository policy.

Setup and validation commands execute with a clean environment as `validation_user`, not as the GitHub-owning orchestrator or subscription-owning worker. They retain network access for normal dependency/test workflows but cannot read either credential home.

Repository-native `AGENTS.md`, `CLAUDE.md`, documentation, OpenHands skills, setup scripts, and hooks remain authoritative within the higher-level safety boundary.

For an ideas repository, the same section supplies advisory `validation_commands`, setup, and repository instructions. `concurrency_scope = "label"` is unavailable because ideas intake has no issue labels. The repository's `.symphony/idea.toml` selects the provider and defines the preview start argv, port, health path, and startup timeout.
