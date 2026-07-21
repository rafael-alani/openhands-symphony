# End-to-end GitHub smoke test

The smoke test creates two clearly named **private** repositories and never deletes them:

- `OWNER/openhands-symphony-smoke-20260716`
- `OWNER/openhands-symphony-smoke-peer-20260716`

It never enables auto-merge. Run it only from the clean Ubuntu VM as an administrator with `sudo`; the script executes every GitHub/SQLite operation as the unprivileged orchestrator identity and uses privilege only to restart the systemd target during recovery simulation.

## Prepare repositories

After `agentctl auth github`:

```bash
cd /opt/openhands-symphony
sudo -iu openhands-symphony ./scripts/prepare_smoke_repositories.sh
```

Add the printed repositories to `github.allowed_repositories`. Add a section for each:

```toml
[repositories."OWNER/openhands-symphony-smoke-20260716"]
concurrency_scope = "repository"
concurrency_key = ""
validation_commands = [["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]]
setup_script = ".openhands/setup.sh"
instruction = ""
approval_policy = "safe-code-only"

[repositories."OWNER/openhands-symphony-smoke-peer-20260716"]
concurrency_scope = "repository"
concurrency_key = ""
validation_commands = [["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]]
setup_script = ".openhands/setup.sh"
instruction = ""
approval_policy = "safe-code-only"
```

Authenticate the provider users, start the stack, and require a clean doctor:

```bash
sudo systemctl start openhands-agent-keyring.service
sudo -iu openhands-agent agentctl auth claude
sudo -iu openhands-agent agentctl auth codex
sudo agentctl start
sudo -iu openhands-symphony agentctl doctor
```

## Execute

The default uses Codex for implementation and Claude for independent review/parallel peer work:

```bash
cd /opt/openhands-symphony
./scripts/run_smoke_test.sh
```

Provider selection and the three-hour default timeout are explicit overrides:

```bash
SYMPHONY_SMOKE_IMPLEMENTER=claude \
SYMPHONY_SMOKE_REVIEWER=codex \
SYMPHONY_SMOKE_TIMEOUT_SECONDS=10800 \
./scripts/run_smoke_test.sh
```

The script creates or reuses exact-title issues, posts one signed webhook delivery twice, observes same-repository serialization, runs a different provider in the peer repository, restarts the target during the recovery case, explicitly expires that one smoke lease, and writes `/var/lib/openhands-symphony/reports/smoke-20260716.json`.

## Expected GitHub state

On a fresh pair of repositories, the primary repository has six open issues and up to five open draft PRs; the peer has one issue and one draft PR.

| Case | Expected issue state | Expected external artifacts |
|---|---|---|
| Successful implementation | `agent:pr-open` | Exactly one `agent/<n>-...` branch, one draft PR, one canonical status comment |
| Independent review | `agent:pr-open` | One draft PR plus at least one categorized `COMMENT` PR review from a fresh provider process; PR remains open |
| Needs guidance | `agent:needs-guidance` | One focused question in the canonical comment; no generated branch or PR |
| Same-repository concurrency A/B | One `agent:running` while the other is visibly `agent:queued`, then both `agent:pr-open` sequentially | Never two active implementation leases for the repository |
| Peer repository | May run while the primary lease is held, subject to global/provider limits | Its own isolated branch and draft PR |
| Recovery | `agent:pr-open` or an explicit terminal `agent:failed` | No duplicate job, status comment, branch, or PR after restart/lease reconciliation |

All created PRs contain `Closes #<issue>`, provider/run provenance, exact observed validation commands, and residual risk. Issues remain open because every PR remains a draft and nothing merges automatically.

The repository is not considered verified merely because local fake-provider tests pass. Verification requires retaining the generated GitHub artifacts, the smoke JSON report, clean test output, and the VM's complete `agentctl doctor` output.
