# Primary-source compatibility references

These are the upstream interfaces checked on 2026-07-16. Version-specific behavior is additionally captured by the local spike; documentation alone is not treated as proof of the pinned stack.

## Platform

- [Ubuntu 26.04 LTS images](https://releases.ubuntu.com/26.04/): Canonical publishes Ubuntu 26.04 LTS (Resolute Raccoon) server images.
- [Playwright release notes](https://playwright.dev/docs/release-notes): Playwright 1.61 adds Ubuntu 26.04 support and bundles Chromium 149.0.7827.55, matching the repository pins.
- [Playwright browser installation](https://playwright.dev/docs/browsers): `playwright install --with-deps chromium` installs both its version-matched browser and the host-specific system dependencies.
- [Ubuntu `python3-venv` packages](https://packages.ubuntu.com/search?keywords=python3-venv&searchon=names&section=all): the unversioned package follows each Ubuntu release's default Python, avoiding a Noble-only `python3.12-venv` dependency.
- [uv Python storage](https://docs.astral.sh/uv/reference/storage/): `UV_PYTHON_INSTALL_DIR` places a managed Python outside root's private home so installed service environments remain executable by their unprivileged accounts.

## OpenHands

- [ACP Agent](https://docs.openhands.dev/sdk/guides/agent-acp): OpenHands can delegate a conversation to an ACP server. The ACP server owns its tools and authentication; `mcp_config` is not supported on `ACPAgent`, so any provider-side MCP server must be configured by that provider.
- [OpenHands MCP settings](https://docs.openhands.dev/openhands/usage/settings/mcp-settings): Agent Canvas/OpenHands has MCP client support for non-ACP profiles. Symphony leaves this configuration empty by default because neither GitHub nor Browser Use MCP improves the security/billing properties of this workflow.
- [OpenHands automated code review ACP backend](https://docs.openhands.dev/openhands/usage/use-cases/code-review): documents authenticated ACP CLIs on trusted self-hosted runners and the Codex device-login/status flow.

Agent Canvas 1.4.0 and Agent Server 1.35.0 package/API behavior was inspected and exercised locally; see [capability-spike.md](capability-spike.md).

## Subscription-authenticated providers

- [Claude Code setup](https://docs.anthropic.com/en/docs/claude-code/getting-started): Claude Pro/Max authentication is supported, Ubuntu is supported, and `DISABLE_AUTOUPDATER=1` disables automatic updates.
- [Using Codex with a ChatGPT plan](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan): Codex CLI is available through ChatGPT sign-in and plan usage limits apply.
- [Antigravity CLI getting started](https://antigravity.google/docs/cli-getting-started): SSH authentication uses a printed authorization URL/code and credentials are stored in the OS secure keyring.
- [Antigravity CLI troubleshooting](https://antigravity.google/docs/cli-troubleshooting): Linux headless use requires D-Bus plus an accessible keyring daemon; `AGY_CLI_DISABLE_AUTO_UPDATE=true` disables its self-updater.
- [Antigravity headless CLI codelab](https://codelabs.developers.google.com/sdd-agy-cli): documents `--print`, `--print-timeout`, `--sandbox`, and `--mode`, which are the only interface used by the custom ACP bridge.

## Browser Use

- [Browser Use CLI](https://docs.browser-use.com/open-source/browser-use-cli): documents local direct browser commands, managed Chromium installation, `browser-use doctor`, persistent sessions, and `BROWSER_USE_HOME`.
- [Browser Harness connection reference](https://github.com/browser-use/browser-harness/blob/main/install.md): documents the `browser-harness` heredoc/helper interface and loopback `BU_CDP_URL` connection used by the worker prompt.
- [Chromium AppArmor user-namespace guidance](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md): documents Ubuntu's restriction on downloaded Chromium builds and the narrow per-build AppArmor profile used instead of disabling Chromium's sandbox.
- [Browser Use local MCP server](https://docs.browser-use.com/open-source/customize/integrations/mcp-server): the local MCP server requires an OpenAI or alternative model API key. Symphony therefore does not register it in the subscription-only default.

No upstream statement is used to claim that a provider is authenticated on a particular VM. `agentctl auth`, `agentctl doctor`, the capability spike, and the retained GitHub smoke artifacts are the deployment evidence.
