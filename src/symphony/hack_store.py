from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .execution import ExecutionRef, RepositoryLeases
from .hack_contract import HackContractError, parse_board, parse_lanes
from .intake import validate_repository_name
from .models import utcnow

if TYPE_CHECKING:
    from .store import Store

HACK_RUN_KIND = "hack-task"
ACTIVE_CAMPAIGN_STATES = ("starting", "active", "draining", "polishing", "publishing")
CAMPAIGN_STATES = {*ACTIVE_CAMPAIGN_STATES, "completed", "failed", "expired"}
TASK_STATES = {"queued", "running", "ready", "merged", "blocked", "question", "canceled"}


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS hack_campaigns (
            id TEXT PRIMARY KEY,
            repository TEXT NOT NULL,
            home_tier TEXT NOT NULL,
            default_branch TEXT NOT NULL,
            base_commit TEXT NOT NULL,
            branch TEXT NOT NULL UNIQUE,
            provider TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'starting',
            expires_at TEXT NOT NULL,
            max_parallel INTEGER NOT NULL,
            max_tasks INTEGER NOT NULL,
            publish_ideas INTEGER NOT NULL DEFAULT 0,
            stop_requested INTEGER NOT NULL DEFAULT 0,
            board_hash TEXT NOT NULL DEFAULT '',
            observed_board_hash TEXT NOT NULL DEFAULT '',
            board_revision INTEGER NOT NULL DEFAULT 0,
            result_commit TEXT,
            campaign_commit TEXT,
            worktree TEXT,
            pr_url TEXT,
            publication_url TEXT,
            note TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS hack_campaign_active_repo_idx ON hack_campaigns(repository)
            WHERE state IN ('starting','active','draining','polishing','publishing');
        CREATE TABLE IF NOT EXISTS hack_tasks (
            id TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL REFERENCES hack_campaigns(id),
            repository TEXT NOT NULL,
            task_key TEXT NOT NULL,
            lane TEXT NOT NULL,
            prompt TEXT NOT NULL,
            footprint TEXT NOT NULL,
            depends_on TEXT NOT NULL DEFAULT '[]',
            kind TEXT NOT NULL DEFAULT 'lane',
            state TEXT NOT NULL DEFAULT 'queued',
            provider TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            board_owned INTEGER NOT NULL DEFAULT 0,
            branch TEXT NOT NULL,
            base_commit TEXT,
            result_commit TEXT,
            prepared_commit TEXT,
            prepared_base_commit TEXT,
            implementation_commit TEXT,
            worktree TEXT,
            conversation_id TEXT,
            session_id TEXT,
            lease_owner TEXT,
            lease_expires_at TEXT,
            heartbeat_at TEXT,
            attempt INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            UNIQUE(campaign_id,task_key)
        );
        CREATE INDEX IF NOT EXISTS hack_task_queue_idx ON hack_tasks(campaign_id,state,priority,created_at);
    """)
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(hack_tasks)")}
    for column in ("prepared_commit", "prepared_base_commit", "implementation_commit"):
        if column not in columns:
            connection.execute(f"ALTER TABLE hack_tasks ADD COLUMN {column} TEXT")
    campaign_columns = {row["name"] for row in connection.execute("PRAGMA table_info(hack_campaigns)")}
    if "observed_board_hash" not in campaign_columns:
        connection.execute("ALTER TABLE hack_campaigns ADD COLUMN observed_board_hash TEXT NOT NULL DEFAULT ''")
    if "board_revision" not in campaign_columns:
        connection.execute("ALTER TABLE hack_campaigns ADD COLUMN board_revision INTEGER NOT NULL DEFAULT 0")


class HackStore:
    """Durable hack campaigns sharing the normal transactional capacity and repository fences."""

    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _campaign(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["base_branch"] = result["default_branch"]
        return result

    @staticmethod
    def _task(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["footprint"] = json.loads(result["footprint"])
        result["depends_on"] = json.loads(result["depends_on"])
        result["key"] = result["task_key"]
        result["title"] = result["prompt"]
        return result

    def start_campaign(
        self, repository: str, home_tier: str, default_branch: str, base_commit: str,
        provider: str, hours: float, max_parallel: int = 4, max_tasks: int = 100,
        publish_ideas: bool = False,
    ) -> dict[str, Any]:
        from .store import StoreError

        validate_repository_name(repository)
        if isinstance(hours, bool) or not math.isfinite(hours) or not 0 < hours <= 168:
            raise StoreError("hack duration must be a finite positive number of hours up to 168")
        if isinstance(max_parallel, bool) or not isinstance(max_parallel, int) or not 1 <= max_parallel <= 6:
            raise StoreError("hack parallelism must be between 1 and 6")
        if isinstance(max_tasks, bool) or not isinstance(max_tasks, int) or max_tasks < 1:
            raise StoreError("hack task budget must be a positive integer")
        if home_tier not in {"github", "idea"} or not default_branch or not base_commit or not provider:
            raise StoreError("hack campaign must retain its home tier, branch, base commit and provider")
        campaign_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        timestamp = now.isoformat()
        branch = f"hack/{now:%Y%m%d}-{repository.split('/')[-1]}-{campaign_id[:8]}"
        with self.store.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM operation_locks WHERE name='graduate' AND expires_at>?", (timestamp,),
            ).fetchone():
                raise StoreError("repository tier graduation is active; wait until it completes before hacking")
            if connection.execute(
                "SELECT 1 FROM hack_campaigns WHERE repository=? AND state IN ('starting','active','draining','polishing','publishing')",
                (repository,),
            ).fetchone():
                raise StoreError("repository already has an active hack campaign")
            if connection.execute("SELECT 1 FROM leases WHERE repository=?", (repository,)).fetchone():
                raise StoreError("repository has an existing execution lease; finish or cancel that work before hacking")
            connection.execute(
                """INSERT INTO hack_campaigns(id,repository,home_tier,default_branch,base_commit,branch,provider,
                   expires_at,max_parallel,max_tasks,publish_ideas,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (campaign_id, repository, home_tier, default_branch, base_commit, branch, provider,
                 (now + timedelta(hours=hours)).isoformat(), max_parallel, max_tasks, int(publish_ideas), timestamp, timestamp),
            )
            self._enqueue(connection, campaign_id, "__scaffold__", "scaffold",
                          "Create or verify a bootable shared skeleton, frozen interfaces, lane boundaries and the fast gate. "
                          "Preserve the director-owned hack/BOARD.md. Prepare the project for independent lane work.",
                          ("**",), (), "scaffold")
            return self._campaign(connection.execute("SELECT * FROM hack_campaigns WHERE id=?", (campaign_id,)).fetchone())

    def active_campaign(self, repository: str) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            return self._campaign(connection.execute(
                "SELECT * FROM hack_campaigns WHERE repository=? AND state IN ('starting','active','draining','polishing','publishing')",
                (repository,),
            ).fetchone())

    def list_campaigns(self, active_only: bool = True) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            clause = "WHERE state IN ('starting','active','draining','polishing','publishing')" if active_only else ""
            return [self._campaign(row) for row in connection.execute(f"SELECT * FROM hack_campaigns {clause} ORDER BY created_at")]

    def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            return self._campaign(connection.execute("SELECT * FROM hack_campaigns WHERE id=?", (campaign_id,)).fetchone())

    def update_campaign(self, campaign_id: str, **fields: Any) -> dict[str, Any]:
        from .store import StoreError

        allowed = {"state", "expires_at", "stop_requested", "board_hash", "observed_board_hash", "board_revision", "result_commit", "campaign_commit", "worktree",
                   "pr_url", "publication_url", "note", "error", "finished_at"}
        if fields.keys() - allowed:
            raise StoreError("unsupported hack campaign fields: " + ", ".join(sorted(fields.keys() - allowed)))
        if "state" in fields and fields["state"] not in CAMPAIGN_STATES:
            raise StoreError("invalid hack campaign state")
        if fields.get("state") in {"completed", "failed", "expired"}:
            fields.setdefault("finished_at", utcnow())
        fields["updated_at"] = utcnow()
        with self.store.transaction() as connection:
            if fields.get("state") in {"completed", "failed", "expired"} and connection.execute(
                "SELECT 1 FROM leases l JOIN hack_tasks t ON t.id=l.run_id WHERE l.run_kind=? AND t.campaign_id=?",
                (HACK_RUN_KIND, campaign_id),
            ).fetchone():
                raise StoreError("cannot close a hack campaign until all execution leases are released")
            self._update(connection, "hack_campaigns", campaign_id, fields)
            return self._campaign(connection.execute("SELECT * FROM hack_campaigns WHERE id=?", (campaign_id,)).fetchone())

    def request_stop(self, repository: str) -> dict[str, Any] | None:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM hack_campaigns WHERE repository=? AND state IN ('starting','active','draining','polishing','publishing')",
                (repository,),
            ).fetchone()
            if row is None:
                return None
            # Publishing is already the final drain; never move it backwards.
            state = "draining" if row["state"] in {"starting", "active"} else row["state"]
            connection.execute("UPDATE hack_campaigns SET stop_requested=1,state=?,updated_at=? WHERE id=?", (state, utcnow(), row["id"]))
            connection.execute(
                "UPDATE hack_tasks SET state='canceled',note='Campaign stopped before task claim',updated_at=? WHERE campaign_id=? AND state='queued' AND kind='lane'",
                (utcnow(), row["id"]),
            )
            return self._campaign(connection.execute("SELECT * FROM hack_campaigns WHERE id=?", (row["id"],)).fetchone())

    @staticmethod
    def _update(connection: sqlite3.Connection, table: str, identifier: str, fields: dict[str, Any]) -> None:
        from .store import StoreError

        cursor = connection.execute(f"UPDATE {table} SET " + ",".join(f"{name}=?" for name in fields) + " WHERE id=?",
                                    (*fields.values(), identifier))
        if cursor.rowcount != 1:
            raise StoreError(f"unknown {table} row: {identifier}")

    def _enqueue(
        self, connection: sqlite3.Connection, campaign_id: str, key: str, lane: str, prompt: str,
        footprint: Any, depends_on: Any, kind: str, *, priority: int = 0, board_owned: bool = False,
        validate_existing: bool = True,
    ) -> dict[str, Any]:
        from .store import StoreError

        campaign = connection.execute("SELECT * FROM hack_campaigns WHERE id=?", (campaign_id,)).fetchone()
        if campaign is None:
            raise StoreError(f"unknown hack campaign: {campaign_id}")
        if kind not in {"scaffold", "lane", "polish", "dispatcher"} or not key or not lane or not prompt:
            raise StoreError("hack tasks require a key, lane, prompt and valid task kind")
        if kind == "lane":
            parse_lanes({lane: footprint})
        existing = connection.execute("SELECT * FROM hack_tasks WHERE campaign_id=? AND task_key=?", (campaign_id, key)).fetchone()
        if existing:
            return self._task(existing)
        if campaign["state"] not in ACTIVE_CAMPAIGN_STATES:
            raise StoreError("cannot enqueue work for a closed hack campaign")
        if kind == "lane":
            count = connection.execute("SELECT count(*) FROM hack_tasks WHERE campaign_id=? AND kind='lane'", (campaign_id,)).fetchone()[0]
            if count >= campaign["max_tasks"]:
                raise StoreError("hack task budget is exhausted")
            # Manual jobs obey the same non-overlap contract as board jobs.
            for row in connection.execute(
                "SELECT lane,footprint FROM hack_tasks WHERE campaign_id=? AND kind='lane' AND state IN ('queued','running','ready') AND lane!=?",
                (campaign_id, lane),
            ):
                if validate_existing:
                    parse_lanes({lane: footprint, row["lane"]: json.loads(row["footprint"])})
        task_id = str(uuid.uuid4())
        now = utcnow()
        connection.execute(
            """INSERT INTO hack_tasks(id,campaign_id,repository,task_key,lane,prompt,footprint,depends_on,kind,provider,
                priority,board_owned,branch,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, campaign_id, campaign["repository"], key, lane, prompt, json.dumps(list(footprint)),
             json.dumps(list(depends_on)), kind, campaign["provider"], priority, int(board_owned),
             f"hack/{campaign_id[:8]}/task-{task_id[:8]}", now, now),
        )
        return self._task(connection.execute("SELECT * FROM hack_tasks WHERE id=?", (task_id,)).fetchone())

    def enqueue_task(
        self, campaign_id: str, key: str, lane: str, prompt: str, footprint: Any,
        depends_on: Any = (), kind: str = "lane",
    ) -> dict[str, Any]:
        with self.store.transaction() as connection:
            return self._enqueue(connection, campaign_id, key, lane, prompt, footprint, depends_on, kind)

    def sync_board(
        self, campaign_id: str, board: bytes | str, lane_config: Any, *, source_hash: str | None = None,
        expected_task_id: str | None = None, expected_owner: str | None = None, expected_attempt: int | None = None,
    ) -> list[dict[str, Any]]:
        from .store import StoreError

        lanes = parse_lanes(lane_config)
        parsed = parse_board(board, lanes)
        content = board.encode() if isinstance(board, str) else board
        with self.store.transaction() as connection:
            if expected_task_id is not None:
                self._require_attempt(connection, expected_task_id, expected_owner, expected_attempt)
                dispatcher = connection.execute("SELECT campaign_id,kind FROM hack_tasks WHERE id=?", (expected_task_id,)).fetchone()
                if dispatcher is None or dispatcher["campaign_id"] != campaign_id or dispatcher["kind"] != "dispatcher":
                    raise StoreError("board reconciliation requires this campaign's dispatcher task")
            elif expected_owner is not None or expected_attempt is not None:
                raise StoreError("fenced board reconciliation requires a dispatcher task ID")
            campaign = connection.execute("SELECT * FROM hack_campaigns WHERE id=?", (campaign_id,)).fetchone()
            if campaign is None:
                raise StoreError(f"unknown hack campaign: {campaign_id}")
            if campaign["state"] not in {"starting", "active"} or campaign["stop_requested"]:
                return [self._task(row) for row in connection.execute("SELECT * FROM hack_tasks WHERE campaign_id=? ORDER BY priority,created_at", (campaign_id,))]
            existing = {row["task_key"]: row for row in connection.execute("SELECT * FROM hack_tasks WHERE campaign_id=? AND kind='lane'", (campaign_id,))}
            checked = {task.key for task in parsed if task.checked}
            for task in parsed:
                for dependency in task.depends_on:
                    if (not task.checked and dependency in checked
                            and (dependency not in existing or existing[dependency]["state"] != "merged")):
                        raise HackContractError(
                            f"task {task.key} depends on checked task {dependency} with no integrated result; "
                            "uncheck the dependency to implement it or remove the dependency if its contract already exists"
                        )
            live_footprints = {row["lane"]: json.loads(row["footprint"]) for row in existing.values() if row["state"] in {"running", "ready"}}
            for lane, footprint in live_footprints.items():
                if lane in lanes and tuple(footprint) != lanes[lane]:
                    raise HackContractError(f"lane {lane} footprint is frozen until running and ready work has drained")
                for other, other_paths in lanes.items():
                    if other != lane:
                        parse_lanes({lane: footprint, other: other_paths})
            incoming = {task.key for task in parsed}
            for key, row in existing.items():
                if row["board_owned"] and key not in incoming and row["state"] in {"queued", "blocked", "question"}:
                    connection.execute("UPDATE hack_tasks SET state='canceled',note='Removed from board',updated_at=? WHERE id=?", (utcnow(), row["id"]))
            for task in parsed:
                previous = existing.get(task.key)
                if previous and previous["state"] in {"running", "ready", "merged"}:
                    continue
                if previous:
                    changed = (previous["prompt"] != task.prompt or previous["lane"] != task.lane
                               or json.loads(previous["footprint"]) != list(task.footprint)
                               or json.loads(previous["depends_on"]) != list(task.depends_on))
                    state = "canceled" if task.checked else ("queued" if changed or previous["state"] == "canceled" else previous["state"])
                    self._update(connection, "hack_tasks", previous["id"], {
                        "lane": task.lane, "prompt": task.prompt, "footprint": json.dumps(task.footprint),
                        "depends_on": json.dumps(task.depends_on), "priority": task.priority, "state": state,
                        "note": "" if changed else previous["note"], "updated_at": utcnow(),
                    })
                elif not task.checked:
                    self._enqueue(connection, campaign_id, task.key, task.lane, task.prompt, task.footprint,
                                  task.depends_on, "lane", priority=task.priority, board_owned=True, validate_existing=False)
            effective_lanes: dict[str, list[str]] = {}
            for row in connection.execute(
                "SELECT lane,footprint FROM hack_tasks WHERE campaign_id=? AND kind='lane' AND state IN ('queued','running','ready')",
                (campaign_id,),
            ):
                effective_lanes.setdefault(row["lane"], []).extend(json.loads(row["footprint"]))
            if effective_lanes:
                parse_lanes(effective_lanes)
            connection.execute("UPDATE hack_campaigns SET board_hash=?,updated_at=? WHERE id=?", (source_hash or hashlib.sha256(content).hexdigest(), utcnow(), campaign_id))
            return [self._task(row) for row in connection.execute("SELECT * FROM hack_tasks WHERE campaign_id=? ORDER BY priority,created_at", (campaign_id,))]

    def list_tasks(self, campaign_id: str) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            return [self._task(row) for row in connection.execute("SELECT * FROM hack_tasks WHERE campaign_id=? ORDER BY priority,created_at", (campaign_id,))]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            return self._task(connection.execute("SELECT * FROM hack_tasks WHERE id=?", (task_id,)).fetchone())

    @staticmethod
    def _require_attempt(
        connection: sqlite3.Connection, task_id: str, expected_owner: str | None, expected_attempt: int | None,
    ) -> None:
        from .store import StoreError

        if expected_owner is None and expected_attempt is None:
            return
        row = connection.execute(
            "SELECT t.state,t.lease_owner,t.attempt,l.owner,l.expires_at FROM hack_tasks t "
            "LEFT JOIN leases l ON l.run_id=t.id AND l.run_kind=? WHERE t.id=?", (HACK_RUN_KIND, task_id),
        ).fetchone()
        if (row is None or row["state"] != "running" or row["expires_at"] is None or row["expires_at"] <= utcnow()
                or (expected_owner is not None and (row["lease_owner"] != expected_owner or row["owner"] != expected_owner))
                or (expected_attempt is not None and row["attempt"] != expected_attempt)):
            raise StoreError("hack task execution lease no longer belongs to this attempt")

    def update_task(
        self, task_id: str, *, expected_owner: str | None = None, expected_attempt: int | None = None, **fields: Any,
    ) -> dict[str, Any]:
        from .store import StoreError

        allowed = {"state", "base_commit", "result_commit", "prepared_commit", "prepared_base_commit", "implementation_commit", "worktree", "conversation_id", "session_id", "note", "error", "attempt", "finished_at"}
        if fields.keys() - allowed:
            raise StoreError("unsupported hack task fields: " + ", ".join(sorted(fields.keys() - allowed)))
        if "state" in fields and fields["state"] not in TASK_STATES:
            raise StoreError("invalid hack task state")
        fields["updated_at"] = utcnow()
        with self.store.transaction() as connection:
            self._require_attempt(connection, task_id, expected_owner, expected_attempt)
            self._update(connection, "hack_tasks", task_id, fields)
            return self._task(connection.execute("SELECT * FROM hack_tasks WHERE id=?", (task_id,)).fetchone())

    def claim_next(
        self, owner: str, lease_seconds: int, global_limit: int, provider_limits: dict[str, int],
        allowed_repositories: tuple[str, ...] | None = None, reserve_slots: int = 1,
    ) -> dict[str, Any] | None:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        with self.store.transaction() as connection:
            # The reservation remains available for controlled urgent work throughout a campaign.
            active = RepositoryLeases.active_count(connection, now)
            noncontrolled = connection.execute(
                "SELECT count(*) FROM leases WHERE expires_at>? AND run_kind!='github-issue'", (now,),
            ).fetchone()[0]
            if active >= global_limit or noncontrolled >= max(0, global_limit - max(0, reserve_slots)):
                return None
            counts = RepositoryLeases.provider_counts(connection, now)
            candidates = connection.execute(
                """SELECT t.* FROM hack_tasks t JOIN hack_campaigns c ON c.id=t.campaign_id
                   LEFT JOIN provider_backoff b ON b.provider=t.provider AND b.until_at>?
                   WHERE t.state='queued' AND c.expires_at>? AND b.provider IS NULL
                   AND NOT (t.kind='lane' AND EXISTS (
                       SELECT 1 FROM hack_tasks dispatcher WHERE dispatcher.campaign_id=t.campaign_id
                       AND dispatcher.kind='dispatcher' AND dispatcher.state IN ('queued','running')
                   ))
                   AND ((t.kind='scaffold' AND c.state='starting' AND c.stop_requested=0)
                     OR (t.kind IN ('lane','dispatcher') AND c.state='active' AND c.stop_requested=0)
                     OR (t.kind='polish' AND c.state IN ('draining','polishing')))
                   ORDER BY CASE t.kind WHEN 'scaffold' THEN 0 WHEN 'polish' THEN 1 WHEN 'dispatcher' THEN 2 ELSE 3 END,t.priority,t.created_at""",
                (now, now),
            ).fetchall()
            for row in candidates:
                if allowed_repositories is not None and row["repository"] not in allowed_repositories:
                    continue
                if counts.get(row["provider"], 0) >= provider_limits.get(row["provider"], 1):
                    continue
                campaign = connection.execute("SELECT * FROM hack_campaigns WHERE id=?", (row["campaign_id"],)).fetchone()
                if row["kind"] in {"lane", "dispatcher"}:
                    used = connection.execute(
                        "SELECT COALESCE(SUM(attempt),0) FROM hack_tasks WHERE campaign_id=? AND kind IN ('lane','dispatcher')",
                        (row["campaign_id"],),
                    ).fetchone()[0]
                    if used >= campaign["max_tasks"]:
                        continue
                leases = connection.execute("SELECT * FROM leases WHERE repository=?", (row["repository"],)).fetchall()
                if any(lease["run_kind"] != HACK_RUN_KIND for lease in leases) or len(leases) >= campaign["max_parallel"]:
                    continue
                unfinished = connection.execute(
                    "SELECT id,lane,kind,state FROM hack_tasks WHERE campaign_id=? AND state IN ('running','ready')",
                    (row["campaign_id"],),
                ).fetchall()
                if row["kind"] in {"scaffold", "polish"} and (unfinished or leases):
                    continue
                if any(task["lane"] == row["lane"] or task["kind"] in {"scaffold", "polish"} for task in unfinished):
                    continue
                if row["kind"] in {"lane", "dispatcher"} and not connection.execute(
                    "SELECT 1 FROM hack_tasks WHERE campaign_id=? AND kind='scaffold' AND state='merged'",
                    (row["campaign_id"],),
                ).fetchone():
                    continue
                dependencies = json.loads(row["depends_on"])
                if any(not connection.execute(
                    "SELECT 1 FROM hack_tasks WHERE campaign_id=? AND task_key=? AND state='merged'", (row["campaign_id"], dependency),
                ).fetchone() for dependency in dependencies):
                    continue
                concurrency_key = f"{row['repository']}/{row['lane']}"
                if any(lease["concurrency_key"] == concurrency_key for lease in leases):
                    continue
                seconds = min(lease_seconds, max(1, math.ceil((datetime.fromisoformat(campaign["expires_at"]) - now_dt).total_seconds())))
                lease = RepositoryLeases.claim(connection, ExecutionRef(HACK_RUN_KIND, row["id"], row["repository"], concurrency_key, row["provider"]), owner, seconds, now=now_dt)
                # Never round a lease beyond the hard campaign deadline.
                expires = min(lease.expires_at, campaign["expires_at"])
                connection.execute("UPDATE leases SET expires_at=? WHERE run_kind=? AND run_id=?", (expires, HACK_RUN_KIND, row["id"]))
                connection.execute(
                    """UPDATE hack_tasks SET state='running',lease_owner=?,lease_expires_at=?,heartbeat_at=?,started_at=?,
                       updated_at=?,base_commit=?,attempt=attempt+1,finished_at=NULL,
                       result_commit=NULL,prepared_commit=NULL,prepared_base_commit=NULL,implementation_commit=NULL,
                       conversation_id=NULL,session_id=NULL,error='',note='' WHERE id=?""",
                    (owner, expires, now, now, now, campaign["result_commit"] or campaign["campaign_commit"] or campaign["base_commit"], row["id"]),
                )
                return self._task(connection.execute("SELECT * FROM hack_tasks WHERE id=?", (row["id"],)).fetchone())
        return None

    def renew_lease(self, task_id: str, owner: str, seconds: int) -> bool:
        now_dt = datetime.now(UTC)
        with self.store.transaction() as connection:
            row = connection.execute("SELECT c.expires_at,c.state FROM hack_tasks t JOIN hack_campaigns c ON c.id=t.campaign_id WHERE t.id=? AND t.state='running'", (task_id,)).fetchone()
            if row is None or row["state"] not in ACTIVE_CAMPAIGN_STATES or row["expires_at"] <= now_dt.isoformat():
                return False
            expires = RepositoryLeases.renew(connection, HACK_RUN_KIND, task_id, owner, seconds, now=now_dt)
            if not expires:
                return False
            expires = min(expires, row["expires_at"])
            connection.execute("UPDATE leases SET expires_at=? WHERE run_kind=? AND run_id=?", (expires, HACK_RUN_KIND, task_id))
            connection.execute("UPDATE hack_tasks SET lease_expires_at=?,heartbeat_at=?,updated_at=? WHERE id=?", (expires, now_dt.isoformat(), now_dt.isoformat(), task_id))
            return True

    def finish_task(
        self, task_id: str, state: str, *, expected_owner: str | None = None, expected_attempt: int | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        from .store import StoreError

        if state not in TASK_STATES - {"queued", "running"}:
            raise StoreError("finished hack work must be ready, merged, blocked, question or canceled")
        allowed = {"base_commit", "result_commit", "worktree", "conversation_id", "session_id", "note", "error", "attempt"}
        if fields.keys() - allowed:
            raise StoreError("unsupported hack result fields")
        with self.store.transaction() as connection:
            self._require_attempt(connection, task_id, expected_owner, expected_attempt)
            self._update(connection, "hack_tasks", task_id, {
                **fields, "state": state, "lease_owner": None, "lease_expires_at": None,
                "updated_at": utcnow(), "finished_at": utcnow(),
            })
            RepositoryLeases.release(connection, HACK_RUN_KIND, task_id)
            return self._task(connection.execute("SELECT * FROM hack_tasks WHERE id=?", (task_id,)).fetchone())

    def expired_tasks(self) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            return [self._task(row) for row in connection.execute(
                "SELECT t.* FROM hack_tasks t JOIN leases l ON l.run_id=t.id AND l.run_kind=? WHERE l.expires_at<=? ORDER BY t.created_at",
                (HACK_RUN_KIND, utcnow()),
            )]

    def recover_expired_tasks(self, task_ids: set[str] | None = None) -> list[str]:
        """Call only after canceling the expired providers; failed work is never automatically retried."""
        recovered = []
        with self.store.transaction() as connection:
            rows = connection.execute(
                "SELECT t.id FROM hack_tasks t JOIN leases l ON l.run_id=t.id AND l.run_kind=? WHERE l.expires_at<=?",
                (HACK_RUN_KIND, utcnow()),
            ).fetchall()
            for row in rows:
                if task_ids is not None and row["id"] not in task_ids:
                    continue
                connection.execute(
                    "UPDATE hack_tasks SET state='blocked',lease_owner=NULL,lease_expires_at=NULL,note='Execution lease expired; edit the board to retry',updated_at=?,finished_at=? WHERE id=?",
                    (utcnow(), utcnow(), row["id"]),
                )
                RepositoryLeases.release(connection, HACK_RUN_KIND, row["id"])
                recovered.append(row["id"])
        return recovered

    def acquire_operation(self, campaign_id: str, owner: str, lease_seconds: int) -> bool:
        return self.store.acquire_operation_lock(f"hack:{campaign_id}", owner, lease_seconds)

    def renew_operation(self, campaign_id: str, owner: str, lease_seconds: int) -> bool:
        now = datetime.now(UTC)
        with self.store.transaction() as connection:
            return connection.execute(
                "UPDATE operation_locks SET expires_at=? WHERE name=? AND owner=? AND expires_at>?",
                ((now + timedelta(seconds=lease_seconds)).isoformat(), f"hack:{campaign_id}", owner, now.isoformat()),
            ).rowcount == 1

    def release_operation(self, campaign_id: str, owner: str) -> None:
        self.store.release_operation_lock(f"hack:{campaign_id}", owner)
