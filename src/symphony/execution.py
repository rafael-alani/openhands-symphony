from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class ExecutionRef:
    """Source-neutral identity and capacity metadata for one runnable unit."""

    kind: str
    id: str
    repository: str
    concurrency_key: str
    provider: str


@dataclass(frozen=True)
class Lease:
    execution: ExecutionRef
    owner: str
    expires_at: str
    heartbeat_at: str


class RepositoryLeases:
    """Transactional repository leases shared by every intake source."""

    @staticmethod
    def active_count(connection: sqlite3.Connection, now: str) -> int:
        return int(connection.execute("SELECT COUNT(*) FROM leases WHERE expires_at>?", (now,)).fetchone()[0])

    @staticmethod
    def provider_counts(connection: sqlite3.Connection, now: str) -> dict[str, int]:
        return {
            str(row["provider"]): int(row["count"])
            for row in connection.execute(
                "SELECT provider, COUNT(*) AS count FROM leases WHERE expires_at>? GROUP BY provider",
                (now,),
            ).fetchall()
        }

    @staticmethod
    def claim(
        connection: sqlite3.Connection,
        execution: ExecutionRef,
        owner: str,
        lease_seconds: int,
        *,
        now: datetime | None = None,
    ) -> Lease:
        now_dt = now or datetime.now(UTC)
        heartbeat = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        connection.execute(
            """
            INSERT INTO leases(
                concurrency_key, run_kind, run_id, repository, provider, owner, expires_at, heartbeat_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution.concurrency_key,
                execution.kind,
                execution.id,
                execution.repository,
                execution.provider,
                owner,
                expires,
                heartbeat,
            ),
        )
        return Lease(execution, owner, expires, heartbeat)

    @staticmethod
    def renew(
        connection: sqlite3.Connection,
        kind: str,
        run_id: str,
        owner: str,
        lease_seconds: int,
        *,
        now: datetime | None = None,
    ) -> str | None:
        now_dt = now or datetime.now(UTC)
        heartbeat = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        cursor = connection.execute(
            """
            UPDATE leases SET expires_at=?, heartbeat_at=?
            WHERE run_kind=? AND run_id=? AND owner=? AND expires_at>?
            """,
            (expires, heartbeat, kind, run_id, owner, heartbeat),
        )
        return expires if cursor.rowcount == 1 else None

    @staticmethod
    def release(connection: sqlite3.Connection, kind: str, run_id: str) -> None:
        connection.execute("DELETE FROM leases WHERE run_kind=? AND run_id=?", (kind, run_id))


class ProviderSlots:
    """In-process provider slots for turns that happen inside claimed runs."""

    def __init__(self, limits: dict[str, int], providers: set[str]):
        self._slots = {
            name: threading.BoundedSemaphore(limits.get(name, 1))
            for name in providers
            if limits.get(name, 1) > 0
        }

    @contextmanager
    def acquire(self, provider: str, timeout_seconds: float, heartbeat) -> Iterator[bool]:
        semaphore = self._slots.get(provider)
        if semaphore is None:
            yield False
            return
        while not semaphore.acquire(timeout=timeout_seconds):
            heartbeat()
        try:
            yield True
        finally:
            semaphore.release()
