from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from conftest import issue

from symphony.hack_contract import HackContractError
from symphony.hack_store import HackStore
from symphony.models import IdeaSnapshot, JobState
from symphony.store import Store, StoreError


@pytest.fixture
def state(tmp_path):
    store = Store(tmp_path / "state.db")
    return store, HackStore(store)


def start(hacks, repository="solo/project", **kwargs):
    return hacks.start_campaign(repository, "github", "main", "a" * 40, "codex", 2, **kwargs)


def activate(hacks, campaign):
    scaffold = next(task for task in hacks.list_tasks(campaign["id"]) if task["kind"] == "scaffold")
    hacks.finish_task(scaffold["id"], "merged")
    return hacks.update_campaign(campaign["id"], state="active", result_commit="b" * 40)


def lane(hacks, campaign, key="api", lane="api", **kwargs):
    return hacks.enqueue_task(campaign["id"], key, lane, f"Implement {key}", [f"{lane}/**"], **kwargs)


def claim(hacks, *, global_limit=8, provider_limit=8):
    return hacks.claim_next("worker", 60, global_limit, {"codex": provider_limit})


def issue_job(store, repository="solo/project"):
    snapshot = issue(repository)
    return store.ensure_job(snapshot, "codex", None, False, "agent/test", repository)[0]


def idea_job(store, repository="solo/project"):
    return store.ensure_idea_run(IdeaSnapshot(
        repository=repository, spec_hash="spec-one", spec_content=b"spec", runtime_content=b"runtime",
        previous_progress=b"", base_commit="a" * 40, default_branch="main",
    ), "codex")[0]


def test_campaign_start_and_scaffold_persist_and_exclusively_fence_both_intakes(state):
    store, hacks = state
    issue_job(store)
    idea_job(store)
    campaign = start(hacks)
    assert store.hack_active("solo/project")
    assert store.claim_next("issue", 60, 8, {"codex": 8}) is None
    assert store.claim_next_idea("idea", 60, 8, {"codex": 8}) is None
    restarted = HackStore(Store(store.path))
    assert restarted.active_campaign("solo/project")["id"] == campaign["id"]
    assert len(restarted.list_tasks(campaign["id"])) == 1
    with pytest.raises(StoreError, match="already has"):
        start(hacks)
    hacks.update_campaign(campaign["id"], state="completed")
    assert not store.hack_active("solo/project")
    assert store.claim_next("issue", 60, 8, {"codex": 8})


def test_campaign_start_rejects_existing_work_even_expired_unreconciled_lease(state):
    store, hacks = state
    issue_job(store)
    store.claim_next("worker", 60, 8, {"codex": 8})
    with store.transaction() as connection:
        connection.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00'")
    with pytest.raises(StoreError, match="existing execution lease"):
        start(hacks)


def test_campaign_start_cannot_race_tier_graduation(state):
    store, hacks = state
    assert store.acquire_operation_lock("graduate", "cli", 60)
    with pytest.raises(StoreError, match="graduation is active"):
        start(hacks)
    assert not hacks.list_campaigns()
    store.release_operation_lock("graduate", "cli")
    assert start(hacks)


@pytest.mark.parametrize("settings", [{"hours": 0}, {"hours": 169}, {"hours": float("nan")}, {"max_parallel": 7}, {"max_parallel": 1.5}, {"max_tasks": 0}])
def test_invalid_campaign_limits_fail(state, settings):
    _, hacks = state
    arguments = dict(repository="solo/project", home_tier="github", default_branch="main", base_commit="abc", provider="codex", hours=2)
    arguments.update(settings)
    with pytest.raises(StoreError):
        hacks.start_campaign(**arguments)


def test_scaffold_is_mandatory_and_solo_until_integrated(state):
    _, hacks = state
    campaign = start(hacks)
    lane(hacks, campaign)
    scaffold = claim(hacks)
    assert scaffold["kind"] == "scaffold"
    assert claim(hacks) is None
    hacks.finish_task(scaffold["id"], "ready")
    hacks.update_campaign(campaign["id"], state="active")
    assert claim(hacks) is None
    hacks.finish_task(scaffold["id"], "merged")
    assert claim(hacks)["kind"] == "lane"


