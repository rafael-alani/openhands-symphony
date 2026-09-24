from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor

from .config import Config
from .coordinator import Coordinator
from .ideas_coordinator import IdeasCoordinator
from .models import IdeaRun, Job
from .store import Store


class Scheduler:
    """Work-conserving, fair-enough scheduler backed by transactional SQLite leases."""

    def __init__(
        self,
        config: Config,
        store: Store,
        coordinator: Coordinator,
        ideas: IdeasCoordinator | None = None,
    ):
        self.config = config
        self.store = store
        self.coordinator = coordinator
        self.ideas = ideas
        self.hack = getattr(coordinator, "hack", None)
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=config.scheduler.global_concurrency,
            thread_name_prefix="symphony-worker",
        )
        self._maintenance = ThreadPoolExecutor(max_workers=1, thread_name_prefix="symphony-integrator")
        self._hack_reconcile: Future | None = None
        self._futures: dict[Future, str] = {}
        self._lock = threading.Lock()
        self._last_reconcile = 0.0
        self._prefer_ideas = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="symphony-scheduler", daemon=True)
        self._thread.start()

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._executor.shutdown(wait=wait, cancel_futures=False)
        self._maintenance.shutdown(wait=wait, cancel_futures=False)

    def _clean_futures(self) -> None:
        with self._lock:
            finished = [future for future in self._futures if future.done()]
            for future in finished:
                try:
                    future.result()
                except Exception:
                    # Coordinator is responsible for recording/reporting job-scoped failures.
                    pass
                self._futures.pop(future, None)

    def tick(self, *, reconcile: bool = False) -> int:
        self._clean_futures()
        if self.hack:
            # Build/boot checks may take minutes. Keep them off the scheduling
            # thread so the reserved GitHub capacity remains usable. Task
            # heartbeats and transactional claims enforce the hard deadline
            # even while a slow integration is still finishing.
            with self._lock:
                if self._hack_reconcile is None or self._hack_reconcile.done():
                    self._hack_reconcile = self._maintenance.submit(self.hack.reconcile)
        if self.coordinator.vault is not None:
            self.coordinator.refresh_vault()
            self.config = self.coordinator.config
            reconcile = True
        now = time.monotonic()
        if reconcile or now - self._last_reconcile >= self.config.scheduler.reconcile_seconds:
            self.coordinator.reconcile()
            if self.ideas:
                self.ideas.reconcile()
            self._last_reconcile = now
        started = 0
        while True:
            with self._lock:
                available = self.config.scheduler.global_concurrency - len(self._futures)
            if available <= 0:
                break
            claimed: Job | IdeaRun | dict | None = None
            selected_kind = "issue"
            campaigns = bool(self.hack and self.hack.state.list_campaigns())
            if campaigns:
                # Hack claims enforce the reserved controlled-work capacity
                # transactionally. Give the remaining burst slots to campaigns
                # before a large issue backlog can consume every vacancy.
                order = ("hack", "issue", "idea")
            else:
                order = ("idea", "issue") if self._prefer_ideas and self.ideas else ("issue", "idea")
            for kind in order:
                if kind == "idea" and self.ideas:
                    claimed = self.store.claim_next_idea(
                        self.owner,
                        self.config.scheduler.lease_seconds,
                        self.config.scheduler.global_concurrency,
                        self.config.scheduler.provider_concurrency,
                        allowed_repositories=self.config.ideas.repositories,
                    )
                elif kind == "hack" and self.hack:
                    claimed = self.hack.state.claim_next(
                        self.owner,
                        self.config.scheduler.lease_seconds,
                        self.config.scheduler.global_concurrency,
                        self.config.scheduler.provider_concurrency,
                        allowed_repositories=self.config.hack.repositories if self.config.hack.enabled else (),
                        reserve_slots=self.config.hack.reserve_slots,
                    )
                elif kind == "issue":
                    claimed = self.store.claim_next(
                        self.owner,
                        self.config.scheduler.lease_seconds,
                        self.config.scheduler.global_concurrency,
                        self.config.scheduler.provider_concurrency,
                        allowed_repositories=self.config.github.allowed_repositories,
                    )
                if claimed is not None:
                    selected_kind = kind
                    break
            if claimed is None:
                break
            if selected_kind == "hack":
                target = self.hack.run_claimed
            elif selected_kind == "idea":
                target = self.ideas.run_claimed
            else:
                target = self.coordinator.run_claimed
            future = self._executor.submit(target, claimed)
            with self._lock:
                self._futures[future] = claimed["id"] if isinstance(claimed, dict) else claimed.id
            self._prefer_ideas = selected_kind != "idea"
            started += 1
        return started

    def _loop(self) -> None:
        self.coordinator.recover_expired_leases()
        if self.ideas:
            self.ideas.recover_expired_leases()
        if self.hack:
            self.hack.recover_expired_leases()
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.config.scheduler.poll_seconds)
