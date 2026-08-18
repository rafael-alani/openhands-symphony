from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .execution import ExecutionRef, RepositoryLeases
from .models import (
    ACTIVE_STATES,
    IDEA_ACTIVE_STATES,
    IdeaProject,
    IdeaRun,
    IdeaRunState,
    IdeaSnapshot,
    IssueSnapshot,
    Job,
    JobState,
    ValidationResult,
    utcnow,
)

SCHEMA_VERSION = 5
ISSUE_RUN_KIND = "github-issue"
IDEA_RUN_KIND = "idea-spec"

ALLOWED_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.QUEUED: {
        JobState.RUNNING,
        JobState.NEEDS_GUIDANCE,
        JobState.PR_OPEN,
        JobState.BLOCKED,
        JobState.FAILED,
        JobState.CANCELED,
    },
    JobState.RUNNING: {
        JobState.QUEUED,
        JobState.NEEDS_GUIDANCE,
        JobState.PR_OPEN,
        JobState.BLOCKED,
        JobState.FAILED,
        JobState.CANCELED,
    },
    JobState.NEEDS_GUIDANCE: {JobState.QUEUED, JobState.CANCELED, JobState.FAILED},
    JobState.PR_OPEN: {JobState.QUEUED, JobState.REVIEWING, JobState.DONE, JobState.CANCELED},
    JobState.REVIEWING: {JobState.PR_OPEN, JobState.QUEUED, JobState.DONE, JobState.FAILED, JobState.CANCELED},
    JobState.BLOCKED: {JobState.QUEUED, JobState.CANCELED},
    JobState.FAILED: {JobState.QUEUED, JobState.CANCELED},
    JobState.CANCELED: {JobState.QUEUED},
    JobState.DONE: set(),
}

IDEA_ALLOWED_TRANSITIONS: dict[IdeaRunState, set[IdeaRunState]] = {
    IdeaRunState.DISCOVERED: {IdeaRunState.QUEUED, IdeaRunState.FAILED, IdeaRunState.SUPERSEDED},
    IdeaRunState.QUEUED: {IdeaRunState.RUNNING, IdeaRunState.FAILED, IdeaRunState.SUPERSEDED},
    IdeaRunState.RUNNING: {
        IdeaRunState.QUEUED,
        IdeaRunState.PUBLISHED,
        IdeaRunState.QUESTION,
        IdeaRunState.FAILED,
        IdeaRunState.SUPERSEDED,
    },
    IdeaRunState.PUBLISHED: set(),
    IdeaRunState.QUESTION: set(),
    IdeaRunState.FAILED: {IdeaRunState.QUEUED},
    IdeaRunState.SUPERSEDED: set(),
}


class StoreError(RuntimeError):
    pass


