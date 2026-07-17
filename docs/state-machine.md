# State machine

```text
queued -> running -> pr-open
   |         |          |
   |         |          +-> reviewing -> pr-open
   |         |                    |          |
   |         |                    |          +-> queued (explicit review retry)
   |         +-> needs-guidance -> queued (explicit resume/retry)
   |         +-> blocked        -> queued (explicit retry)
   |         +-> failed         -> queued (bounded retry)
   |         +-> canceled       -> queued (explicit retry)
   +-> canceled

pr-open -> done only after later reconciliation observes the merged/closed result
```

`agent:pr-open` is intentionally the stable successful automation outcome while the draft remains open. A clean independent review does not merge the PR and does not prematurely mark the issue done.

## Claim and lease

Claiming atomically inserts a lease, moves `queued` to `running`, increments the attempt, records the owner/expiry, and starts heartbeat timestamps. Heartbeats renew the lease. A worker cancellation or `/agent pause` interrupts the OpenHands conversation.

At restart, unexpired leases remain authoritative. After expiry, reconciliation first interrupts the durable OpenHands implementation, reviewer, or repair conversation. Only after cancellation succeeds does it remove the lease and return implementation to `queued`; interrupted review work is requeued against the preserved PR. If the provider/cancel endpoint is unavailable, the job stops for guidance and is not made runnable. The next worker resumes the interrupted OpenHands conversation when supported or starts a fresh review process; attempts remain bounded.

The same reconciliation pass replays trusted exact control comments missed while the service was offline. GitHub comment IDs are the idempotency key, so webhook redelivery and scheduled recovery cannot apply the same pause/resume/retry/cancel twice.

## Bounded loops

- `max_attempts`: total implementation attempts, default 3.
- `max_implementation_corrections`: same-session corrections after wrapper validation, default 1.
- `max_review_repairs`: implementer passes prompted by independent review, default 1.
- `max_iterations`: OpenHands conversation bound, set by the adapter.
- per-provider timeout and per-command validation timeout.

Exhaustion is a named failure with retained report evidence. Review exhaustion preserves the implementation PR and posts an actionable status rather than hiding it.

## Guidance

`needs-guidance` is nonterminal but never resumes implicitly. A missing product decision, secret, credential, inaccessible service, destructive migration, or unsafe ambiguity produces one focused question. Workspace and conversation IDs are retained. Only `/agent resume`, `/agent retry`, or an explicit `agentctl run` accepts the latest issue revision and requeues it.