def test_lanes_fan_out_but_each_lane_waits_for_integration(state):
    _, hacks = state
    campaign = activate(hacks, start(hacks, max_parallel=2))
    api = lane(hacks, campaign)
    lane(hacks, campaign, "api-two")
    ui = lane(hacks, campaign, "ui", "ui")
    lane(hacks, campaign, "infra", "infra")
    first = claim(hacks)
    second = claim(hacks)
    assert (first["id"], second["id"]) == (api["id"], ui["id"])
    assert first["base_commit"] == "b" * 40
    assert claim(hacks) is None
    hacks.finish_task(first["id"], "ready", result_commit="c" * 40)
    assert claim(hacks)["lane"] == "infra"
    assert claim(hacks) is None
    hacks.finish_task(first["id"], "merged")
    hacks.finish_task(second["id"], "merged")
    assert claim(hacks)["key"] == "api-two"


def test_dependencies_wait_for_merged_contract(state):
    _, hacks = state
    campaign = activate(hacks, start(hacks))
    api = lane(hacks, campaign, "contract")
    lane(hacks, campaign, "ui", "ui", depends_on=("contract",))
    first = claim(hacks)
    assert first["id"] == api["id"]
    assert claim(hacks) is None
    hacks.finish_task(first["id"], "ready")
    assert claim(hacks) is None
    hacks.finish_task(first["id"], "merged")
    assert claim(hacks)["lane"] == "ui"


def test_checked_dependencies_require_a_known_integrated_result(state):
    _, hacks = state
    campaign = activate(hacks, start(hacks))
    lanes = {"api": ["api/**"], "ui": ["ui/**"]}
    board = "## api\n- [x] [contract] Define API\n## ui\n- [ ] [screen] Use API <!-- depends: contract -->"
    with pytest.raises(HackContractError, match="uncheck the dependency"):
        hacks.sync_board(campaign["id"], board, lanes)
    assert len(hacks.list_tasks(campaign["id"])) == 1
    contract = lane(hacks, campaign, "contract")
    hacks.finish_task(contract["id"], "merged")
    hacks.sync_board(campaign["id"], board, lanes)
    assert claim(hacks)["key"] == "screen"


def test_global_reservation_allows_controlled_work_to_use_reserved_slot(state):
    store, hacks = state
    campaign = activate(hacks, start(hacks))
    lane(hacks, campaign)
    lane(hacks, campaign, "ui", "ui")
    assert claim(hacks, global_limit=2)
    assert claim(hacks, global_limit=2) is None
    urgent = issue_job(store, "solo/urgent")
    assert store.claim_next("urgent", 60, 2, {"codex": 8}).id == urgent.id
    assert claim(hacks, global_limit=2) is None
    store.transition(urgent.id, JobState.PR_OPEN)
    assert claim(hacks, global_limit=3)


def test_provider_capacity_and_backoff_share_normal_lease_pool(state):
    store, hacks = state
    campaign = activate(hacks, start(hacks))
    lane(hacks, campaign)
    urgent = issue_job(store, "solo/urgent")
    store.claim_next("urgent", 60, 8, {"codex": 8})
    assert claim(hacks, provider_limit=1) is None
    store.transition(urgent.id, JobState.PR_OPEN)
    with store.transaction() as connection:
        connection.execute("INSERT INTO provider_backoff VALUES ('codex','wait',?,'now')", ((datetime.now(UTC) + timedelta(minutes=2)).isoformat(),))
    assert claim(hacks) is None


def test_board_edits_freeze_claims_reorder_idle_and_require_explicit_changed_retry(state):
    _, hacks = state
    campaign = activate(hacks, start(hacks))
    lanes = {"api": ["api/**"], "ui": ["ui/**"]}
    board = "## api\n- [ ] [one] First\n- [ ] [two] Second\n## ui\n- [ ] [three] Third"
    hacks.sync_board(campaign["id"], board, lanes)
    running = claim(hacks)
    edited = "## api\n- [ ] [one] Changed while running\n## ui\n- [ ] [three] Third revised"
    hacks.sync_board(campaign["id"], edited, lanes)
    by_key = {task["key"]: task for task in hacks.list_tasks(campaign["id"])}
    assert by_key["one"]["prompt"] == "First"
    assert by_key["two"]["state"] == "canceled"
    assert by_key["three"]["prompt"] == "Third revised"
    hacks.finish_task(running["id"], "blocked", note="Needs director answer")
    hacks.sync_board(campaign["id"], board, lanes)
    assert hacks.get_task(running["id"])["state"] == "blocked"
    hacks.sync_board(campaign["id"], edited, lanes)
    assert hacks.get_task(running["id"])["state"] == "queued"


