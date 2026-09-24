from __future__ import annotations

from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace

from conftest import make_config

from symphony.config import HackConfig
from symphony.scheduler import Scheduler


def test_campaign_scheduler_fills_burst_then_reserved_issues_then_ideas(tmp_path):
    config = replace(make_config(tmp_path), hack=HackConfig(enabled=True, repositories=("solo/hack",)))
    events = []
    issue = SimpleNamespace(id="issue-1")
    idea = SimpleNamespace(id="idea-1")
    task = {"id": "hack-1"}
    queued = {"issue": [issue], "idea": [idea], "hack": [task]}

    def claim(kind, *args, **kwargs):
        events.append(("claim", kind))
        if kind == "hack":
            assert kwargs["reserve_slots"] == 1
            assert kwargs["allowed_repositories"] == ("solo/hack",)
        return queued[kind].pop(0) if queued[kind] else None

    store = SimpleNamespace(
        claim_next=lambda *a, **kw: claim("issue", *a, **kw),
        claim_next_idea=lambda *a, **kw: claim("idea", *a, **kw),
    )
    hack = SimpleNamespace(
        state=SimpleNamespace(
            list_campaigns=lambda: [{"id": "campaign"}],
            claim_next=lambda *a, **kw: claim("hack", *a, **kw),
        ),
        reconcile=lambda: events.append(("reconcile", "hack")),
        run_claimed=lambda _: None,
    )
    coordinator = SimpleNamespace(
        hack=hack, vault=None, reconcile=lambda: events.append(("reconcile", "issue")), run_claimed=lambda _: None,
    )
    ideas = SimpleNamespace(reconcile=lambda: events.append(("reconcile", "idea")), run_claimed=lambda _: None)
    scheduler = Scheduler(config, store, coordinator, ideas)
    scheduler._executor.shutdown()
    scheduler._maintenance.shutdown()

    def maintain(target):
        future = Future()
        future.set_result(target())
        return future

    scheduler._maintenance = SimpleNamespace(submit=maintain)

    def submit(target, value):
        events.append(("start", value["id"] if isinstance(value, dict) else value.id))
        return Future()

    scheduler._executor = SimpleNamespace(submit=submit)
    assert scheduler.tick(reconcile=True) == 3
    assert events[0] == ("reconcile", "hack")
    assert [value for action, value in events if action == "start"] == ["hack-1", "issue-1", "idea-1"]
    assert scheduler.tick() == 0
    assert events[-1] == ("reconcile", "hack")  # Deadlines run even with every executor slot occupied.


def test_slow_integrator_does_not_block_normal_scheduler_or_start_twice(tmp_path):
    import threading

    entered = threading.Event()
    release = threading.Event()
    calls = []

    def reconcile_hack():
        calls.append("integrator")
        entered.set()
        release.wait(5)

    hack = SimpleNamespace(
        state=SimpleNamespace(list_campaigns=lambda: []), reconcile=reconcile_hack,
    )
    coordinator = SimpleNamespace(hack=hack, vault=None, reconcile=lambda: calls.append("issues"))
    store = SimpleNamespace(claim_next=lambda *a, **kw: calls.append("claim") or None)
    scheduler = Scheduler(make_config(tmp_path), store, coordinator)
    try:
        assert scheduler.tick(reconcile=True) == 0
        assert entered.wait(1)
        assert "claim" in calls
        assert not scheduler._hack_reconcile.done()
        assert scheduler.tick() == 0
        assert calls.count("integrator") == 1
    finally:
        release.set()
        scheduler.stop()


def test_issue_backlog_cannot_consume_campaign_burst_allocation(tmp_path):
    from conftest import FakeGitHub, issue

    from symphony.coordinator import Coordinator
    from symphony.hack_store import HackStore
    from symphony.providers.fake import FakeProvider
    from symphony.store import Store

    config = make_config(tmp_path, repositories=("solo/urgent", "solo/hack"))
    config = replace(
        config,
        scheduler=replace(config.scheduler, global_concurrency=4, provider_concurrency={"codex": 4}),
        hack=HackConfig(enabled=True, repositories=("solo/hack",)),
    )
    store = Store(config.service.state_dir / "state.db")
    issues = [issue(repository="solo/urgent", number=n) for n in range(1, 4)]
    coordinator = Coordinator(config, store, FakeGitHub(issues), {"codex": FakeProvider("codex")})
    for snapshot in issues:
        coordinator.enqueue(snapshot)
    state = HackStore(store)
    campaign = state.start_campaign("solo/hack", "github", "main", "a" * 40, "codex", 1)
    state.finish_task(state.list_tasks(campaign["id"])[0]["id"], "merged")
    state.update_campaign(campaign["id"], state="active")
    for lane in ("api", "ui", "infra"):
        state.enqueue_task(campaign["id"], lane, lane, "Implement task", [f"{lane}/**"])
    coordinator.hack = SimpleNamespace(state=state, reconcile=lambda: None, run_claimed=lambda _: None)
    scheduler = Scheduler(config, store, coordinator)
    scheduler._executor.shutdown()
    scheduler._maintenance.shutdown()
    started = []

    def submit(target, value):
        started.append(value)
        return Future()

    scheduler._executor = SimpleNamespace(submit=submit)
    scheduler._maintenance = SimpleNamespace(submit=lambda target: Future())
    assert scheduler.tick() == 4
    assert sum(isinstance(task, dict) for task in started) == 3
    assert sum(not isinstance(task, dict) for task in started) == 1
