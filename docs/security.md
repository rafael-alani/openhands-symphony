# Security model

## Assumptions

The dedicated VM is trusted but disposable. Only private, operator-controlled, explicitly allowlisted repositories are accepted. Public repositories and untrusted public pull-request execution are disabled. A coding issue grants no production authority.

## Credential separation

Three unprivileged Unix identities create the main authority boundary:

- `openhands-symphony` owns SQLite, reports, webhook secret, GitHub login, commits, pushes, labels, comments, PRs, and reviews.
- `openhands-agent` owns Claude/Codex/Antigravity subscription login and Browser Use profiles, and runs Canvas plus all model subprocesses.
- `openhands-validator` has no GitHub or provider login and runs repository setup plus configured quality gates with an empty environment.
- `openhands-agents` grants the three identities access only to confined worktrees. A separate `openhands-operators` group lets only the orchestrator read non-secret provider auth-status markers. The Canvas key is narrower still: root-owned and group-readable only by `openhands-symphony`.

The worker has no orchestrator `gh` configuration and is not a member of the operator group that can read the Canvas API-key file. Provider ACP wrappers also remove GitHub, provider API-key, and Canvas-key environment variables before starting the model-facing process. Setup and validation cross a one-way sudo boundary into the lower-authority validator account and start through `env -i`; the orchestrator may run arbitrary commands only as that lower-authority identity. Its only root sudo command is the exact read-only nftables table listing used by `agentctl doctor`. Logs/reports redact token/key/password patterns and cap output.

Antigravity's Linux credential store uses a private D-Bus socket and GNOME Secret Service daemon under the worker UID; the socket is confined to `/run/openhands-agent`. Headless Chromium exposes CDP only on worker-local loopback port 9222. Browser Harness telemetry, cloud sync, cloud auto-spawn, and Browser Use/model API keys are absent from the service environment.

Canvas host mode is not adversarial multi-tenant isolation: Canvas and its ACP children share the worker UID because subscription credentials must be visible to those children. The Canvas localhost key is removed from the child environment, but a deliberately hostile same-UID process could inspect other same-user process state on a normally configured Linux host. Use only trusted private repositories and a disposable VM; stronger hostile-code isolation requires an additional container/VM boundary not claimed by this release.

A fine-grained PAT through `gh` is supported initially. Prefer a GitHub App with short-lived installation tokens for long-running use; GitHub authority remains behind the adapter.

## Injection and paths

Issue-derived strings never enter a shell. Repository names and issue numbers are validated, branches use a restricted generated slug, commands are argv arrays, and all setup/worktree paths must resolve beneath configured roots. Setup and quality gates come from administrator config or an allowlisted trusted repository and run without either credential set. Git metadata is group-read-only to agents; the wrapper verifies the worktree pointer/metadata owner before validation or commit, disables hooks for its commit, and pushes to a validated explicit `https://github.com/owner/repo.git` URL rather than trusting mutable `origin`. Intentional quality/stop hooks must be configured as explicit validation commands or `.openhands/quality-gate.sh`.

The task prompt treats issue/repository text as untrusted and denies GitHub use, deployment, credential work, and destructive migrations. The separate worker identity and wrapper-owned mutation are the enforcement boundary; the prompt is defense in depth.

## Approval boundary

`safe-code-only` authorizes reversible source edits and configured local validation. Destructive database migrations, deployments, infrastructure deletion, secret creation/rotation/export, destructive Git history changes, and auto-merge require explicit human policy. An agent must request guidance instead of proceeding.

## Exposure and subscription policy

Symphony and Chromium bind loopback. Canvas 1.4.0's frontend ingress binds a wildcard socket upstream, so the installed nftables table drops non-loopback traffic to 8000/8787/9222. Use SSH forwarding. Tailscale access requires an explicit, narrow firewall exception; do not remove the default table wholesale. If low-latency webhooks are needed, expose only the HMAC-verified webhook path through a narrowly scoped HTTPS ingress; polling works without public ingress.

Symphony never invents tasks, evades quotas, rotates identities to bypass provider limits, or silently changes providers. Backoff is durable and bounded.
