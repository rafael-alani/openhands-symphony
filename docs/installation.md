# Installation and operations

## VM

Use an Ubuntu 26.04 LTS Proxmox VM, not an LXC. Ubuntu 24.04 LTS remains supported as a fallback. Enable the QEMU guest agent, use VirtIO SCSI/network, and place workspaces on SSD-backed storage. For one agent, start with 6 vCPU, 16 GB RAM, and 80 GB disk; see the README for higher concurrency and Chromium tiers.

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
```

Codex uses device authentication on headless Linux. Claude uses its official Pro/Max login. Each provider command finishes with its official status probe and records only a non-secret timestamp marker. Antigravity is disabled by default; authenticate it only after explicitly enabling it and verifying the official binary against the VM CPU in a disposable smoke test. The private D-Bus/Secret Service pair supplies its required Linux keyring when enabled.

Antigravity is installed and can be authenticated, but it remains `enabled = false` in the example configuration because no subscription-backed Ubuntu autonomous run has passed yet. After a successful disposable smoke run, enable it explicitly; no fallback provider is used when it is disabled.

## Labels, start, and verify

```bash
sudo -iu openhands-symphony agentctl labels
sudo agentctl start
sudo -iu openhands-symphony agentctl doctor
sudo -iu openhands-symphony agentctl status
curl -fsS http://127.0.0.1:8787/healthz
```

`doctor` must be run after start because it checks the pinned Agent Server over the authenticated localhost API.

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

## Access

```bash
ssh -L 8000:127.0.0.1:8000 -L 8787:127.0.0.1:8787 your-vm
```

Open Canvas at `http://127.0.0.1:8000`. A GitHub webhook cannot reach loopback directly; either expose only `/webhooks/github` through a narrow HTTPS ingress or rely on five-minute reconciliation. Tailscale requires an explicit interface-specific nftables exception because non-loopback access is blocked by default. Never expose all of Canvas.
