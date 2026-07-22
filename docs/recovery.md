# Recovery, backup, and migration

## Normal restart

```bash
sudo agentctl restart
sudo -iu openhands-symphony agentctl reconcile
sudo -iu openhands-symphony agentctl status
```

Unexpired leases are not stolen. For an expired lease, reconciliation identifies the durable implementation, reviewer, or repair conversation and interrupts it through the provider adapter before making the job runnable. Cancellation failure produces a guidance state instead of risking two workers. A canceled implementation conversation may then resume when supported; interrupted review work starts a fresh review against the preserved PR. Existing worktrees, matching remote branches, canonical comments, and PRs are adopted rather than recreated.

Reconciliation also scans trusted repository issue comments for exact `/agent` commands. Each command is durably coalesced by GitHub comment ID across the webhook and timer processes, recovering operator controls missed during VM downtime.

## Inspect a failed run

The canonical GitHub comment includes the durable run ID. Use it to read the redacted human-readable report on the VM before retrying:

```bash
sudo sed -n '1,240p' /var/lib/openhands-symphony/reports/RUN_ID/run.md
sudo jq '.validations' /var/lib/openhands-symphony/reports/RUN_ID/run.json
```

The Markdown report includes each setup or validation command, exit status, timeout status, and retained output. The JSON report also includes the job event history. For surrounding service events, use `sudo agentctl logs` or query `openhands-symphony.service` with `journalctl`. Correct the reported cause and deploy any Symphony update before posting `/agent retry`; retrying an unchanged setup failure consumes another bounded attempt.

## Backup

Stop the stack for the simplest consistent filesystem backup:

```bash
sudo agentctl stop
sudo install -d -m 0700 /var/backups/openhands-symphony
sudo sqlite3 /var/lib/openhands-symphony/state.db ".backup '/var/backups/openhands-symphony/state.db'"
sudo tar --xattrs --acls -C /var/lib -czf /var/backups/openhands-symphony/state-and-reports.tgz openhands-symphony
sudo tar --xattrs --acls -C /var/lib -czf /var/backups/openhands-symphony/provider-and-browser-state.tgz openhands-agent openhands-auth-status
sudo tar --xattrs --acls -C /etc -czf /var/backups/openhands-symphony/config.tgz openhands-symphony
sudo agentctl start
```

The `/etc` archive contains secrets; encrypt it and restrict access. Provider OAuth and browser profiles also require secret-grade handling. Git branches/PRs are already redundant remote artifacts, but unpushed worktrees exist only on the VM.

## State loss

Do not delete an orphan generated branch. Reconciliation intentionally marks it for guidance unless a durable job/worktree proves the local and remote commits match. Restore the database/reports when possible; otherwise inspect and adopt artifacts manually.

## Migrations

SQLite uses `PRAGMA user_version`. Migrations run transactionally at service start, reject a database newer than the binary, and only move forward. Before `agentctl update`, the installer preserves config/state and a production operator should take the backup above. Rollback means reinstalling the previous versions from `versions.env` and restoring the pre-migration database if the schema changed.

Provider/Canvas upgrades are never automatic. Antigravity's built-in updater is disabled. Update `versions.env` and checksums, rerun the capability/headless-interface spikes, run all tests, then run `agentctl update`.
