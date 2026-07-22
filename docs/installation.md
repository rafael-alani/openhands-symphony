# Installation and operations

## VM

Use an Ubuntu 26.04 LTS Proxmox VM, not an LXC. Ubuntu 24.04 LTS remains supported as a fallback. Enable **QEMU Guest Agent** in the Proxmox VM options (or run `qm set VMID --agent enabled=1` on the Proxmox host) and reboot the VM after the device is added; the installer installs the guest package and `doctor` reports a non-blocking warning when it is inactive. Use VirtIO SCSI/network and place workspaces on SSD-backed storage. For one agent, start with 6 vCPU, 16 GB RAM, and 80 GB disk; see the README for higher concurrency and Chromium tiers.

## Clean install

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates
git clone git@github.com:rafael-alani/openhands-symphony.git
cd openhands-symphony
sudo ./install.sh
sudoedit /etc/openhands-symphony/config.toml
```

The source repository is private. Configure a read-only deploy key or your GitHub SSH key on the VM before the clone; do not place that key in this repository or in the orchestrator configuration.

The idempotent installer accepts Ubuntu 26.04 or 24.04 LTS and rejects other hosts. It installs `gh` through GitHub's apt repository, verifies the official Node tarball, and installs a service-readable uv-managed Python 3.12 under `/opt/uv-python` for Symphony, Browser Use, and the Antigravity ACP bridge instead of relying on a distribution-specific Python package name. It pins Canvas/Agent Server/ACP/provider/Browser Use/Browser Harness/Playwright versions, lets the pinned Playwright release install the correct Chromium system dependencies for the selected Ubuntu release, installs pinned headless Chromium, pins Antigravity with Google-published SHA-512 hashes, disables Antigravity auto-update, creates separate orchestrator/agent/validator identities, installs a one-way lower-authority validation sudo rule plus one exact read-only nftables doctor probe, a narrow nftables non-exposure rule, and systemd units, and generates secrets without printing them. Existing configuration, credentials, state, and reports are preserved.

Pins and accepted ranges are in `versions.env`. `agentctl doctor` checks the installed versions and Agent Server response.

## Interactive authentication

OAuth cannot be silently automated. GitHub authority belongs to the orchestrator; provider subscriptions belong to the worker:

```bash
sudo systemctl start openhands-agent-keyring.service
sudo -iu openhands-symphony agentctl auth github
sudo -iu openhands-agent agentctl auth claude
sudo -iu openhands-agent agentctl auth codex
sudo -iu openhands-agent agentctl auth antigravity
```

Codex uses device authentication on headless Linux. Claude uses its official Pro/Max login. Each `agentctl auth` command, including GitHub, runs the provider's official status probe before OAuth; an existing login is reported and OAuth is skipped. A successful provider check records only a non-secret timestamp marker, so the authentication block is safe to repeat after an installer rerun. Antigravity is disabled by default; its line will fail the CPU preflight unless the VM exposes the required instruction-set feature, so omit it unless you explicitly intend to authenticate and enable that provider. The private D-Bus/Secret Service pair supplies its required Linux keyring when enabled.

Antigravity is installed and can be authenticated, but it remains `enabled = false` in the example configuration because no subscription-backed Ubuntu autonomous run has passed yet. After a successful disposable smoke run, enable it explicitly; no fallback provider is used when it is disabled.
Its executable is named `agy`, not `antigravity`. The optional `sudo -iu openhands-agent agentctl auth antigravity` command forces Antigravity's remote SSH OAuth mode after `sudo` strips the caller's SSH variables. Open the URL it prints in a local browser, complete sign-in, and paste only the resulting alphanumeric authorization code into the terminal—not the browser URL.

## Labels, start, and verify

```bash
sudo -iu openhands-symphony agentctl labels
sudo agentctl start
sudo -iu openhands-symphony agentctl doctor
sudo -iu openhands-symphony agentctl status
curl -fsS http://127.0.0.1:8787/healthz
```

`doctor` must be run after start because it checks the pinned Agent Server over the authenticated localhost API.

### Clean-machine regression gate

Do not route a real issue until every required `doctor` row is `PASS`. The first VM exposed several installer/runtime integration failures that are now guarded explicitly:

| Previously observed failure | Default prevention and verification |
|---|---|
| Ubuntu release-specific Python package/uv behavior | The installer accepts only tested Ubuntu LTS releases, uses release-neutral packages plus pinned uv-managed Python, and the platform tests reject other hosts. |
| Proxmox reported no guest agent | The installer includes `qemu-guest-agent`; `doctor` warns when the Proxmox option/device is not active. Balloon minimum/maximum policy remains an explicit host setting. |
| `git pull` left a stale installed `agentctl` | `agentctl update` force-refreshes the wheel and byte-compares installed Symphony Python sources with the checkout. |
| Re-running authentication restarted OAuth | Every provider and GitHub runs its official status probe first; tests assert that a successful probe skips login. |
| Browser Harness executable was absent | The installer exposes the pinned package's executables; `doctor` checks both executable and package version. |
| Chromium failed under Ubuntu's user-namespace policy | A narrow AppArmor profile permits the pinned Chromium sandbox; the live CDP/version check proves startup without `--no-sandbox`. |
| Chromium Crashpad aborted under the read-only service home | Profile plus XDG/Crashpad state live below the writable private browser directory; `doctor` checks the installed service environment and live CDP. |
| An unhealthy browser left an apparently active target | `agentctl start` verifies every required unit and restarts an unhealthy target; `doctor` reports the failed unit and a privileged journal command when needed. |
| Doctor crashed while probing an intentionally inaccessible worker credential | Permission denial is treated as expected account isolation and covered by a regression test. |
| Empty `setup_script` ran the worktree directory; Ubuntu `setpriv` rejected `--umask` | Empty setup is now a no-op. `doctor` executes the exact production validator wrapper and verifies the lower-authority user, clean environment, and `0007` umask before any issue is accepted. |
| Agent Server could not traverse an orchestrator-created worktree under `UMask=0077` | Workspace container and repository-cache parent directories receive explicit shared-group traversal without granting workers permission to list or create sibling runs. |

The repository test suite covers these static contracts, while `doctor` covers the installed VM. Retain the complete clean `doctor` output with the first disposable [end-to-end smoke test](smoke-test.md); local tests alone do not certify a machine.

## Operations

```bash
sudo agentctl start
sudo agentctl stop
sudo agentctl restart
sudo -iu openhands-symphony agentctl status
sudo agentctl logs
sudo agentctl update
sudo -iu openhands-symphony agentctl reconcile
sudo -iu openhands-symphony agentctl run owner/repo#123
sudo -iu openhands-symphony agentctl cancel owner/repo#123
```

`agentctl update` reruns the installer from the source checkout recorded at installation. Update that checkout and review `versions.env` intentionally first; the command itself does not pull a branch and no service follows an unpinned `latest` channel. The installer stops an active target before replacing executables and restarts it only after a successful update.

In particular, `git pull` alone does not replace `/usr/local/bin/agentctl`: that executable comes from the installed environment under `/opt/openhands-symphony-tool`. After pulling, run `sudo agentctl update` (or `sudo ./install.sh --update` from the checkout) before testing changed CLI behavior. Updates force-refresh the local project wheel even when its version is unchanged and compare the installed package's Python sources with the checkout before reporting success.

## Access

```bash
ssh -L 8000:127.0.0.1:8000 -L 8787:127.0.0.1:8787 your-vm
```

Open Canvas at `http://127.0.0.1:8000`. On first launch, choose Codex (recommended after its authentication check passes) or Claude Code as the default for manual conversations. The choice does not affect Symphony provider routing; see the [Canvas operator guide](canvas.md). A GitHub webhook cannot reach loopback directly; either expose only `/webhooks/github` through a narrow HTTPS ingress or rely on five-minute reconciliation. Tailscale requires an explicit interface-specific nftables exception because non-loopback access is blocked by default. Never expose all of Canvas.
