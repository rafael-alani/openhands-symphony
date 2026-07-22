# Canvas operator guide

Canvas supplies the browser UI and the local OpenHands Agent Server. Opening its UI is optional; Symphony uses the Agent Server whether or not an operator has Canvas open in a browser. Canvas is not Symphony's scheduler or source of truth: GitHub issues, labels, comments, and draft PRs remain the control and result surfaces.

## What to choose on first launch

Choose **Codex** if `agentctl auth codex` has passed. That is the recommended manual-chat default for the documented setup. Choose **Claude Code** instead if you prefer it and `agentctl auth claude` has passed. Both run as `openhands-agent` and can use that account's existing provider login.

The picker controls only the default agent for conversations started manually in Canvas, and Canvas lets you change it later. It does not affect a Symphony-created issue run. Symphony creates each conversation through Agent Server REST with explicit ACP settings derived from the issue label:

| GitHub label | Symphony conversation agent |
|---|---|
| `agent:claude` | Claude Code through ACP |
| `agent:codex` | Codex through ACP |
| `agent:antigravity` | the custom `agy` ACP bridge, if explicitly enabled |

The other first-run choices are not useful defaults for this installation:

- **OpenHands** is the general OpenHands agent. The repository intentionally ships without model API-key variables, so select it only after separately configuring and accepting a supported model's credential and billing model.
- **Gemini CLI** is Google's Gemini coding agent. It is not the Antigravity `agy` CLI and is not authenticated by `agentctl auth antigravity`.

## What the web interface is useful for

Canvas has two practical uses on this VM:

1. **Observe Symphony conversations.** Open a conversation to follow agent messages and the activity/history exposed by Canvas while an issue job runs.
2. **Run an ad-hoc manual coding conversation.** Start a new conversation, select Codex or Claude Code, point it at a workspace the `openhands-agent` service can access, and prompt it directly.

Manual conversations are deliberately outside Symphony's safety workflow. They do not claim a GitHub issue, acquire a Symphony lease, run the configured credential-free quality gate, update the canonical issue comment, or use Symphony's guarded commit/push/draft-PR path. The Canvas worker also does not have the orchestrator account's GitHub credentials. Treat a manual conversation as direct local work that you must review and finish yourself.

For a Symphony-managed job, use Canvas primarily as a viewer. Sending extra messages, cancelling, or resuming it in Canvas can make the visible conversation diverge from Symphony's durable job state. Control the job through the GitHub issue instead:

- select the implementation provider with exactly one `agent:*` label;
- start eligibility with `agent:ready`;
- use `/agent pause`, `/agent resume`, `/agent retry`, or `/agent cancel` in an issue comment;
- review the canonical status comment and resulting draft PR in GitHub.

Canvas upstream also exposes automation features, but this installation does not use them for Symphony. Pinned Canvas 1.4.0 automations cannot select the required ACP agent profile, so creating a second Canvas schedule or trigger would duplicate Symphony's webhook/reconciliation scheduler without preserving its routing and safety guarantees.

## Access

Keep the SSH session open:

```bash
ssh -N -L 8000:127.0.0.1:8000 your-vm
```

Then open `http://127.0.0.1:8000` on the computer running that SSH command. The browser connects to local port 8000, and SSH carries the connection to port 8000 on the VM. If the browser reports a refusal, verify the remote stack before changing firewall rules:

```bash
sudo -iu openhands-symphony agentctl status
sudo -iu openhands-symphony agentctl doctor
sudo systemctl status openhands-canvas.service --no-pager
```

Do not expose Canvas directly to the public internet.
