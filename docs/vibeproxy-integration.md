# VibeProxy integration proposal

**Status:** Platform rechecked 2026-09-20; integration remains blocked and disabled

**Default posture:** Optional, disabled, and fail-closed

**Decision requested:** Add VibeProxy as a supported model-transport integration once the platform, policy, and isolation gates below pass.

## Recommendation

The 2026-09-20 completion audit rechecked the official repository and installation
guide. The supported application still requires macOS 13 or later; the README
now offers both Apple Silicon and an explicitly untested Intel build. There is
no documented Ubuntu VibeProxy application installation in those sources.
The separately named CLIProxyAPIPlus backend is not an accepted Symphony
provider merely because VibeProxy bundles it. Shipping it would require the
packaging, route policy, and agent-lifecycle acceptance below. No proxy accounts,
model routes, or replacement provider were enabled by the four-phase audit.

Sources checked: [upstream README](https://github.com/automazeio/vibeproxy),
[upstream installation](https://github.com/automazeio/vibeproxy/blob/main/INSTALLATION.md).

Symphony should add an optional VibeProxy lane alongside its existing ACP providers. VibeProxy presents subscription-backed models through API-compatible endpoints and centralizes authentication, token refresh, model routing, and provider status. That could let Symphony and Agent Canvas reach a broader set of models without adding a separate API-key integration for each one.

This should extend the current design, not replace it. Claude Code, Codex, and Antigravity should continue to use their existing ACP paths for production work until a VibeProxy-backed OpenHands agent proves equivalent lifecycle, tool-use, sandbox, cancellation, and recovery behavior.

```text
                                      ┌─> Claude/Codex/Antigravity ACP
GitHub -> Symphony -> Agent Server ───┤
                                      └─> OpenHands agent -> VibeProxy -> selected model backend
```

The first useful release would make VibeProxy available for explicitly selected manual or experimental runs. Promotion to implementation or independent-review work should require the same evidence currently demanded of every provider.

## Why this pushes Symphony further

- **More model choice behind one local boundary.** VibeProxy advertises Claude Code, ChatGPT/Codex, Gemini, Antigravity, GitHub Copilot, Qwen, Kimi, and Z.AI routes through compatible APIs.
- **A path for non-ACP Agent Canvas profiles.** A VibeProxy-backed model profile could power the OpenHands agent while Canvas continues to own the conversation and tool loop.
- **Centralized provider operations.** Authentication state, token refresh, model discovery, and upstream availability can be observed at one integration boundary instead of being reimplemented for every API-shaped provider.
- **Subscription-first experimentation.** It fits Symphony's preference for reusing an operator's existing subscriptions, while keeping API-key-backed routes optional.
- **Room for specialized roles.** Once verified, operator-defined aliases such as `vibeproxy-fast` or `vibeproxy-deep` could support bounded planning, implementation, or review experiments without allowing issue prose to choose arbitrary model IDs.

## Guardrails that must remain true

VibeProxy must not weaken Symphony's existing guarantees:

- no silent provider or model substitution;
- no automatic account rotation intended to evade a quota or rate limit;
- no provider credentials in prompts, worktrees, GitHub comments, or validation processes;
- no public listener—the proxy binds to worker-local loopback only;
- no unpinned self-update in the production VM;
- no mutation based on stale GitHub state;
- no auto-merge;
- no claim of independent review unless the execution layer enforces a genuinely non-mutating mode.

Initial support should therefore allow only one explicitly authorized account per provider route. Upstream multi-account round-robin and failover must remain off unless a later policy decision defines a compliant use case. A quota or authentication failure should become a durable, visible Symphony state rather than selecting another account or backend.

## Important constraints

### The official application does not match the production host yet

The official VibeProxy project currently documents a native macOS application (Apple Silicon and an untested Intel build). Symphony targets an Ubuntu VM. We should not make an unreviewed community Linux fork part of the trusted production path merely to bridge that gap.

The capability spike must first establish one of these deployable options:

1. an upstream-supported, headless Linux VibeProxy distribution;
2. an upstream-documented way to run its bundled backend on Linux with equivalent behavior; or
3. a separately reviewed packaging plan that preserves VibeProxy compatibility without importing its macOS UI assumptions.

Until one exists, a macOS VibeProxy instance reached through a private tunnel may be useful for a disposable experiment, but it is not the recommended production architecture.

### A model proxy is not an agent adapter

VibeProxy translates and routes model API traffic. Symphony's provider contract controls a complete agent lifecycle: start, resume, cancel, wait, capabilities, authentication, and quota state. The integration therefore needs an agent runtime—most naturally the OpenHands agent in Agent Server—in front of VibeProxy. Pointing the existing ACP adapter at port 8317 would not be sufficient.

### Subscription use needs an explicit policy decision

VibeProxy's own setup guide warns that proxying subscription credentials may conflict with provider terms and may create account risk. That warning is material, not boilerplate. Installation must be opt-in, must name the enabled upstream routes, and must require the operator to accept responsibility for the applicable provider terms. Symphony should make no claim that a technically working route is provider-sanctioned.

## Proposed delivery

### Phase 0: platform and policy spike

- Verify a maintainable Ubuntu deployment path and record exact upstream version, artifacts, checksums, license, and transitive backend version.
- Disable automatic updates and prove rollback to the last known-good package.
- Document the terms-of-service decision separately for every enabled route.
- Confirm that a single account can be selected without round-robin or hidden fallback.
- Identify the credential files and prove that only `openhands-agent` can read them.

Exit criterion: `agentctl doctor` can report the proxy version, bind address, authentication status, enabled routes, and update policy without printing secrets.

### Phase 1: isolated Canvas experiment

- Run VibeProxy as `openhands-agent` in a hardened systemd unit bound to `127.0.0.1`.
- Add an explicit Agent Canvas model profile that targets the local compatible endpoint using a non-secret placeholder key only if the client requires one.
- Test model discovery, streaming, tool calls, long-running turns, cancellation, malformed responses, authentication expiry, quota exhaustion, and proxy restart.
- Confirm that logs and error reports redact credentials and response headers.
- Keep this phase manual/experimental; do not add GitHub routing labels yet.

Exit criterion: a disposable private repository completes a tool-using Canvas conversation and survives proxy restart without gaining GitHub authority or escaping the assigned worktree.

### Phase 2: Symphony provider integration

- Add a distinct Agent Server adapter for VibeProxy-backed OpenHands profiles; do not overload `openhands-acp`.
- Configure stable operator-owned route aliases. Labels select aliases, never raw model names or arbitrary base URLs.
- Implement `auth_status`, `health`, `start`, `cancel`, `wait`, and quota/rate-limit classification with fail-closed behavior.
- Preserve provider/model provenance in reports and draft PRs.
- Add deterministic fake-proxy tests plus a subscription-backed Ubuntu smoke test.
- Keep implementation and review capabilities false until their respective sandbox and non-mutation tests pass.

Exit criterion: the provider support matrix can truthfully mark each lifecycle capability as verified, and the end-to-end smoke artifacts demonstrate no fallback, credential leakage, or duplicate mutation.

### Phase 3: deliberate expansion

Only after Phase 2 should Symphony consider additional model aliases, Browser Use/OpenHands model profiles, or specialized planning/review roles. Each addition remains independently allowlisted and capacity-limited. Multi-account behavior requires a new recorded decision rather than arriving as an upstream default.

## Acceptance criteria

The integration is ready to ship only when all of the following are true:

- an Ubuntu-compatible runtime is pinned and reproducibly installed;
- the service listens only on loopback and is covered by Symphony's firewall and systemd checks;
- credentials are worker-owned, absent from environment dumps, and excluded from reports;
- enabled routes and model aliases are explicit configuration;
- authentication, quota, transport, model, validation, and implementation failures remain distinguishable;
- cancellation and restart recovery cannot leave a second active writer;
- no fallback or account rotation occurs unless a future recorded decision explicitly enables it;
- the fake test suite and a disposable subscription-backed smoke run pass;
- documentation states the provider-policy risk and the fact that the integration is optional.

## Sources and evidence to re-check during the spike

- [VibeProxy repository and feature overview](https://github.com/automazeio/vibeproxy)
- [VibeProxy Factory integration guide](https://github.com/automazeio/vibeproxy/blob/main/FACTORY_SETUP.md)
- [VibeProxy installation requirements](https://github.com/automazeio/vibeproxy/blob/main/INSTALLATION.md)

These are fast-moving upstream sources. The spike must capture observed versions and behavior rather than treating this proposal as compatibility evidence.
