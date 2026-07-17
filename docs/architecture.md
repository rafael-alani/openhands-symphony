# Architecture

## Components

```text
GitHub webhook ─┐
                ├─> Symphony intake ─> SQLite jobs/leases ─> fair scheduler
reconciliation ─┘                                      │
                                                       v
                                              isolated git worktree
                                                       │
                                           OpenHands Agent Server REST
                                                       │
                              fresh Claude/Codex/custom-`agy` ACP process
                                                       │
                  credential-free validation -> live guard -> push -> draft PR
                                                       │
                                          optional fresh review process
```

GitHub remains the specification, routing surface, and result surface. SQLite is operational state, not a replacement source of truth. Human-readable Markdown and JSON reports live separately under the report directory.

Agent Canvas supplies the UI, conversation persistence, Agent Profiles, and Agent Server. Symphony uses the versioned Agent Server REST API through `OpenHandsACPProvider`; it does not fork Canvas or modify OpenHands internals.

The orchestration service and GitHub adapter run as `openhands-symphony`. Canvas, providers, and the loopback-only headless Chromium service run as `openhands-agent`. Repository setup/tests run as `openhands-validator`, which has neither credential set. Only the worktree root is group-shared, so model and validation processes do not inherit the orchestrator's GitHub credential store.

Canvas 1.4.0's internal Agent Server and automation backend bind loopback, but its Node ingress listens on a wildcard socket. A dedicated `inet openhands_symphony` nftables table therefore drops every non-loopback inbound packet to ports 8000, 8787, and 9222 before the stack starts. This is the default non-exposure boundary; SSH forwarding continues to work.

## Analysis versus mutation

Preflight reads the issue, labels, repository privacy/default branch, generated branch, linked PR, and provider health. An agent receives no GitHub write token and is explicitly limited to its worktree. The wrapper owns labels, the canonical comment, commits, pushes, PR creation, and reviews.

Immediately before a push or PR creation, `guard_code_mutation` fetches the live issue again and rejects mutation when:

- the issue closed;
- `agent:paused` or `agent:manual-only` appeared;
- title or body differs from the accepted claim hash;
- routing labels are no longer exact;
- another PR or non-recoverable generated branch exists.

After a crash between push and PR creation, an existing remote branch is accepted only when its SHA matches the preserved worktree HEAD. Otherwise the run asks for guidance instead of overwriting it.

## Durable intake and idempotency

`deliveries.delivery_id` deduplicates webhook retries. `jobs` has a unique `(repository, issue_number)` key. The canonical comment is discovered through `<!-- openhands-symphony-status -->` only when the authenticated bot owns it, so losing its local ID does not append a new comment or adopt an attacker-controlled marker. Branch and PR lookup makes restarts artifact-aware.

The event path handles exact issues with low latency. The scheduler also searches each allowlisted repository for open `agent:ready` issues. A persistent systemd timer provides a second reconciliation trigger when the long-running service was offline.

## Concurrency and fairness

The default concurrency key is `owner/repository`. A transactional `leases` row is unique by key; `BEGIN IMMEDIATE` makes two workers unable to claim it together. A repository may set a stable monorepo project key. Global and per-provider limits apply in addition.

Jobs are ordered by explicit retry first, then the repository least recently given a claim, then oldest update. A held repository key causes the query to select eligible work from another repository. This round-robin bias prevents a large backlog in one repository from monopolizing a global slot. Provider backoff rows suppress auth/quota/tool retry loops and receive bounded additive jitter so recovered providers are not hit in lockstep.

The worker renews its lease for the complete claimed lifecycle: checkout, setup, provider execution, validation, GitHub mutation, independent review, and repair. It also renews synchronously immediately before a push, PR creation/update, or review mutation. Canonical status-comment synchronization uses a separate expiring SQLite operation lock, preventing the webhook service and timer process from racing to create duplicate comments.

## Trust boundaries

Issue titles, bodies, labels, branch slugs, and comments are untrusted. They are never interpolated into a shell. Repository IDs are validated, branches are generated from a restricted slug, subprocesses receive argument arrays, and all worktrees must resolve below the configured workspace root.

Repository code is trusted only because public repositories and unallowlisted repositories are disabled by default. Never route untrusted public pull requests to this VM.
