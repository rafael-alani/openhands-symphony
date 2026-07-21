# Provider adapter contract

Every adapter exposes `auth_status`, `health`, `start`, `resume`, `cancel`, `wait`, capability flags, and `quota_or_rate_limit_state`. Authentication, quota/rate-limit, provider transport/tool, validation, and implementation failures remain distinct durable phases.

## Pinned support matrix

| Provider | Subscription authentication | Autonomous execution | ACP | Resume | Independent review | Known limitation |
|---|---|---|---|---|---|---|
| Claude Code 2.1.205 | Supported by official interactive Claude Pro/Max login; clean-VM login pending | Implemented; subscription-backed smoke pending | `claude-agent-acp` 0.59.0 | Agent Server cancel/resume proved with a fake ACP process; provider smoke pending | Implemented; GitHub smoke pending | OAuth is interactive; re-spike wrapper compatibility on upgrades |
| Codex CLI 0.144.4 | Supported by official `codex login --device-auth`; clean-VM ChatGPT login verified 2026-07-21 | Implemented; subscription-backed autonomous smoke pending | `@agentclientprotocol/codex-acp` 1.1.4 | Agent Server cancel/resume proved with a fake ACP process; provider smoke pending | Implemented; GitHub smoke pending | No OpenAI API key is configured; a real autonomous model-turn smoke is still required |
| Antigravity CLI 1.1.3 | Official Google OAuth/keyring flow documented; clean-VM login pending | **Disabled by default; not yet verified** | Implemented Symphony bridge over ACP 0.11.0 and official `agy --print` | Subsequent/reloaded ACP turns use workspace-scoped `agy --continue`; no stable native conversation ID is exposed | Implemented but unverified | Headless flags/checksum were spiked without a model turn; no subscription-backed Ubuntu run has passed |

Claude and Codex use ACP because it keeps the Canvas conversation lifecycle. Antigravity now has an official non-interactive `--print` command; `scripts/antigravity_acp_bridge.py` translates ACP prompts to that command without terminal emulation or screen scraping. It runs `--sandbox`, disables the built-in self-updater, and never falls back to a different provider.

Symphony explicitly selects Claude's `acceptEdits` mode and Codex's `agent` mode for implementation. OpenHands' ACP bridge answers individual permission requests, so the agents remain unattended; Codex stays in its `workspace-write` sandbox with network disabled by that mode. Independent review uses Claude/Antigravity `plan` or Codex `read-only`. Symphony never selects Claude `bypassPermissions` or Codex `agent-full-access`. The Codex wrapper also pins `CODEX_PATH` to the separately installed, version-checked official CLI and disables browser-based login inside headless jobs.

The Antigravity 1.1.3 binary, checksum, and headless flags were verified locally on 2026-07-16 without consuming a model turn. On 2026-07-21 its official x86_64 binary failed fast on a Proxmox VM whose CPU profile did not expose PCLMULQDQ. This is not evidence that autonomous subscription execution works. The shipped example therefore sets `providers.antigravity.enabled = false`; enable it only after the VM exposes the required CPU features and `agentctl auth antigravity`, `agentctl doctor`, and a disposable subscription-backed Ubuntu smoke run all pass.

## Authentication and process identity

Run GitHub authentication as `openhands-symphony`. Run all provider authentication as `openhands-agent`. `agentctl auth` follows the interactive login with the provider's official status/list command and writes a non-secret verification marker only after that command exits successfully.

Agent Canvas and ACP subprocesses run as `openhands-agent`, so they inherit provider subscription storage but not the orchestrator user's `gh` configuration. The orchestrator reads the protected Canvas localhost key, but prompt text, reports, and GitHub comments never receive it. API-key environment variables are absent by default.

Independent review always starts a fresh provider process and conversation. The wrapper posts a real GitHub `COMMENT` review because GitHub rejects approve/request-changes reviews from the same identity that opened the PR. The review body preserves the recommendation and categorized findings; substantive findings still trigger the bounded repair loop.