def test_running_lane_footprints_and_cross_lane_collisions_are_frozen(state):
    _, hacks = state
    campaign = activate(hacks, start(hacks))
    lane(hacks, campaign)
    claim(hacks)
    with pytest.raises(HackContractError, match="frozen"):
        hacks.sync_board(campaign["id"], "## api\n- [ ] Task", {"api": ["other/**"]})
    with pytest.raises(HackContractError, match="overlapping"):
        hacks.enqueue_task(campaign["id"], "collision", "ui", "Steal api", ["api/**"])


def test_deadline_stops_claim_and_renewal_but_keeps_busy_fence_until_cancel(state):
    store, hacks = state
    campaign = activate(hacks, start(hacks))
    lane(hacks, campaign)
    task = claim(hacks)
    deadline = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    hacks.update_campaign(campaign["id"], expires_at=deadline)
    assert not hacks.renew_lease(task["id"], "worker", 60)
    assert claim(hacks) is None
    with pytest.raises(StoreError, match="execution leases"):
        hacks.update_campaign(campaign["id"], state="completed")
    assert store.hack_active("solo/project")
    hacks.finish_task(task["id"], "canceled")
    hacks.update_campaign(campaign["id"], state="completed")
    assert not store.repository_has_lease("solo/project")


def test_lease_never_extends_beyond_deadline_and_wrong_owner_cannot_renew(state):
    _, hacks = state
    campaign = activate(hacks, start(hacks))
    deadline = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
    hacks.update_campaign(campaign["id"], expires_at=deadline)
    lane(hacks, campaign)
    task = claim(hacks)
    assert task["lease_expires_at"] == deadline
    assert not hacks.renew_lease(task["id"], "other", 900)
    assert hacks.renew_lease(task["id"], "worker", 900)
    assert hacks.get_task(task["id"])["lease_expires_at"] == deadline


def test_expired_recovery_is_visible_and_never_automatic_retry(state):
    store, hacks = state
    campaign = activate(hacks, start(hacks))
    lane(hacks, campaign)
    task = claim(hacks)
    with store.transaction() as connection:
        connection.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00'")
    assert [item["id"] for item in hacks.expired_tasks()] == [task["id"]]
    assert hacks.get_task(task["id"])["state"] == "running"
    assert hacks.recover_expired_tasks(set()) == []
    assert hacks.recover_expired_tasks({task["id"]}) == [task["id"]]
    assert hacks.get_task(task["id"])["state"] == "blocked"
    assert claim(hacks) is None
    assert not store.repository_has_lease("solo/project")


@pytest.mark.parametrize("same_owner", [False, True])
def test_stale_worker_cannot_overwrite_or_release_reclaimed_attempt(state, same_owner):
    store, hacks = state
    campaign = activate(hacks, start(hacks))
    lanes = {"api": ["api/**"]}
    hacks.sync_board(campaign["id"], "## api\n- [ ] [one] First", lanes)
    first = claim(hacks)
    with store.transaction() as connection:
        connection.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00'")
    hacks.recover_expired_tasks({first["id"]})
    hacks.sync_board(campaign["id"], "## api\n- [ ] [one] First corrected", lanes)
    owner = "worker" if same_owner else "another-worker"
    current = hacks.claim_next(owner, 60, 8, {"codex": 8})
    assert current["attempt"] == 2
    guard = {"expected_owner": first["lease_owner"], "expected_attempt": first["attempt"]}
    with pytest.raises(StoreError, match="no longer belongs"):
        hacks.finish_task(first["id"], "blocked", note="Late old worker failure", **guard)
    with pytest.raises(StoreError, match="no longer belongs"):
        hacks.update_task(first["id"], conversation_id="old-conversation", **guard)
    active = hacks.get_task(current["id"])
    assert active["state"] == "running"
    assert active["lease_owner"] == owner
    assert active["conversation_id"] is None
    assert store.repository_has_lease("solo/project")
    hacks.finish_task(current["id"], "ready", expected_owner=owner, expected_attempt=current["attempt"])
    assert not store.repository_has_lease("solo/project")


