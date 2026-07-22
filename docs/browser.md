# Browser tooling

Browser Use 0.13.4 plus Browser Harness 0.1.5 is installed at `/opt/browser-use`. Playwright 1.61.0 installs Chrome for Testing 149.0.7827.55 at a pinned shared path. `openhands-browser.service` runs that browser headlessly as `openhands-agent`, with CDP bound only to `127.0.0.1:9222`; Canvas provider processes receive `BU_CDP_URL` for direct control.

The direct interface was locally spiked on 2026-07-16: the pinned CLI connected to the pinned loopback CDP browser with telemetry/cloud disabled, opened `https://example.com`, and returned its page URL/title without a Browser Use or model API key. The non-secret observation is retained in `docs/evidence/browser-capability-spike.json`. Ubuntu systemd/browser-sandbox acceptance remains part of the clean-VM gate.

## Recommended mode

The selected coding agent invokes the local `browser-harness` CLI installed alongside Browser Use. Browser Harness supplies direct CDP control, while Claude, Codex, or Antigravity remains the reasoning model. This local direct-browser mode does not require `BROWSER_USE_API_KEY`. Browser Use Cloud/hosted agents do require separate credentials and are not configured.

| Integration | Claude | Codex | Antigravity | Extra model API key |
|---|---|---|---|---|
| Local `browser-harness` direct CDP CLI | Supported | Supported | Supported through the headless bridge | No |
| Browser Use local MCP server | Not configured | Not configured | Not configured | **Yes, for the MCP server's reasoning model** |
| Browser Use Cloud/hosted agent | Disabled | Disabled | Disabled | Yes |

CLI-first is the default because all three workers already have shell tool access. The installed 0.13.4 CLI delegates its direct interface to Browser Harness and accepts Python helper calls on stdin; Symphony sets both `BROWSER_HARNESS_HOME` and `BU_CDP_URL`. Its packaged upstream skill was evaluated but is not globally installed because it recommends optional cloud/browser-key flows that are outside this subscription-only baseline. The generated task prompt exposes only the local mode and forbids cloud, API keys, credential export, and tunnels.

The exact local pattern exposed to workers is:

```bash
browser-harness <<'PY'
new_tab("https://example.com")
wait_for_load()
print(page_info())
PY
```

Helpers are pre-imported by Browser Harness. Browser Use cloud authentication, `start_remote_daemon`, cloud/profile sync, and remote browser IDs are outside the supported baseline. The separately installed `browser-use` CLI remains available for diagnostics and explicit opt-in use, but Symphony does not give its autonomous/cloud-agent modes any API key.

Browser Use's current local MCP documentation requires an `OPENAI_API_KEY` (or another supported model API key), so enabling that server would silently defeat the subscription-only design. Agent Canvas/OpenHands remains MCP-capable for standard OpenHands agents, but the pinned OpenHands ACP Agent contract delegates tools to the provider and explicitly does not accept `mcp_config`. Symphony therefore ships no MCP registration: GitHub mutations are more safely handled by the wrapper and `gh`, while browser control stays inside the selected subscription-authenticated coding agent. An operator may opt into a provider-side MCP server separately after accepting its credential and billing model; Symphony never injects such keys.

`BROWSER_HARNESS_HOME=/var/lib/openhands-agent/browser` and `BH_AGENT_WORKSPACE=/var/lib/openhands-agent/browser/agent-workspace` keep profiles, downloads, screenshots, daemon sockets, and helper state outside repositories with mode 0700. `BROWSER_USE_HOME` is also set for compatibility. Do not copy browser state into reports or commits. Persistent profiles are VM secrets.

The browser service also points `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, and `XDG_DATA_HOME` below that writable private directory. Chrome for Testing requires a writable XDG configuration location to initialize its local Crashpad database even when reporting is disabled; leaving the defaults under the otherwise read-only service home causes an immediate `SIGTRAP`. The launcher passes `--disable-breakpad` and does not upload crash reports.

Browser Harness telemetry and Browser Use cloud sync are disabled in the worker environment. The pinned CLI may still perform a daily best-effort PyPI version check, but it cannot self-update the root-owned installation; upgrades remain an explicit `versions.env`/`agentctl update` operation.

Headless Chromium requires RAM and shared-memory headroom. URL/tool policy is not an egress firewall: enforce hard domain restrictions with VM DNS, proxy, or firewall rules. Keep downloads below the private browser state or the assigned worktree, and never report cookies, local storage, or credential exports.

Ubuntu restricts unprivileged user namespaces for downloaded Chromium builds. The installer loads a root-owned AppArmor profile scoped to `/opt/browser-use/chromium/**/chrome`, allowing the pinned Playwright build to create Chromium's own namespace/seccomp sandbox. It does not disable the restriction globally and does not launch Chromium with `--no-sandbox`.
