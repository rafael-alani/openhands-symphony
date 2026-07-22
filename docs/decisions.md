# Decisions

## Companion, not fork

Agent Canvas automations cannot select ACP Agent Profiles in 1.4.0. Use Canvas unchanged as UI/server and put provider routing behind a small Agent Server adapter. This keeps upstream upgrades possible.

## GitHub is truth; SQLite is coordination

Labels and live issue/PR state decide whether mutation is legal. SQLite supplies durable attempts, locks, leases, artifact IDs, and evidence. It never permits a stale claim to override live GitHub.

## One durable job per issue

A unique repository/issue row coalesces webhooks, polls, and restarts. Attempts live inside that row; human-readable reports capture the audit trail. Existing exact PRs are adopted; orphan branches stop for guidance.

Expired leases are not blindly requeued. Reconciliation first cancels the exact durable OpenHands implementation, reviewer, or repair conversation. A failed cancellation stops for guidance; a successfully interrupted review can be explicitly or automatically retried against the existing PR without recreating it.

Lease heartbeats cover every claimed phase rather than only model execution, because repository setup and validation can outlast a lease on their own. External code mutations also require a synchronous successful renewal. Cross-process status updates take an expiring SQLite operation lock so exact-item intake and scheduled reconciliation cannot both create the canonical comment.

Monorepo parallelism is explicit rather than inferred from paths or issue prose. A repository may map operator-created labels such as `project:frontend` to allowlisted scope keys; exactly one mapped label is required, and each resulting key remains namespaced to that repository.

## Repository default concurrency key

One implementation per repository is safe and understandable for a solo developer. A configured stable key supports monorepo project scopes. Review holds or safely releases the same write lease; it never permits two implementation writers.

## Wrapper-owned GitHub mutation

Agents edit only worktrees. The wrapper performs comments, labels, commits, pushes, PR creation, and reviews. This prevents prompt content from becoming GitHub authority and makes last-moment revalidation enforceable.

## Proof is mandatory

No passing validation evidence means no PR. Operators may pin commands in service configuration; otherwise the first suitable architecture issue proposes a repository-owned `.openhands/quality-gate.sh` based on the actual toolchain. The credential-free wrapper runs that gate before pushing even the bootstrap draft PR, records exact commands/exit/output, and permits only a bounded correction. Provider prose is never treated as test evidence, and a human still decides whether to merge the proposed bootstrap gate.

A validation-policy-only diff cannot satisfy an unrelated implementation issue. Before push, the wrapper requires at least one changed path beyond `.openhands/quality-gate.sh` unless the accepted issue explicitly requests that gate. Agent Server completion also fails closed unless the final response contains the structured Symphony result record; response envelopes are unwrapped before parsing rather than copied into PR summaries.

## Subscription-first providers

Claude and Codex are wired to reuse worker-owned subscription login through ACP. Antigravity 1.1.5 documents `agy --print`; a minimal custom ACP bridge invokes that official headless mode. There is no screen scraping, API-key fallback, or provider substitution. Native Antigravity conversation resume is not claimed because print mode exposes no stable machine-readable conversation ID. Antigravity is disabled in the example until a subscription-backed Ubuntu smoke run proves the full path.

## Unattended does not mean full access

OpenHands 1.35.0 auto-approves ACP permission requests, but its built-in provider defaults select Claude `bypassPermissions` and Codex `agent-full-access`. Symphony explicitly selects Claude `acceptEdits` and Codex `agent`; Codex therefore remains under its workspace-write, network-disabled sandbox. Agent Canvas itself runs as the credential-only worker account with a strict systemd filesystem boundary and cannot read the orchestrator's GitHub credentials.

Independent reviewers receive a fresh conversation in a provider-enforced non-mutating mode: Claude and Antigravity `plan`, or Codex `read-only`. The review prompt also forbids edits and GitHub actions. Repair passes are separate implementation-mode processes and remain bounded.

A review is never considered clean when the process omits its structured recommendation and findings count. Such output leaves the PR intact in a retryable review state. Manual retry adopts the existing branch/PR and starts a fresh reviewer instead of reimplementing or duplicating artifacts.

## Split GitHub and provider identities

`openhands-symphony` owns GitHub credentials and mutations; `openhands-agent` owns provider subscriptions and model processes. A narrow shared group exposes only worktrees and non-secret auth markers. This makes wrapper-owned mutation an operating-system boundary instead of prompt policy alone.

Repository setup and validation use a third `openhands-validator` identity with an empty environment and neither credential home. The orchestrator's sudo authorization can move down to this lower-authority account; its only root command is the exact read-only nftables table probe used by `agentctl doctor`. Agent-facing Git metadata is read-only; privileged Git operations verify it and use an explicit validated GitHub remote.

The Canvas API key is root-owned and readable only by the orchestrator service group. Systemd reads it into Canvas before dropping privileges, while the `openhands-agent` account is deliberately excluded from that group; model-facing wrappers additionally delete the environment variable.

## Comment reviews from one bot identity

GitHub forbids a PR author from approving or requesting changes on its own PR. The wrapper therefore posts the independent result as a real COMMENT review, records the reviewer's recommendation separately, and uses substantive findings—not the GitHub event type—to drive bounded repairs.

After every repair validation and every newly observed CI state, the orchestrator replaces only the generated PR body's Validation section. This keeps command outcomes and current CI evidence attached to the PR without overwriting the implementation summary or risks.

The PR body shows validation evidence from the current implementation attempt. The VM run report retains all earlier attempts and failures for audit history.

## Browser is a tool, not a second model

Browser Use's local CLI is controlled by the selected coding agent. Its current local MCP server requires a separate model API key, so Symphony does not register it by default. GitHub MCP is also omitted because the credential-less worker cannot mutate GitHub and the guarded wrapper/`gh` path is safer. Agent Canvas supports MCP for non-ACP profiles; OpenHands' ACP Agent delegates tools to the provider and rejects `mcp_config`, so any operator-added server for these workers belongs in the provider's own configuration. Cloud/autonomous Browser Use is disabled to avoid hidden API billing. Hard domain controls belong at VM egress.

## No auto-merge

Draft PR plus human review is the terminal production output. Even a clean independent review leaves the PR open.

## Empty global harness

`global_agent_instruction` and per-repository `instruction` default empty. Safety/task boundaries are generated by the wrapper; speculative provider prompt duplication is not shipped.

## No ClawSweeper dependency

Only the requested design patterns were reproduced. No ClawSweeper code or repository content was cloned, indexed, loaded, or vendored.