def test_dispatcher_board_commit_and_source_hash_are_fenced_atomically(state):
    _, hacks = state
    campaign = activate(hacks, start(hacks))
    dispatch = hacks.enqueue_task(campaign["id"], "dispatch", "dispatcher", "Split board", ["hack/dispatch.json"], kind="dispatcher")
    claimed = claim(hacks)
    guard = {"expected_task_id": dispatch["id"], "expected_owner": claimed["lease_owner"], "expected_attempt": claimed["attempt"]}
    hacks.sync_board(campaign["id"], "## api\n- [ ] [one] First", {"api": ["api/**"]}, source_hash="original-source-hash", **guard)
    assert hacks.get_campaign(campaign["id"])["board_hash"] == "original-source-hash"
    hacks.finish_task(dispatch["id"], "canceled")
    with pytest.raises(StoreError, match="no longer belongs"):
        hacks.sync_board(campaign["id"], "## api\n- [ ] [one] Stale edit", {"api": ["api/**"]}, **guard)
    assert next(task for task in hacks.list_tasks(campaign["id"]) if task["key"] == "one")["prompt"] == "First"
    assert hacks.get_campaign(campaign["id"])["board_hash"] == "original-source-hash"


def test_stop_drains_running_lanes_then_solo_polish(state):
    _, hacks = state
    campaign = activate(hacks, start(hacks))
    lane(hacks, campaign)
    lane(hacks, campaign, "ui", "ui")
    running = claim(hacks)
    stopped = hacks.request_stop("solo/project")
    assert stopped["state"] == "draining"
    assert next(task for task in hacks.list_tasks(campaign["id"]) if task["key"] == "ui")["state"] == "canceled"
    hacks.enqueue_task(campaign["id"], "polish", "polish", "Polish demo", ["**"], kind="polish")
    assert claim(hacks) is None
    hacks.finish_task(running["id"], "ready")
    assert claim(hacks) is None
    hacks.finish_task(running["id"], "merged")
    assert claim(hacks)["kind"] == "polish"


def test_campaign_task_budget_and_dispatcher_solo_barrier(state):
    _, hacks = state
    campaign = activate(hacks, start(hacks, max_tasks=2))
    lane(hacks, campaign)
    lane(hacks, campaign, "ui", "ui")
    with pytest.raises(StoreError, match="budget"):
        lane(hacks, campaign, "infra", "infra")
    dispatch = hacks.enqueue_task(campaign["id"], "dispatch", "dispatcher", "Split board", ["hack/dispatch.json"], kind="dispatcher")
    assert claim(hacks)["id"] == dispatch["id"]
    assert claim(hacks) is None
    hacks.finish_task(dispatch["id"], "merged")
    assert claim(hacks)["kind"] == "lane"
    assert claim(hacks) is None


def test_board_dispatcher_can_steer_running_campaign_but_freezes_unclaimed_lanes(state):
    _, hacks = state
    campaign = activate(hacks, start(hacks))
    api = lane(hacks, campaign)
    ui = lane(hacks, campaign, "ui", "ui")
    assert claim(hacks)["id"] == api["id"]
    dispatch = hacks.enqueue_task(campaign["id"], "dispatch", "dispatcher", "Revise board", ["hack/dispatch.json"], kind="dispatcher")
    assert claim(hacks)["id"] == dispatch["id"]
    assert claim(hacks) is None
    hacks.finish_task(dispatch["id"], "merged")
    assert claim(hacks)["id"] == ui["id"]


def test_director_retries_still_consume_campaign_budget(state):
    _, hacks = state
    campaign = activate(hacks, start(hacks, max_tasks=1))
    hacks.sync_board(campaign["id"], "## api\n- [ ] [one] First", {"api": ["api/**"]})
    task = claim(hacks)
    hacks.finish_task(task["id"], "blocked")
    hacks.sync_board(campaign["id"], "## api\n- [ ] [one] First corrected", {"api": ["api/**"]})
    assert hacks.get_task(task["id"])["state"] == "queued"
    assert claim(hacks) is None


