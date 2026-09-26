# Reasoning and speed

Symphony's Codex jobs default to **Extra high** (`xhigh`) reasoning and
**Normal** speed. This also applies to existing installations whose Symphony
configuration predates these fields. Choose reasoning and speed independently:

| Setting | Choices | Default |
| --- | --- | --- |
| `reasoning_effort` | `low`, `medium`, `high`, `xhigh` (Extra high) | `xhigh` |
| `speed` | `normal`, `fast` | `normal` |

Fast mode uses more subscription allowance and requires a compatible model and
account. The model itself is still selected by the worker's Codex configuration.
These controls currently apply to Codex; explicit choices for another provider
fail clearly instead of being silently discarded. Other providers retain their
native defaults.

## Choose in Obsidian

Add optional properties to the **main project note** in Ideas mode:

```yaml
---
symphony: idea
reasoning_effort: high
speed: fast
---
```

Omit either property to inherit its default. Most of the time, keep both absent
for extra-high/normal. The properties work for a single note and a folder note
with a linked checklist. They are compiled into the accepted spec, retained in
the run snapshot, and changing them creates a new spec revision. Existing
supersession/publication guards apply. Child checklist files do not independently
select settings. GitHub mode uses issue labels below.

`agentctl vault-check /path/to/Project.md` validates and displays the overrides
without launching work.

## Choose for a GitHub issue

Keep `agent:ready` and `agent:codex`. Optionally select one label from each group:

- `reasoning:low`, `reasoning:medium`, `reasoning:high`, `reasoning:xhigh`
- `speed:normal`, `speed:fast`

Omission inherits the configured defaults. Conflicting or unknown choices block
intake. Labels are captured in the durable issue snapshot; changing settings
after a claim requires the normal explicit resume/retry flow. Implementation
and repair use the issue choices. Independent review uses its own provider's
defaults and keeps its read-only permission mode.

`agentctl labels` installs these labels in configured repositories. For existing
vault-managed GitHub projects, run it after upgrading to make the choices appear
in GitHub's label picker.

## Configure defaults

In `/etc/openhands-symphony/config.toml`:

```toml
[providers.codex]
# Keep the existing enabled/adapter/command/auth fields in this table.
reasoning_effort = "xhigh"
speed = "normal"

[repositories."owner/repository"]
# Optional defaults for this repository's Codex work.
reasoning_effort = "high"
speed = "normal"
```

For a Git-managed Ideas project, the top level of `.symphony/idea.toml` also
accepts these two fields, before `[preview]`. They apply to subsequent spec
runs; a runtime-only edit does not itself request implementation. Do not remove
the existing provider and preview configuration.

Precedence, highest first: accepted note/spec or issue choices → Ideas runtime
defaults → repository defaults → provider defaults → built-in extra-high/normal.
Each field inherits independently. Resume within the same provider conversation
keeps that conversation's settings; changing deployment defaults does not rewrite
an in-flight conversation. Restart Symphony after editing its configuration.

Inspect effective configured defaults without running a model:

```bash
sudo -iu openhands-symphony agentctl settings
sudo -iu openhands-symphony agentctl settings --repository owner/repository
curl -fsS http://127.0.0.1:8787/agent-settings
```

The HTTP endpoint is loopback-only and reports no credentials. Each new Canvas
conversation also records effective `reasoningeffort` and `speed` tags (the
Agent Server requires alphanumeric tag keys).
Manual chats created directly in Canvas continue to use Canvas's own controls;
Symphony's defaults apply to work it schedules.

## Transport and pinned compatibility

Symphony sends `model_reasoning_effort` and `service_tier` through the supported
`acp_env.CODEX_CONFIG` field of each conversation. Normal explicitly supplies
JSON `null` to clear an inherited fast tier; fast supplies `fast`. No provider
credential or worker-global configuration file is rewritten.

Codex CLI 0.144.4 reports a requested fast tier as `priority`. The pinned
`codex-acp` 1.1.4 checks only the newer `fast` spelling and otherwise turns it
off. `scripts/patch_codex_acp.py` recognizes both aliases when creating/loading
sessions and rejects a fast prompt for a model without fast support. The patch
checks the exact version and expected source locations, is idempotent, and fails
before writing if the upstream shape differs. Both installation and the focused
application updater apply it. Reassess/remove it when upgrading those pins.

Run `scripts/probe_agent_settings.py` as the authenticated `openhands-agent`
worker. It checks the real adapter using temporary workspaces and new sessions,
reusing the worker's normal login without copying or printing credentials. It
makes no model turns. It verifies extra-high/normal and low/fast with the wire
alias fix. This proves configuration propagation,
not the account's available quota or a guaranteed latency improvement.

Official setting semantics: [Codex configuration reference](https://developers.openai.com/codex/config-reference/)
and [Codex speed](https://developers.openai.com/codex/speed/).

## Focused application deployment

This release is based on deployed revision `36992f3` and preserves database
schema 8. The separate, undeployed Hack-campaign work remains on the development
branch and is not required for these settings. The earlier staged release
included that work; its schema mismatch correctly stopped installation before
any service or data change.

For this update, build the wheel in `dist/` from the reviewed Git checkout and
run `sudo python3 scripts/update_application.py --expected-commit <full SHA>`
from that checkout on the Symphony VM. The updater refuses runtime dependency,
version-pin, or database-schema changes and requires idle Symphony leases.
Its `--check` mode verifies software compatibility against the installed source
and runtime and validates the wheel without sudo or changing services. The full
update checks authenticated adapter settings before stopping the service.
It briefly stops only Symphony and its reconciliation timer, retains old source,
runtime, configuration, adapter, and an integrity-checked SQLite copy under
`/root/symphony-settings-rollback-<timestamp>`, installs and byte-checks the wheel,
sets extra-high/normal defaults, verifies the authenticated adapter without
model turns, records the release checkout for future `agentctl update` runs,
and checks the running defaults endpoint. On failure it restores
the previous software and configuration before restarting the prior services.

The rollback directory remains root-only. This task-specific local copy is not
a VM101 backup or off-host recovery test. Keep the previous checkout and rollback
until a separate cleanup is authorized. The usual full installer also contains
the compatibility patch for future rebuilds.
