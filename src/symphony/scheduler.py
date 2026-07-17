from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor

from .config import Config
from .coordinator import Coordinator
from .models import Job
from .store import Store


class Scheduler:
    """Work-conserving, fair-enough scheduler backed by transactional SQLite leases."""

    def __init__(self, config: Config, store: Store, coordinator: Coordinator):
        self.config = config
        self.store = store
        self.coordinator = coordinator
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=config.scheduler.global_concurrency,
            thread_name_prefix="symphony-worker",
        )
        self._futures: dict[Future[Job], str] = {}
        self._lock = threading.Lock()
        self._last_reconcile = 0.0

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
        now = time.monotonic()
        if reconcile or now - self._last_reconcile >= self.config.scheduler.reconcile_seconds:
            self.coordinator.reconcile()
            self._last_reconcile = now
        started = 0
        while True:
            with self._lock:
                available = self.config.scheduler.global_concurrency - len(self._futures)
            if available <= 0:
                break
            job = self.store.claim_next(
                self.owner,
                self.config.scheduler.lease_seconds,
                self.config.scheduler.global_concurrency,
                self.config.scheduler.provider_concurrency,
            )
            if job is None:
                break
            future = self._executor.submit(self.coordinator.run_claimed, job)
            with self._lock:
                self._futures[future] = job.id
            started += 1
        return started

    def _loop(self) -> None:
        self.coordinator.recover_expired_leases()
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.config.scheduler.poll_seconds)