def test_new_claim_clears_previous_attempt_artifacts_and_conversation(state):
    _, hacks = state
    campaign = activate(hacks, start(hacks))
    lanes = {"api": ["api/**"]}
    hacks.sync_board(campaign["id"], "## api\n- [ ] [one] First", lanes)
    first = claim(hacks)
    hacks.update_task(first["id"], prepared_commit="prepared", prepared_base_commit="prepared-base", implementation_commit="implementation")
    hacks.finish_task(first["id"], "blocked", result_commit="result", conversation_id="old-conversation", session_id="old-session", error="failure")
    hacks.sync_board(campaign["id"], "## api\n- [ ] [one] Corrected", lanes)
    current = claim(hacks)
    assert current["id"] == first["id"] and current["attempt"] == 2
    for field in ("result_commit", "prepared_commit", "prepared_base_commit", "implementation_commit", "conversation_id", "session_id"):
        assert current[field] is None
    assert current["error"] == current["note"] == ""


def test_concurrent_claims_cannot_overspend_last_campaign_slot(state):
    _, hacks = state
    campaign = activate(hacks, start(hacks, max_tasks=2))
    lane(hacks, campaign)
    lane(hacks, campaign, "ui", "ui")
    dispatch = hacks.enqueue_task(campaign["id"], "dispatch", "dispatcher", "Split board", ["hack/dispatch.json"], kind="dispatcher")
    assert claim(hacks)["id"] == dispatch["id"]
    hacks.finish_task(dispatch["id"], "merged")
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _: claim(hacks), range(2)))
    assert sum(task is not None for task in claims) == 1
    assert sum(task["attempt"] for task in hacks.list_tasks(campaign["id"])) == 2


def test_edited_queued_footprints_cannot_overlap_manual_tasks_and_rollback(state):
    _, hacks = state
    campaign = activate(hacks, start(hacks))
    manual = lane(hacks, campaign)
    board = "## ui\n- [ ] [screen] UI work"
    hacks.sync_board(campaign["id"], board, {"ui": ["ui/**"]})
    with pytest.raises(HackContractError, match="overlapping"):
        hacks.sync_board(campaign["id"], board, {"ui": ["api/**"]})
    assert next(task for task in hacks.list_tasks(campaign["id"]) if task["key"] == "screen")["footprint"] == ["ui/**"]
    assert hacks.get_task(manual["id"])["footprint"] == ["api/**"]


def test_claims_can_fill_capacity_when_controlled_work_already_uses_reservation(state):
    store, hacks = state
    campaign = activate(hacks, start(hacks))
    lane(hacks, campaign)
    urgent = issue_job(store, "solo/urgent")
    assert store.claim_next("urgent", 60, 2, {"codex": 8}).id == urgent.id
    assert claim(hacks, global_limit=2)


def test_prepared_integration_checkpoint_survives_restart(state):
    store, hacks = state
    campaign = activate(hacks, start(hacks))
    task = lane(hacks, campaign)
    hacks.update_task(task["id"], prepared_commit="c" * 40, prepared_base_commit="b" * 40, implementation_commit="d" * 40)
    restored = HackStore(Store(store.path)).get_task(task["id"])
    assert restored["prepared_commit"] == "c" * 40
    assert restored["prepared_base_commit"] == "b" * 40
    assert restored["implementation_commit"] == "d" * 40


def test_board_observation_generation_survives_restart(state):
    store, hacks = state
    campaign = start(hacks)
    assert campaign["board_revision"] == 0
    hacks.update_campaign(campaign["id"], observed_board_hash="board-A", board_revision=3)
    restored = HackStore(Store(store.path)).get_campaign(campaign["id"])
    assert restored["observed_board_hash"] == "board-A"
    assert restored["board_revision"] == 3


def test_integrator_operation_lock_excludes_other_owners_and_restarts(state):
    store, hacks = state
    campaign = start(hacks)
    assert hacks.acquire_operation(campaign["id"], "one", 60)
    restarted = HackStore(Store(store.path))
    assert not restarted.acquire_operation(campaign["id"], "two", 60)
    assert not restarted.renew_operation(campaign["id"], "two", 60)
    assert hacks.renew_operation(campaign["id"], "one", 60)
    hacks.release_operation(campaign["id"], "one")
    assert restarted.acquire_operation(campaign["id"], "two", 60)
