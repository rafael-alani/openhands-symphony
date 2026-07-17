# OpenHands capability spike

Test date: 2026-07-16.

## Pinned result

The locally launched Agent Canvas 1.4.0 stack reported Agent Server, SDK, tools, and workspace version 1.35.0; the Canvas automation backend is explicitly pinned to its tested 1.1.6 default. `scripts/openhands_capability_spike.py` used `scripts/fake_acp_agent.py` over real ACP stdio and real Agent Server REST.

Observed:

- `POST /api/conversations` accepts `agent_settings` selecting a custom ACP command.
- Two conversations ran concurrently with different session IDs and wrote only to their assigned local workspaces.
- The ACP subprocess could see the launching user's existing Codex credential file without receiving its contents in the request. The installed deployment therefore runs Canvas as the provider-only `openhands-agent` identity.
- `POST .../interrupt` moved a running conversation to `paused`.
- Posting a new `SendMessageRequest` with `run=true` to `.../events` resumed it to `finished`.
- Creating a conversation with `initial_message` starts the run; a redundant immediate `.../run` yields HTTP 409.
- Canvas's authenticated local REST mutation path requires `X-Session-API-Key`; Symphony reads the protected Canvas key file.
- The pinned OpenHands ACP client auto-approves ACP permission requests. Symphony therefore overrides the upstream provider defaults with Claude `acceptEdits` and Codex `agent`; the latter maps to Codex `workspace-write` with network disabled instead of `agent-full-access`.

The captured non-secret result is in `docs/evidence/openhands-capability-spike.json`.

## Canvas automation gap

The extracted Agent Canvas 1.4.0 `Automation` type has only `model?: string | null`, described as the LLM/model profile for automation runs. It has no Agent Profile or ACP profile selector. By contrast, the same bundle exposes Agent Profile REST/UI support and Agent Server conversation creation accepts `agent_profile_id` or `agent_settings`.

Decision: Canvas remains the UI and agent server; Symphony schedules through Agent Server REST. This is a small companion adapter, not a Canvas fork.

## Upgrade gate

On any Canvas, Agent Server, ACP wrapper, Claude, or Codex version change:

1. launch the pinned Canvas stack;
2. run the deterministic spike;
3. verify provider auth as `openhands-agent` and GitHub auth as `openhands-symphony`;
4. run repository tests;
5. verify Antigravity `--print`/sandbox flags if its version changed;
6. only then update `versions.env`.

The same date's Antigravity interface spike downloaded the official 1.1.3 macOS arm64 artifact, verified Google's manifest SHA-512, and observed `--print`, `--conversation`, `--print-timeout`, `--sandbox`, and `--mode`. It did not consume a model turn. Linux amd64/arm64 URLs and hashes are pinned separately in `versions.env`.
