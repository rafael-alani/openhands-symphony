# Four-phase and follow-on implementation audit

Audit date: 2026-09-20.

The original plan has four **Ideas phases**, preceded by the file-contract
Phase 0. There are three workflow tiers: controlled GitHub development, Ideas
prototyping, and the time-limited Hack overlay. The later Obsidian changes
replace the legacy sync/graduation path for ordinary notes; both paths remain
supported.

| Scope | Implementation | Evidence |
| --- | --- | --- |
| Phase 0: spec/progress ownership and manual run template | Present | `examples/idea-template`, `docs/ideas-run-prompt.md`, contract tests |
| Phase 1: isolated vault/Git sync, conflict handling, scheduler, preview opening | Present as legacy `ideasync` | `ideasync/tests`; macOS launchd scheduler, SSH preview command |
| Phase 2: shared leases/capacity, spec-driven execution, supersession, guarded publication | Present; correctness fixes added by this audit | `tests/test_ideas.py`, `tests/test_store.py`, `tests/test_orchestrator.py` |
| Phase 3: persistent previews, isolated setup/build, health checks, rollback and cleanup | Present; process and rebase checks strengthened | `tests/test_preview_manager.py`, preview tests in `tests/test_ideas.py` |
| Phase 4: reviewed legacy graduation into GitHub issues | Present; stale completion and campaign-race protection added | `tests/test_graduation.py`, `tests/test_hack_boundaries.py` |
| Syncthing + normal Markdown, automatic private repositories, reversible idea/github/paused modes | Present | `tests/test_vault.py`, `tests/test_vault_install.py`, existing `docs/evidence/obsidian-acceptance-20260920.json` |
| Folder note brief, checklist/table subfiles, Waypoint exclusion, automatic checkbox completion/reopening | Present | `tests/test_vault_project.py`, `tests/test_vault.py` |
| Full worker permissions and credential-free preview dependency setup | Present | provider, setup and preview tests; separate worker/validator identities |
| Hack Phase A: lane concurrency and campaign integrator | Added by this audit | `hack_contract.py`, `hack_store.py`, `hack_coordinator.py`; contract/store/Git integration tests |
| Hack Phase B: board watcher, model dispatcher and dependent tasks | Added by this audit | dispatcher prompt/result validation; webhook, store and integration tests |
| Hack Phase C: start/stop/deadline, scaffold/polish, one final publication, home-mode restoration | Added by this audit | CLI, scheduler, expiry/recovery, publication and cross-tier boundary tests |

## Corrections found during the audit

- A changed global brief or removed/reordered section could leave old completion
  claims trusted. These changes now invalidate affected progress and graduation
  carries unfinished work forward. Unresolved sections are revisited after new
  guidance even when their own bytes did not change.
- A publication rebased onto a moving branch could skip the mandatory preview
  gate. The rebased candidate now has to boot successfully before either push.
- Question-only publication needed the same Git metadata integrity check as
  normal publication.
- A noisy preview could fill an unread stdout pipe; wrapper exit could leave
  descendants running. Startup logs now spool to a bounded readback and process
  groups are cleaned up. Recovery stops unhealthy still-running preview processes.
- `doctor` incorrectly rejected valid vault-only or Ideas-only installations.
- New Hack integration preserves scoped monorepo concurrency, fences vault
  changes and graduation, reserves normal-job capacity without blocking the
  scheduler on build checks, and retains completed preview authorization until
  explicitly revoked. Older Ideas runs cannot redeploy over the campaign demo;
  a subsequent Ideas publication takes preview ownership again.

## Verification boundaries

Final local validation:

- Symphony: **352 tests passed** (`.venv/bin/python -m pytest`).
- Legacy bridge: **21 tests passed** (`ideasync/.venv/bin/python -m pytest ideasync/tests -q`).
- Ruff passed across both source trees and test suites.
- `git diff --check` passed.

Package validation on 2026-09-24 built source distributions and wheels for both
Symphony and ideasync. Both wheels installed together in a fresh Python 3.12
environment; the packaged `agentctl hack` and `ideasync` help commands and
Symphony campaign/service/preview module imports passed.

The automated suites use temporary Git remotes, a deterministic provider,
temporary SQLite databases, and real local preview processes. They do not prove
provider quota behavior or Linux account isolation for a newly deployed Hack
campaign. The existing VM acceptance artifact covers the earlier Obsidian and
GitHub workflow; it does not cover the Hack code introduced here. This audit
does not deploy the new checkout or start a real campaign.

The standalone VibeProxy proposal remains an external prerequisite, rather than
an implemented provider. The official application still targets macOS, and no
Ubuntu VibeProxy packaging or route-specific lifecycle acceptance is recorded.
The [rechecked proposal](vibeproxy-integration.md) records the sources and gates.
Antigravity likewise remains an existing optional adapter with its original
subscription-backed acceptance gate. Neither is silently substituted for a
working provider.

Section screenshots remain visual evidence from the app's home page. They are
not a claim that an agent individually exercised every feature. Full quality
checks remain advisory for Ideas and mandatory for controlled GitHub runs;
Hack uses the explicitly documented fast build/boot gate.
