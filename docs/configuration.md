# Configuration reference

The default path is `/etc/openhands-symphony/config.toml`; override it with `SYMPHONY_CONFIG` or `agentctl --config`.

## `[service]`

- `state_dir`: SQLite and durable orchestrator runtime state.
- `workspace_dir`: repository caches and per-run worktrees.
- `report_dir`: separate Markdown/JSON reports.
- `log_dir`: service logs.
- `listen_host`, `listen_port`: webhook/health listener; keep loopback.
- `webhook_secret_file`: GitHub HMAC secret. Never place the secret in TOML.
- `agent_server_url`: local Canvas ingress.
- `agent_server_api_key_file`: root-managed Canvas `LOCAL_BACKEND_API_KEY` environment file.
- `global_agent_instruction`: operator-editable suffix; intentionally empty by default.
- `validation_user`: credential-free local account for setup and quality gates; keep the installed `openhands-validator` default.

## `[github]`

- `allowed_repositories`: required exact `owner/repository` list.
- `private_only`: defaults true.
- `auth_mode`: `gh` in this release; future GitHub App adapter slot.
- `generated_pr_label`: label applied to created PRs.
- `bot_login`: optional expected bot identity for operational auditing.

## `[scheduler]`

Polling/reconciliation intervals, lease/heartbeat durations, global concurrency, attempt/correction/review bounds, validation timeout, and backoff bounds. `[scheduler.provider_concurrency]` caps each provider independently. Setting a provider to zero makes it unavailable to claims.

## `[providers.<name>]`

- `enabled`
- `adapter`
- `acp_command`: argv array for an ACP stdio server.
- `auth_command`: argv-only authentication probe.
- `auth_marker_file`: non-secret marker written only after the official worker-side probe succeeds.
- `timeout_seconds`
- `manual_command`

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