class Store:
    """Transactional SQLite state. One durable job exists per repository/issue."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            if connection.in_transaction:
                connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self.connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise StoreError(f"database schema {version} is newer than supported schema {SCHEMA_VERSION}")
            if version == 0:
                connection.executescript(
                    """
                    CREATE TABLE jobs (
                        id TEXT PRIMARY KEY,
                        repository TEXT NOT NULL,
                        issue_number INTEGER NOT NULL,
                        snapshot_hash TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        snapshot_json TEXT NOT NULL,
                        implementation_provider TEXT NOT NULL,
                        review_provider TEXT,
                        review_required INTEGER NOT NULL DEFAULT 0,
                        state TEXT NOT NULL,
                        attempt INTEGER NOT NULL DEFAULT 0,
                        branch TEXT NOT NULL,
                        worktree TEXT,
                        concurrency_key TEXT NOT NULL,
                        conversation_id TEXT,
                        session_id TEXT,
                        review_conversation_id TEXT,
                        review_session_id TEXT,
                        status_comment_id INTEGER,
                        pr_number INTEGER,
                        pr_url TEXT,
                        phase TEXT NOT NULL DEFAULT 'intake',
                        validation_summary TEXT NOT NULL DEFAULT '',
                        actionable_message TEXT NOT NULL DEFAULT '',
                        terminal_outcome TEXT,
                        terminal_reason TEXT,
                        lease_owner TEXT,
                        lease_expires_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        heartbeat_at TEXT,
                        finished_at TEXT,
                        retry_requested INTEGER NOT NULL DEFAULT 0,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        pause_requested INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(repository, issue_number)
                    );
                    CREATE INDEX jobs_queue_idx ON jobs(state, retry_requested DESC, created_at);
                    CREATE INDEX jobs_concurrency_idx ON jobs(concurrency_key, state);
                    CREATE INDEX jobs_provider_idx ON jobs(implementation_provider, state);

                    CREATE TABLE leases (
                        concurrency_key TEXT PRIMARY KEY,
                        run_kind TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        repository TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        owner TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        heartbeat_at TEXT NOT NULL,
                        UNIQUE(run_kind, run_id)
                    );
                    CREATE INDEX leases_provider_idx ON leases(provider, expires_at);

                    CREATE TABLE deliveries (
                        delivery_id TEXT PRIMARY KEY,
                        event_name TEXT NOT NULL,
                        repository TEXT,
                        issue_number INTEGER,
                        received_at TEXT NOT NULL,
                        payload_hash TEXT NOT NULL
                    );

                    CREATE TABLE job_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        at TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        detail_json TEXT NOT NULL
                    );

                    CREATE TABLE validation_results (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        attempt INTEGER NOT NULL,
                        command_json TEXT NOT NULL,
                        exit_code INTEGER,
                        started_at TEXT NOT NULL,
                        finished_at TEXT NOT NULL,
                        output TEXT NOT NULL,
                        timed_out INTEGER NOT NULL DEFAULT 0
                    );

                    CREATE TABLE provider_backoff (
                        provider TEXT PRIMARY KEY,
                        reason TEXT NOT NULL,
                        until_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE operation_locks (
                        name TEXT PRIMARY KEY,
                        owner TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    );

                    CREATE TABLE idea_projects (
                        repository TEXT PRIMARY KEY,
                        latest_observed_spec_hash TEXT,
                        latest_completed_spec_hash TEXT,
                        last_good_preview_commit TEXT,
                        preview_state TEXT NOT NULL DEFAULT 'unknown',
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE idea_runs (
                        id TEXT PRIMARY KEY,
                        repository TEXT NOT NULL REFERENCES idea_projects(repository) ON DELETE CASCADE,
                        spec_hash TEXT NOT NULL,
                        spec_content BLOB NOT NULL,
                        runtime_content BLOB NOT NULL,
                        previous_progress BLOB NOT NULL,
                        base_commit TEXT NOT NULL,
                        default_branch TEXT NOT NULL,
                        implementation_provider TEXT NOT NULL,
                        state TEXT NOT NULL,
                        attempt INTEGER NOT NULL DEFAULT 0,
                        worktree TEXT,
                        conversation_id TEXT,
                        session_id TEXT,
                        phase TEXT NOT NULL DEFAULT 'discovered',
                        validation_summary TEXT NOT NULL DEFAULT '',
                        question TEXT NOT NULL DEFAULT '',
                        published_commit TEXT,
                        lease_owner TEXT,
                        lease_expires_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        heartbeat_at TEXT,
                        finished_at TEXT,
                        retry_requested INTEGER NOT NULL DEFAULT 0,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(repository, spec_hash)
                    );
                    CREATE INDEX idea_runs_queue_idx ON idea_runs(state, retry_requested DESC, created_at);
                    CREATE INDEX idea_runs_provider_idx ON idea_runs(implementation_provider, state);

                    CREATE TABLE idea_run_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL REFERENCES idea_runs(id) ON DELETE CASCADE,
                        at TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        detail_json TEXT NOT NULL
                    );

                    CREATE TABLE idea_validation_results (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL REFERENCES idea_runs(id) ON DELETE CASCADE,
                        attempt INTEGER NOT NULL,
                        command_json TEXT NOT NULL,
                        exit_code INTEGER,
                        started_at TEXT NOT NULL,
                        finished_at TEXT NOT NULL,
                        output TEXT NOT NULL,
                        timed_out INTEGER NOT NULL DEFAULT 0
                    );
                    """
                )
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            else:
                if version == 1:
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
                    if "pause_requested" not in columns:
                        connection.execute("ALTER TABLE jobs ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0")
                    if "review_conversation_id" not in columns:
                        connection.execute("ALTER TABLE jobs ADD COLUMN review_conversation_id TEXT")
                    if "review_session_id" not in columns:
                        connection.execute("ALTER TABLE jobs ADD COLUMN review_session_id TEXT")
                if version < 3:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS operation_locks (
                            name TEXT PRIMARY KEY,
                            owner TEXT NOT NULL,
                            expires_at TEXT NOT NULL
                        )
                        """
                    )
                if version < 4:
                    self._migrate_source_neutral_leases(connection)
                if version < 5:
                    self._migrate_ideas_tables(connection)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _migrate_source_neutral_leases(connection: sqlite3.Connection) -> None:
        """Rebuild v1-v3 job leases without losing an in-flight production claim."""
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(leases)").fetchall()}
        if "run_kind" in columns:
            connection.execute("CREATE INDEX IF NOT EXISTS leases_provider_idx ON leases(provider, expires_at)")
            return
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS leases_v4 (
                concurrency_key TEXT PRIMARY KEY,
                run_kind TEXT NOT NULL,
                run_id TEXT NOT NULL,
                repository TEXT NOT NULL,
                provider TEXT NOT NULL,
                owner TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                UNIQUE(run_kind, run_id)
            )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO leases_v4(
                concurrency_key, run_kind, run_id, repository, provider, owner, expires_at, heartbeat_at
            )
            SELECT l.concurrency_key, ?, l.job_id, j.repository, j.implementation_provider,
                   l.owner, l.expires_at, l.heartbeat_at
            FROM leases l JOIN jobs j ON j.id=l.job_id
            """,
            (ISSUE_RUN_KIND,),
        )
        connection.execute("DROP TABLE leases")
        connection.execute("ALTER TABLE leases_v4 RENAME TO leases")
        connection.execute("CREATE INDEX IF NOT EXISTS leases_provider_idx ON leases(provider, expires_at)")

    @staticmethod
    def _migrate_ideas_tables(connection: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS idea_projects (
                repository TEXT PRIMARY KEY,
                latest_observed_spec_hash TEXT,
                latest_completed_spec_hash TEXT,
                last_good_preview_commit TEXT,
                preview_state TEXT NOT NULL DEFAULT 'unknown',
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS idea_runs (
                id TEXT PRIMARY KEY,
                repository TEXT NOT NULL REFERENCES idea_projects(repository) ON DELETE CASCADE,
                spec_hash TEXT NOT NULL,
                spec_content BLOB NOT NULL,
                runtime_content BLOB NOT NULL,
                previous_progress BLOB NOT NULL,
                base_commit TEXT NOT NULL,
                default_branch TEXT NOT NULL,
                implementation_provider TEXT NOT NULL,
                state TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                worktree TEXT,
                conversation_id TEXT,
                session_id TEXT,
                phase TEXT NOT NULL DEFAULT 'discovered',
                validation_summary TEXT NOT NULL DEFAULT '',
                question TEXT NOT NULL DEFAULT '',
                published_commit TEXT,
                lease_owner TEXT,
                lease_expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                heartbeat_at TEXT,
                finished_at TEXT,
                retry_requested INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                UNIQUE(repository, spec_hash)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idea_runs_queue_idx ON idea_runs(state, retry_requested DESC, created_at)",
            "CREATE INDEX IF NOT EXISTS idea_runs_provider_idx ON idea_runs(implementation_provider, state)",
            """
            CREATE TABLE IF NOT EXISTS idea_run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES idea_runs(id) ON DELETE CASCADE,
                at TEXT NOT NULL,
                kind TEXT NOT NULL,
                detail_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS idea_validation_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES idea_runs(id) ON DELETE CASCADE,
                attempt INTEGER NOT NULL,
                command_json TEXT NOT NULL,
                exit_code INTEGER,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                output TEXT NOT NULL,
                timed_out INTEGER NOT NULL DEFAULT 0
            )
            """,
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _job(row: sqlite3.Row | None) -> Job | None:
        if row is None:
            return None
        data = dict(row)
        data["state"] = JobState(data["state"])
        for key in ("review_required", "retry_requested", "cancel_requested", "pause_requested"):
            data[key] = bool(data[key])
        return Job(**data)

    @staticmethod
    def _idea_run(row: sqlite3.Row | None) -> IdeaRun | None:
        if row is None:
            return None
        data = dict(row)
        data["state"] = IdeaRunState(data["state"])
        data["spec_content"] = bytes(data["spec_content"])
        data["runtime_content"] = bytes(data["runtime_content"])
        data["previous_progress"] = bytes(data["previous_progress"])
        for key in ("retry_requested", "cancel_requested"):
            data[key] = bool(data[key])
        return IdeaRun(**data)

    @staticmethod
    def _idea_project(row: sqlite3.Row | None) -> IdeaProject | None:
        return IdeaProject(**dict(row)) if row is not None else None

    def get_job(self, repository: str, issue_number: int) -> Job | None:
        with self.connect() as connection:
            return self._job(
                connection.execute(
                    "SELECT * FROM jobs WHERE repository=? AND issue_number=?", (repository, issue_number)
                ).fetchone()
            )

    def get_job_by_id(self, job_id: str) -> Job | None:
        with self.connect() as connection:
            return self._job(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list_jobs(self, states: set[JobState] | None = None) -> list[Job]:
        with self.connect() as connection:
            if states:
                placeholders = ",".join("?" for _ in states)
                rows = connection.execute(
                    f"SELECT * FROM jobs WHERE state IN ({placeholders}) ORDER BY created_at",
                    tuple(str(state) for state in states),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM jobs ORDER BY created_at").fetchall()
            return [self._job(row) for row in rows if row is not None]

    def get_idea_project(self, repository: str) -> IdeaProject | None:
        with self.connect() as connection:
            return self._idea_project(
                connection.execute("SELECT * FROM idea_projects WHERE repository=?", (repository,)).fetchone()
            )

    def list_idea_projects(self) -> list[IdeaProject]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM idea_projects ORDER BY repository").fetchall()
        return [self._idea_project(row) for row in rows if row is not None]

    def get_idea_run(self, repository: str, spec_hash: str) -> IdeaRun | None:
        with self.connect() as connection:
            return self._idea_run(
                connection.execute(
                    "SELECT * FROM idea_runs WHERE repository=? AND spec_hash=?", (repository, spec_hash)
                ).fetchone()
            )

    def get_idea_run_by_id(self, run_id: str) -> IdeaRun | None:
        with self.connect() as connection:
            return self._idea_run(connection.execute("SELECT * FROM idea_runs WHERE id=?", (run_id,)).fetchone())

    def list_idea_runs(self, states: set[IdeaRunState] | None = None) -> list[IdeaRun]:
        with self.connect() as connection:
            if states:
                placeholders = ",".join("?" for _ in states)
                rows = connection.execute(
                    f"SELECT * FROM idea_runs WHERE state IN ({placeholders}) ORDER BY created_at",
                    tuple(str(state) for state in states),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM idea_runs ORDER BY created_at").fetchall()
        return [self._idea_run(row) for row in rows if row is not None]

    def ensure_idea_run(
        self, snapshot: IdeaSnapshot, implementation_provider: str
    ) -> tuple[IdeaRun, bool, list[IdeaRun]]:
        now = utcnow()
        run_id = str(uuid.uuid4())
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO idea_projects(repository, latest_observed_spec_hash, preview_state, updated_at)
                VALUES (?, ?, 'unknown', ?)
                ON CONFLICT(repository) DO UPDATE SET
                    latest_observed_spec_hash=excluded.latest_observed_spec_hash,
                    updated_at=excluded.updated_at
                """,
                (snapshot.repository, snapshot.spec_hash, now),
            )
            existing = connection.execute(
                "SELECT * FROM idea_runs WHERE repository=? AND spec_hash=?",
                (snapshot.repository, snapshot.spec_hash),
            ).fetchone()
            if existing:
                return self._idea_run(existing), False, []  # type: ignore[return-value]
            older_rows = connection.execute(
                """
                SELECT * FROM idea_runs WHERE repository=? AND state IN (?, ?, ?)
                ORDER BY created_at
                """,
                (
                    snapshot.repository,
                    IdeaRunState.DISCOVERED,
                    IdeaRunState.QUEUED,
                    IdeaRunState.RUNNING,
                ),
            ).fetchall()
            older = [self._idea_run(row) for row in older_rows if row is not None]
            for previous in older:
                if previous is None:
                    continue
                connection.execute(
                    """
                    UPDATE idea_runs SET state=?, phase='superseded', cancel_requested=1,
                        updated_at=?, finished_at=? WHERE id=?
                    """,
                    (IdeaRunState.SUPERSEDED, now, now, previous.id),
                )
                connection.execute(
                    "INSERT INTO idea_run_events(run_id, at, kind, detail_json) VALUES (?, ?, 'superseded', ?)",
                    (previous.id, now, json.dumps({"new_spec_hash": snapshot.spec_hash}, sort_keys=True)),
                )
            connection.execute(
                """
                INSERT INTO idea_runs(
                    id, repository, spec_hash, spec_content, runtime_content, previous_progress,
                    base_commit, default_branch, implementation_provider, state, phase, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', ?, ?)
                """,
                (
                    run_id,
                    snapshot.repository,
                    snapshot.spec_hash,
                    snapshot.spec_content,
                    snapshot.runtime_content,
                    snapshot.previous_progress,
                    snapshot.base_commit,
                    snapshot.default_branch,
                    implementation_provider,
                    IdeaRunState.DISCOVERED,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO idea_run_events(run_id, at, kind, detail_json) VALUES (?, ?, 'discovered', ?)",
                (run_id, now, json.dumps({"spec_hash": snapshot.spec_hash}, sort_keys=True)),
            )
            connection.execute(
                "UPDATE idea_runs SET state=?, phase='queued', updated_at=? WHERE id=?",
                (IdeaRunState.QUEUED, now, run_id),
            )
            connection.execute(
                "INSERT INTO idea_run_events(run_id, at, kind, detail_json) VALUES (?, ?, 'transition', ?)",
                (
                    run_id,
                    now,
                    json.dumps(
                        {"from": str(IdeaRunState.DISCOVERED), "to": str(IdeaRunState.QUEUED), "phase": "queued"},
                        sort_keys=True,
                    ),
                ),
            )
            row = connection.execute("SELECT * FROM idea_runs WHERE id=?", (run_id,)).fetchone()
            return self._idea_run(row), True, [value for value in older if value is not None]  # type: ignore[return-value]

    def last_completed_idea_run(self, repository: str) -> IdeaRun | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT r.* FROM idea_projects p
                JOIN idea_runs r ON r.repository=p.repository AND r.spec_hash=p.latest_completed_spec_hash
                WHERE p.repository=?
                """,
                (repository,),
            ).fetchone()
        return self._idea_run(row)

    def ensure_job(
        self,
        snapshot: IssueSnapshot,
        implementation_provider: str,
        review_provider: str | None,
        review_required: bool,
        branch: str,
        concurrency_key: str,
    ) -> tuple[Job, bool]:
        now = utcnow()
        job_id = str(uuid.uuid4())
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE repository=? AND issue_number=?",
                (snapshot.repository, snapshot.number),
            ).fetchone()
            if existing:
                return self._job(existing), False  # type: ignore[return-value]
            connection.execute(
                """
                INSERT INTO jobs (
                    id, repository, issue_number, snapshot_hash, content_hash, snapshot_json,
                    implementation_provider, review_provider, review_required, state, branch,
                    concurrency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    snapshot.repository,
                    snapshot.number,
                    snapshot.revision_hash(),
                    snapshot.content_hash(),
                    snapshot.to_json(),
                    implementation_provider,
                    review_provider,
                    int(review_required),
                    JobState.QUEUED,
                    branch,
                    concurrency_key,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO job_events(job_id, at, kind, detail_json) VALUES (?, ?, ?, ?)",
                (job_id, now, "created", json.dumps({"snapshot_hash": snapshot.revision_hash()})),
            )
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self._job(row), True  # type: ignore[return-value]

    def record_delivery(
        self,
        delivery_id: str,
        event_name: str,
        payload_hash: str,
        repository: str | None,
        issue_number: int | None,
    ) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO deliveries(
                    delivery_id, event_name, repository, issue_number, received_at, payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (delivery_id, event_name, repository, issue_number, utcnow(), payload_hash),
            )
            return cursor.rowcount == 1

    def delivery_recorded(self, delivery_id: str) -> bool:
        with self.connect() as connection:
            return (
                connection.execute("SELECT 1 FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
                is not None
            )

    def update_snapshot_if_unclaimed(
        self,
        snapshot: IssueSnapshot,
        *,
        implementation_provider: str,
        review_provider: str | None,
        review_required: bool,
        branch: str,
        concurrency_key: str,
    ) -> bool:
        """Refresh queued intake without changing the immutable claim snapshot."""
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET snapshot_hash=?, content_hash=?, snapshot_json=?, implementation_provider=?,
                    review_provider=?, review_required=?, branch=?, concurrency_key=?, updated_at=?
                WHERE repository=? AND issue_number=? AND state=? AND attempt=0
                """,
                (
                    snapshot.revision_hash(),
                    snapshot.content_hash(),
                    snapshot.to_json(),
                    implementation_provider,
                    review_provider,
                    int(review_required),
                    branch,
                    concurrency_key,
                    utcnow(),
                    snapshot.repository,
                    snapshot.number,
                    JobState.QUEUED,
                ),
            )
            return cursor.rowcount == 1

    def expired_lease_jobs(self) -> list[Job]:
        now = utcnow()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT j.* FROM leases l JOIN jobs j ON j.id=l.run_id
                WHERE l.run_kind=? AND l.expires_at<=? ORDER BY j.created_at
                """,
                (ISSUE_RUN_KIND, now),
            ).fetchall()
        return [self._job(row) for row in rows if row is not None]

    def expired_idea_lease_runs(self) -> list[IdeaRun]:
        now = utcnow()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.* FROM leases l JOIN idea_runs r ON r.id=l.run_id
                WHERE l.run_kind=? AND l.expires_at<=? ORDER BY r.created_at
                """,
                (IDEA_RUN_KIND, now),
            ).fetchall()
        return [self._idea_run(row) for row in rows if row is not None]

    def reap_expired_leases(self, job_ids: set[str] | None = None) -> list[str]:
        now = utcnow()
        recovered: list[str] = []
        with self.transaction() as connection:
            query = (
                "SELECT l.run_id AS job_id, j.state, j.pause_requested, j.cancel_requested "
                "FROM leases l JOIN jobs j ON j.id=l.run_id WHERE l.run_kind=? AND l.expires_at<=?"
            )
            parameters: list[Any] = [ISSUE_RUN_KIND, now]
            if job_ids is not None:
                if not job_ids:
                    return []
                placeholders = ",".join("?" for _ in job_ids)
                query += f" AND l.run_id IN ({placeholders})"
                parameters.extend(sorted(job_ids))
            rows = connection.execute(query, parameters).fetchall()
            for row in rows:
                job_id = str(row["job_id"])
                state = JobState(row["state"])
                canceled = bool(row["cancel_requested"])
                paused = bool(row["pause_requested"])
                if canceled:
                    recovery_state = JobState.CANCELED
                    phase = "canceled"
                    message = "Canceled by operator while the previous worker lease was active."
                    terminal_outcome = str(JobState.CANCELED)
                    terminal_reason = message
                    finished_at = now
                elif paused:
                    recovery_state = JobState.NEEDS_GUIDANCE
                    phase = "paused"
                    message = "Paused by operator; use /agent resume to requeue."
                    terminal_outcome = None
                    terminal_reason = None
                    finished_at = None
                else:
                    recovery_state = JobState.PR_OPEN if state == JobState.REVIEWING else JobState.QUEUED
                    phase = "recovered-expired-lease"
                    message = "Previous worker lease expired; reconciliation will safely resume or retry."
                    terminal_outcome = None
                    terminal_reason = None
                    finished_at = None
                connection.execute(
                    """
                    UPDATE jobs SET state=?, phase=?, lease_owner=NULL, lease_expires_at=NULL,
                        updated_at=?, actionable_message=?, terminal_outcome=?, terminal_reason=?, finished_at=?
                    WHERE id=?
                    """,
                    (
                        recovery_state,
                        phase,
                        now,
                        message,
                        terminal_outcome,
                        terminal_reason,
                        finished_at,
                        job_id,
                    ),
                )
                RepositoryLeases.release(connection, ISSUE_RUN_KIND, job_id)
                connection.execute(
                    "INSERT INTO job_events(job_id, at, kind, detail_json) VALUES (?, ?, 'lease-expired', '{}')",
                    (job_id, now),
                )
                recovered.append(job_id)
        return recovered

    def claim_next(
        self,
        owner: str,
        lease_seconds: int,
        global_limit: int,
        provider_limits: dict[str, int],
    ) -> Job | None:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        with self.transaction() as connection:
            active_count = RepositoryLeases.active_count(connection, now)
            if active_count >= global_limit:
                return None
            provider_counts = RepositoryLeases.provider_counts(connection, now)
            rows = connection.execute(
                """
                SELECT j.* FROM jobs j
                LEFT JOIN provider_backoff b ON b.provider=j.implementation_provider AND b.until_at>?
                WHERE j.state=? AND j.cancel_requested=0 AND j.pause_requested=0 AND b.provider IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM leases l WHERE l.concurrency_key=j.concurrency_key
                  )
                ORDER BY
                    j.retry_requested DESC,
                    COALESCE((
                        SELECT MAX(e.at)
                        FROM job_events e
                        JOIN jobs previous ON previous.id=e.job_id
                        WHERE previous.repository=j.repository AND e.kind='claimed'
                    ), '') ASC,
                    j.updated_at ASC,
                    j.created_at ASC
                """,
                (now, JobState.QUEUED),
            ).fetchall()
            chosen: sqlite3.Row | None = None
            for row in rows:
                provider = str(row["implementation_provider"])
                if provider_counts.get(provider, 0) < provider_limits.get(provider, 1):
                    chosen = row
                    break
            if chosen is None:
                return None
            job_id = str(chosen["id"])
            concurrency_key = str(chosen["concurrency_key"])
            lease = RepositoryLeases.claim(
                connection,
                ExecutionRef(
                    ISSUE_RUN_KIND,
                    job_id,
                    str(chosen["repository"]),
                    concurrency_key,
                    str(chosen["implementation_provider"]),
                ),
                owner,
                lease_seconds,
                now=now_dt,
            )
            connection.execute(
                """
                UPDATE jobs SET state=?, lease_owner=?, lease_expires_at=?,
                    heartbeat_at=?, started_at=COALESCE(started_at, ?), updated_at=?, phase='preflight',
                    finished_at=NULL
                WHERE id=?
                """,
                (JobState.RUNNING, owner, lease.expires_at, now, now, now, job_id),
            )
            connection.execute(
                "INSERT INTO job_events(job_id, at, kind, detail_json) VALUES (?, ?, 'claimed', ?)",
                (job_id, now, json.dumps({"owner": owner, "expires_at": lease.expires_at})),
            )
            return self._job(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def claim_next_idea(
        self,
        owner: str,
        lease_seconds: int,
        global_limit: int,
        provider_limits: dict[str, int],
    ) -> IdeaRun | None:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        with self.transaction() as connection:
            if RepositoryLeases.active_count(connection, now) >= global_limit:
                return None
            provider_counts = RepositoryLeases.provider_counts(connection, now)
            rows = connection.execute(
                """
                SELECT r.* FROM idea_runs r
                LEFT JOIN provider_backoff b ON b.provider=r.implementation_provider AND b.until_at>?
                WHERE r.state=? AND r.cancel_requested=0 AND b.provider IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM leases l WHERE l.concurrency_key=r.repository
                  )
                ORDER BY r.retry_requested DESC, r.updated_at ASC, r.created_at ASC
                """,
                (now, IdeaRunState.QUEUED),
            ).fetchall()
            chosen: sqlite3.Row | None = None
            for row in rows:
                provider = str(row["implementation_provider"])
                if provider_counts.get(provider, 0) < provider_limits.get(provider, 1):
                    chosen = row
                    break
            if chosen is None:
                return None
            run_id = str(chosen["id"])
            repository = str(chosen["repository"])
            provider = str(chosen["implementation_provider"])
            lease = RepositoryLeases.claim(
                connection,
                ExecutionRef(IDEA_RUN_KIND, run_id, repository, repository, provider),
                owner,
                lease_seconds,
                now=now_dt,
            )
            connection.execute(
                """
                UPDATE idea_runs SET state=?, phase='preflight', lease_owner=?, lease_expires_at=?,
                    heartbeat_at=?, started_at=COALESCE(started_at, ?), updated_at=?, finished_at=NULL
                WHERE id=?
                """,
                (IdeaRunState.RUNNING, owner, lease.expires_at, now, now, now, run_id),
            )
            connection.execute(
                "INSERT INTO idea_run_events(run_id, at, kind, detail_json) VALUES (?, ?, 'claimed', ?)",
                (run_id, now, json.dumps({"owner": owner, "expires_at": lease.expires_at}, sort_keys=True)),
            )
            return self._idea_run(connection.execute("SELECT * FROM idea_runs WHERE id=?", (run_id,)).fetchone())

    def begin_idea_attempt(
        self,
        run_id: str,
        *,
        conversation_id: str | None = None,
        session_id: str | None = None,
    ) -> IdeaRun:
        now = utcnow()
        with self.transaction() as connection:
            row = connection.execute("SELECT state, attempt FROM idea_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise StoreError(f"unknown idea run: {run_id}")
            if IdeaRunState(row["state"]) != IdeaRunState.RUNNING:
                raise StoreError("an idea attempt may only start for a running run")
            attempt = int(row["attempt"]) + 1
            connection.execute(
                """
                UPDATE idea_runs SET attempt=?, conversation_id=COALESCE(?, conversation_id),
                    session_id=COALESCE(?, session_id), phase='implementation', retry_requested=0, updated_at=?
                WHERE id=?
                """,
                (attempt, conversation_id, session_id, now, run_id),
            )
            connection.execute(
                "INSERT INTO idea_run_events(run_id, at, kind, detail_json) VALUES (?, ?, 'attempt-started', ?)",
                (
                    run_id,
                    now,
                    json.dumps(
                        {"attempt": attempt, "conversation_id": conversation_id, "session_id": session_id},
                        sort_keys=True,
                    ),
                ),
            )
            return self._idea_run(
                connection.execute("SELECT * FROM idea_runs WHERE id=?", (run_id,)).fetchone()
            )  # type: ignore[return-value]

    def renew_idea_lease(self, run_id: str, owner: str, lease_seconds: int) -> bool:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        with self.transaction() as connection:
            expires = RepositoryLeases.renew(
                connection, IDEA_RUN_KIND, run_id, owner, lease_seconds, now=now_dt
            )
            if expires:
                connection.execute(
                    "UPDATE idea_runs SET lease_expires_at=?, heartbeat_at=?, updated_at=? WHERE id=?",
                    (expires, now, now, run_id),
                )
            return expires is not None

    def release_idea_lease(self, run_id: str) -> IdeaRun:
        with self.transaction() as connection:
            RepositoryLeases.release(connection, IDEA_RUN_KIND, run_id)
            connection.execute(
                "UPDATE idea_runs SET lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?",
                (utcnow(), run_id),
            )
            row = connection.execute("SELECT * FROM idea_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise StoreError(f"unknown idea run: {run_id}")
            return self._idea_run(row)  # type: ignore[return-value]

    def update_idea_run(self, run_id: str, **values: Any) -> IdeaRun:
        allowed = {
            "worktree",
            "conversation_id",
            "session_id",
            "phase",
            "validation_summary",
            "question",
            "published_commit",
            "retry_requested",
        }
        invalid = set(values) - allowed
        if invalid:
            raise StoreError(f"unsupported idea run fields: {sorted(invalid)}")
        values["updated_at"] = utcnow()
        with self.transaction() as connection:
            assignments = ", ".join(f"{key}=?" for key in values)
            connection.execute(f"UPDATE idea_runs SET {assignments} WHERE id=?", (*values.values(), run_id))
            row = connection.execute("SELECT * FROM idea_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise StoreError(f"unknown idea run: {run_id}")
            return self._idea_run(row)  # type: ignore[return-value]

    def transition_idea_run(
        self,
        run_id: str,
        new_state: IdeaRunState,
        *,
        phase: str | None = None,
        validation_summary: str | None = None,
        question: str | None = None,
        published_commit: str | None = None,
        release_lease: bool | None = None,
    ) -> IdeaRun:
        now = utcnow()
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM idea_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise StoreError(f"unknown idea run: {run_id}")
            current = IdeaRunState(row["state"])
            if new_state != current and new_state not in IDEA_ALLOWED_TRANSITIONS[current]:
                raise StoreError(f"invalid idea transition: {current} -> {new_state}")
            fields: dict[str, Any] = {"state": str(new_state), "updated_at": now}
            if phase is not None:
                fields["phase"] = phase
            if validation_summary is not None:
                fields["validation_summary"] = validation_summary
            if question is not None:
                fields["question"] = question
            if published_commit is not None:
                fields["published_commit"] = published_commit
            terminal = new_state not in IDEA_ACTIVE_STATES
            if terminal:
                fields["finished_at"] = now
            should_release = release_lease if release_lease is not None else new_state != IdeaRunState.RUNNING
            if should_release:
                fields["lease_owner"] = None
                fields["lease_expires_at"] = None
                RepositoryLeases.release(connection, IDEA_RUN_KIND, run_id)
            assignments = ", ".join(f"{key}=?" for key in fields)
            connection.execute(f"UPDATE idea_runs SET {assignments} WHERE id=?", (*fields.values(), run_id))
            detail = {"from": str(current), "to": str(new_state), "phase": phase}
            connection.execute(
                "INSERT INTO idea_run_events(run_id, at, kind, detail_json) VALUES (?, ?, 'transition', ?)",
                (run_id, now, json.dumps(detail, sort_keys=True)),
            )
            if new_state in {IdeaRunState.PUBLISHED, IdeaRunState.QUESTION}:
                preview_state = "pending" if new_state == IdeaRunState.PUBLISHED else None
                connection.execute(
                    """
                    UPDATE idea_projects SET latest_completed_spec_hash=?,
                        preview_state=COALESCE(?, preview_state), updated_at=? WHERE repository=?
                    """,
                    (
                        row["spec_hash"],
                        preview_state,
                        now,
                        row["repository"],
                    ),
                )
            return self._idea_run(
                connection.execute("SELECT * FROM idea_runs WHERE id=?", (run_id,)).fetchone()
            )  # type: ignore[return-value]

    def update_idea_preview(
        self,
        repository: str,
        preview_state: str,
        *,
        last_good_commit: str | None = None,
    ) -> IdeaProject:
        if not preview_state or len(preview_state) > 64:
            raise StoreError("idea preview state must be a short non-empty string")
        now = utcnow()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT repository FROM idea_projects WHERE repository=?", (repository,)
            ).fetchone()
            if row is None:
                raise StoreError(f"unknown idea project: {repository}")
            connection.execute(
                """
                UPDATE idea_projects SET preview_state=?,
                    last_good_preview_commit=COALESCE(?, last_good_preview_commit), updated_at=?
                WHERE repository=?
                """,
                (preview_state, last_good_commit, now, repository),
            )
            return self._idea_project(
                connection.execute(
                    "SELECT * FROM idea_projects WHERE repository=?", (repository,)
                ).fetchone()
            )  # type: ignore[return-value]

    def latest_published_idea_run(self, repository: str) -> IdeaRun | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM idea_runs WHERE repository=? AND state=? AND published_commit IS NOT NULL
                ORDER BY finished_at DESC, created_at DESC LIMIT 1
                """,
                (repository, IdeaRunState.PUBLISHED),
            ).fetchone()
        return self._idea_run(row)

    def begin_attempt(
        self,
        job_id: str,
        *,
        conversation_id: str | None = None,
        session_id: str | None = None,
    ) -> Job:
        """Count an implementation attempt only after the provider accepts the turn."""
        now = utcnow()
        with self.transaction() as connection:
            row = connection.execute("SELECT state, attempt FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise StoreError(f"unknown job: {job_id}")
            if JobState(row["state"]) != JobState.RUNNING:
                raise StoreError("an implementation attempt may only start for a running job")
            attempt = int(row["attempt"]) + 1
            connection.execute(
                """
                UPDATE jobs SET attempt=?, conversation_id=COALESCE(?, conversation_id),
                    session_id=COALESCE(?, session_id), phase='implementation', retry_requested=0, updated_at=?
                WHERE id=?
                """,
                (attempt, conversation_id, session_id, now, job_id),
            )
            connection.execute(
                "INSERT INTO job_events(job_id, at, kind, detail_json) VALUES (?, ?, 'attempt-started', ?)",
                (
                    job_id,
                    now,
                    json.dumps(
                        {
                            "attempt": attempt,
                            "conversation_id": conversation_id,
                            "session_id": session_id,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            return self._job(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def renew_lease(self, job_id: str, owner: str, lease_seconds: int) -> bool:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        with self.transaction() as connection:
            expires = RepositoryLeases.renew(
                connection, ISSUE_RUN_KIND, job_id, owner, lease_seconds, now=now_dt
            )
            if expires:
                connection.execute(
                    "UPDATE jobs SET lease_expires_at=?, heartbeat_at=?, updated_at=? WHERE id=?",
                    (expires, now, now, job_id),
                )
            return expires is not None

    def transition(
        self,
        job_id: str,
        new_state: JobState,
        *,
        phase: str | None = None,
        actionable_message: str | None = None,
        terminal_reason: str | None = None,
        validation_summary: str | None = None,
        release_lease: bool | None = None,
    ) -> Job:
        now = utcnow()
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise StoreError(f"unknown job: {job_id}")
            current = JobState(row["state"])
            if new_state != current and new_state not in ALLOWED_TRANSITIONS[current]:
                raise StoreError(f"invalid transition: {current} -> {new_state}")
            should_release = release_lease if release_lease is not None else new_state not in ACTIVE_STATES
            fields: dict[str, Any] = {"state": str(new_state), "updated_at": now}
            if phase is not None:
                fields["phase"] = phase
            if actionable_message is not None:
                fields["actionable_message"] = actionable_message
            if validation_summary is not None:
                fields["validation_summary"] = validation_summary
            if terminal_reason is not None:
                fields["terminal_reason"] = terminal_reason
                fields["terminal_outcome"] = str(new_state)
            if new_state in {JobState.BLOCKED, JobState.FAILED, JobState.CANCELED, JobState.DONE}:
                fields["finished_at"] = now
            if should_release:
                fields["lease_owner"] = None
                fields["lease_expires_at"] = None
                RepositoryLeases.release(connection, ISSUE_RUN_KIND, job_id)
            assignments = ", ".join(f"{key}=?" for key in fields)
            connection.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*fields.values(), job_id))
            event_detail: dict[str, Any] = {"from": str(current), "to": str(new_state), "phase": phase}
            if actionable_message is not None:
                event_detail["actionable_message"] = actionable_message
            if terminal_reason is not None:
                event_detail["terminal_reason"] = terminal_reason
            if validation_summary is not None:
                event_detail["validation_summary"] = validation_summary
            connection.execute(
                "INSERT INTO job_events(job_id, at, kind, detail_json) VALUES (?, ?, 'transition', ?)",
                (job_id, now, json.dumps(event_detail, sort_keys=True)),
            )
            return self._job(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def update_job(self, job_id: str, **values: Any) -> Job:
        allowed = {
            "worktree",
            "conversation_id",
            "session_id",
            "status_comment_id",
            "pr_number",
            "pr_url",
            "phase",
            "validation_summary",
            "actionable_message",
            "snapshot_hash",
            "content_hash",
            "snapshot_json",
            "implementation_provider",
            "review_provider",
            "review_required",
            "review_conversation_id",
            "review_session_id",
            "concurrency_key",
            "retry_requested",
        }
        invalid = set(values) - allowed
        if invalid:
            raise StoreError(f"unsupported job fields: {sorted(invalid)}")
        values["updated_at"] = utcnow()
        with self.transaction() as connection:
            assignments = ", ".join(f"{key}=?" for key in values)
            connection.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*values.values(), job_id))
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise StoreError(f"unknown job: {job_id}")
            return self._job(row)  # type: ignore[return-value]

    def request_control(self, repository: str, issue_number: int, command: str) -> Job | None:
        now = utcnow()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE repository=? AND issue_number=?", (repository, issue_number)
            ).fetchone()
            if row is None:
                return None
            job_id = str(row["id"])
            state = JobState(row["state"])
            reset_pre_provider_attempts = False
            if command == "cancel":
                connection.execute("UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?", (now, job_id))
                if state not in ACTIVE_STATES:
                    connection.execute(
                        """
                        UPDATE jobs SET state=?, phase='canceled', terminal_outcome=?, terminal_reason=?,
                            finished_at=?, lease_owner=NULL, lease_expires_at=NULL WHERE id=?
                        """,
                        (JobState.CANCELED, JobState.CANCELED, "Canceled by operator.", now, job_id),
                    )
                    RepositoryLeases.release(connection, ISSUE_RUN_KIND, job_id)
            elif command == "pause":
                message = "Paused by /agent pause; use /agent resume to requeue."
                connection.execute(
                    "UPDATE jobs SET pause_requested=1, actionable_message=?, updated_at=? WHERE id=?",
                    (message, now, job_id),
                )
                if state == JobState.QUEUED:
                    connection.execute(
                        "UPDATE jobs SET state=?, phase='paused', lease_owner=NULL, lease_expires_at=NULL WHERE id=?",
                        (JobState.NEEDS_GUIDANCE, job_id),
                    )
                    RepositoryLeases.release(connection, ISSUE_RUN_KIND, job_id)
            elif command in {"resume", "retry"}:
                has_attempt_events = (
                    connection.execute(
                        "SELECT 1 FROM job_events WHERE job_id=? AND kind='attempt-started' LIMIT 1",
                        (job_id,),
                    ).fetchone()
                    is not None
                )
                has_legacy_setup_failure = (
                    connection.execute(
                        """
                        SELECT 1 FROM job_events
                        WHERE job_id=? AND kind='transition' AND detail_json LIKE '%setup-failed%'
                        LIMIT 1
                        """,
                        (job_id,),
                    ).fetchone()
                    is not None
                )
                has_preconversation_provider_failure = (
                    connection.execute(
                        """
                        SELECT 1 FROM job_events
                        WHERE job_id=? AND kind='transition'
                          AND (detail_json LIKE '%provider-tool-retry%'
                               OR detail_json LIKE '%provider-tool-failure%')
                        LIMIT 1
                        """,
                        (job_id,),
                    ).fetchone()
                    is not None
                )
                reset_pre_provider_attempts = (
                    row["conversation_id"] is None
                    and row["pr_number"] is None
                    and int(row["attempt"]) > 0
                    and (
                        (not has_attempt_events and has_legacy_setup_failure)
                        or has_preconversation_provider_failure
                    )
                )
                retryable_pr = state == JobState.PR_OPEN
                if (
                    state in {JobState.NEEDS_GUIDANCE, JobState.BLOCKED, JobState.FAILED, JobState.CANCELED}
                    or retryable_pr
                    or (state == JobState.QUEUED and reset_pre_provider_attempts)
                ):
                    connection.execute(
                        """
                        UPDATE jobs SET state=?, phase='explicit-requeue', retry_requested=1,
                            cancel_requested=0, pause_requested=0, actionable_message='', terminal_outcome=NULL,
                            terminal_reason=NULL, finished_at=NULL,
                            attempt=CASE WHEN ? THEN 0 ELSE attempt END,
                            updated_at=? WHERE id=?
                        """,
                        (JobState.QUEUED, int(reset_pre_provider_attempts), now, job_id),
                    )
            else:
                raise StoreError(f"unknown control command: {command}")
            connection.execute(
                "INSERT INTO job_events(job_id, at, kind, detail_json) VALUES (?, ?, 'control', ?)",
                (
                    job_id,
                    now,
                    json.dumps(
                        {"command": command, "reset_pre_provider_attempts": reset_pre_provider_attempts},
                        sort_keys=True,
                    ),
                ),
            )
            return self._job(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def accept_snapshot(self, job_id: str, snapshot: IssueSnapshot) -> Job:
        """Accept a changed specification only after an explicit resume/retry command."""
        with self.transaction() as connection:
            row = connection.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise StoreError(f"unknown job: {job_id}")
            if JobState(row["state"]) != JobState.QUEUED:
                raise StoreError("a new issue revision may only be accepted while explicitly requeued")
            connection.execute(
                """
                UPDATE jobs SET snapshot_hash=?, content_hash=?, snapshot_json=?, updated_at=? WHERE id=?
                """,
                (snapshot.revision_hash(), snapshot.content_hash(), snapshot.to_json(), utcnow(), job_id),
            )
            connection.execute(
                "INSERT INTO job_events(job_id, at, kind, detail_json) VALUES (?, ?, 'snapshot-accepted', ?)",
                (job_id, utcnow(), json.dumps({"snapshot_hash": snapshot.revision_hash()})),
            )
            return self._job(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def record_validation(self, job_id: str, attempt: int, result: ValidationResult) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO validation_results(
                    job_id, attempt, command_json, exit_code, started_at, finished_at, output, timed_out
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    attempt,
                    json.dumps(result.command),
                    result.exit_code,
                    result.started_at,
                    result.finished_at,
                    result.output,
                    int(result.timed_out),
                ),
            )

    def validations(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM validation_results WHERE job_id=? ORDER BY id", (job_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def events(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM job_events WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
        return [dict(row) for row in rows]

    def record_event(self, job_id: str, kind: str, detail: dict[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO job_events(job_id, at, kind, detail_json) VALUES (?, ?, ?, ?)",
                (job_id, utcnow(), kind, json.dumps(detail, sort_keys=True)),
            )

    def record_idea_validation(self, run_id: str, attempt: int, result: ValidationResult) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO idea_validation_results(
                    run_id, attempt, command_json, exit_code, started_at, finished_at, output, timed_out
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    attempt,
                    json.dumps(result.command),
                    result.exit_code,
                    result.started_at,
                    result.finished_at,
                    result.output,
                    int(result.timed_out),
                ),
            )

    def idea_validations(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM idea_validation_results WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def idea_events(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM idea_run_events WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def record_idea_event(self, run_id: str, kind: str, detail: dict[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO idea_run_events(run_id, at, kind, detail_json) VALUES (?, ?, ?, ?)",
                (run_id, utcnow(), kind, json.dumps(detail, sort_keys=True)),
            )

    def set_provider_backoff(self, provider: str, reason: str, seconds: int) -> None:
        now_dt = datetime.now(UTC)
        until = (now_dt + timedelta(seconds=seconds)).isoformat()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO provider_backoff(provider, reason, until_at, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET reason=excluded.reason,
                    until_at=excluded.until_at, updated_at=excluded.updated_at
                """,
                (provider, reason, until, now_dt.isoformat()),
            )

    def acquire_operation_lock(self, name: str, owner: str, seconds: int = 900) -> bool:
        """Acquire a short durable mutex for idempotent external artifact mutation."""
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=seconds)).isoformat()
        with self.transaction() as connection:
            connection.execute("DELETE FROM operation_locks WHERE expires_at<=?", (now,))
            cursor = connection.execute(
                "INSERT OR IGNORE INTO operation_locks(name, owner, expires_at) VALUES (?, ?, ?)",
                (name, owner, expires),
            )
            return cursor.rowcount == 1

    def release_operation_lock(self, name: str, owner: str) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM operation_locks WHERE name=? AND owner=?", (name, owner))

    def backup(self, target: str | Path) -> None:
        target_path = Path(target)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(target_path) as destination:
            source.backup(destination)
