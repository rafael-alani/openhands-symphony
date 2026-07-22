# OpenHands Symphony

OpenHands Symphony is a self-hosted, durable GitHub issue-to-draft-PR companion orchestrator for a dedicated Ubuntu VM. OpenHands Agent Canvas is the UI and agent server; a small provider-neutral service supplies the scheduling semantics Canvas automations currently lack. Claude Code and Codex use ACP. An Antigravity adapter bridges ACP to the official `agy --print` interface, but the example configuration keeps it disabled until a subscription-backed Ubuntu smoke run passes. Provider login belongs to the worker account, and production merges are always human-controlled.

The design borrows only the requested ClawSweeper patterns—analysis/mutation separation, exact-item intake plus reconciliation, durable per-item state, leases, a canonical status comment, and bounded loops. No ClawSweeper repository or code is cloned, indexed, vendored, or required.

## Safe default flow

1. Write and refine a detailed issue yourself.
2. Add `agent:ready` and exactly one of `agent:claude`, `agent:codex`, or `agent:antigravity`.
3. Optionally add `review:required` and one `review:*` provider.
4. The webhook path queues it immediately, or the five-minute reconciler recovers it.
5. One implementation lease per repository is the default. The selected ACP agent edits only its isolated worktree.
6. A credential-free validator reruns configured quality gates; the wrapper re-reads live GitHub state, commits, pushes, and opens a draft PR.
7. GitHub contains either that PR, one focused guidance question, or a terminal failure report. Nothing auto-merges.

`agent:antigravity` produces a clear provider-unavailable guidance state while the adapter is disabled; Symphony never silently substitutes another provider.

Use `/agent pause`, `/agent resume`, `/agent retry`, or `/agent cancel` in an issue comment. Commands are accepted only from an owner, member, or collaborator.

## Clean install

The preferred target is an unprivileged service on an Ubuntu 26.04 LTS Proxmox **VM** (not LXC). Ubuntu 24.04 LTS remains supported as a fallback. From a fresh VM:

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates
git clone git@github.com:rafael-alani/openhands-symphony.git
cd openhands-symphony
sudo ./install.sh
```

Because the source repository is private, install a read-only GitHub deploy key or your own GitHub SSH key on the VM before cloning. The installer does not copy or print that key.

The installer preserves `/etc/openhands-symphony/config.toml` and credential state on reruns. It exposes nothing publicly: the webhook listener and Chromium bind loopback, while a dedicated nftables table drops non-loopback ingress to Canvas, the webhook listener, and CDP (ports 8000/8787/9222). This firewall is necessary because Canvas 1.4.0's Node ingress listens on a wildcard socket even though its internal backends use loopback.

After replacing `CHANGE_ME/CHANGE_ME` in the config:

```bash
sudo systemctl start openhands-agent-keyring.service
sudo -iu openhands-symphony agentctl auth github
sudo -iu openhands-agent agentctl auth claude
sudo -iu openhands-agent agentctl auth codex
sudo -iu openhands-symphony agentctl labels
sudo agentctl start
sudo -iu openhands-symphony agentctl doctor
sudo -iu openhands-symphony agentctl status
```

Each `agentctl auth` command checks the provider's official login status first. If that account is already authenticated, it reports that no login is needed and does not start OAuth, so this block is safe to repeat after an installer rerun.

Antigravity is disabled by default. Do not authenticate or probe it unless you explicitly enable it after verifying that the VM CPU exposes the instruction-set features required by its official binary.
Its official executable is `agy`, not `antigravity`. For an optional worker-account login, `agentctl auth antigravity` forces the remote SSH URL/code flow even though `sudo -iu` removes the original SSH environment: open the printed URL locally, then paste only the alphanumeric code displayed by the browser back into the terminal.

The example deliberately leaves repository validation empty. Once the first architecture issue establishes the real toolchain, its agent must add a non-interactive `.openhands/quality-gate.sh` containing the repository's actual checks. Symphony executes that credential-free gate and will not push a draft PR without passing evidence. Operators may instead pin immutable `validation_commands` in the service configuration at any time.

Open Canvas through SSH:

```bash
ssh -L 8000:127.0.0.1:8000 your-vm
```

Then open `http://127.0.0.1:8000`. Tailscale is a good alternative after adding a narrow `tailscale0` exception to the installed nftables table. Do not expose Canvas or Symphony directly to the public internet.

## Resource sizing

| Workload | Minimum | Recommended |
|---|---:|---:|
| One implementation agent, no browser | 4 vCPU, 8 GB RAM, 40 GB SSD | 6 vCPU, 16 GB RAM, 80 GB SSD |
| Two or three repositories concurrently | 8 vCPU, 24 GB RAM, 120 GB SSD | 12 vCPU, 32 GB RAM, 200 GB SSD |
| Concurrent agents with Chromium | 12 vCPU, 32 GB RAM, 160 GB SSD | 16 vCPU, 48–64 GB RAM, 250 GB SSD |

The OpenHands single-user baseline is smaller; these allocations include agent CLIs, dependency builds, Git worktrees, Browser Use, and useful headroom. Add swap only as an emergency buffer, not as a substitute for RAM.

## Documentation

- [Architecture](docs/architecture.md)
- [Installation and operations](docs/installation.md)
- [State machine](docs/state-machine.md)
- [GitHub and label contract](docs/github-contract.md)
- [Provider adapter contract and support matrix](docs/providers.md)
- [Browser tooling](docs/browser.md)
- [Security model](docs/security.md)
- [Configuration reference](docs/configuration.md)
- [Recovery, backup, and migrations](docs/recovery.md)
- [Capability-spike evidence](docs/capability-spike.md)
- [End-to-end smoke test](docs/smoke-test.md)
- [Recorded decisions](docs/decisions.md)
- [Primary-source compatibility references](docs/sources.md)

## Development

```bash
UV_CACHE_DIR=/tmp/uv-cache uv sync --extra test
UV_CACHE_DIR=/tmp/uv-cache uv run --extra test pytest
```

The deterministic fake provider never consumes model quota. The test suite covers duplicate delivery, restart recovery, per-repository concurrency, needs-guidance, independent review, validation evidence, and mutation revalidation after pause/edit.
