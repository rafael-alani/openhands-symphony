# GitHub contract

## User-controlled labels

| Label | Meaning |
|---|---|
| `agent:ready` | Explicit autonomous implementation trigger |
| `agent:claude` / `agent:codex` / `agent:antigravity` | Exactly one implementation provider |
| `review:required` | Request a fresh independent reviewer |
| `review:claude` / `review:codex` / `review:antigravity` | Optional single review provider |
| `agent:paused` | Prevent code mutation until explicit resume |
| `agent:manual-only` | Permanently exclude from autonomous intake while present |

If review is required without a review-provider label, Symphony chooses a different available provider from the implementer when possible. It never silently falls back for implementation.

## System-controlled labels

`agent:queued`, `agent:running`, `agent:needs-guidance`, `agent:pr-open`, `agent:failed`, and `agent:done` are mutually normalized by the wrapper. Generated PRs receive `generated-by-agent` by default.

Recommended colors/descriptions are in `symphony.labels.LABEL_CONTRACT`; `agentctl labels` creates or updates them after GitHub authentication.

## Canonical status comment

One comment per issue contains a hidden marker and is edited in place. It shows state, provider/attempt, start/elapsed time, phase, branch, PR, validation summary, focused question/failure, and run ID. Reconciliation searches for the marker if the stored comment ID is missing.

## Commands

The exact accepted issue comments are:

```text
/agent pause
/agent resume
/agent retry
/agent cancel
```

No suffix or embedded command is accepted. The webhook author association must be OWNER, MEMBER, or COLLABORATOR.

## Branches and PRs

Branches are `agent/<issue>-<short-slug>`. PRs always begin as drafts and include:

- `Closes #<issue>` (closure occurs only after merge);
- provider, run ID, and attempt;
- change summary;
- exact validation commands and observed outcomes;
- remaining risks;
- `generated-by-agent`.

Production auto-merge does not exist in the implementation. A disposable smoke repository may enable it only through a separate explicit test action.
