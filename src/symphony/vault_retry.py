"""Durable checkbox commands using the existing metadata and run tables."""
from __future__ import annotations

import hashlib
import json
import uuid

from .models import IdeaRun, IdeaRunState, IdeaSnapshot, utcnow
from .store import Store
from .vault_project import ProjectSnapshot

TERMINAL = {IdeaRunState.PUBLISHED, IdeaRunState.FAILED, IdeaRunState.QUESTION}


def _key(repository: str) -> str:
    return "vault-checkbox-controls:" + repository


def _read(connection, repository: str) -> dict:
    row = connection.execute("SELECT value FROM metadata WHERE key=?", (_key(repository),)).fetchone()
    return json.loads(row[0]) if row else {"boxes": {}, "pending": None}


def _write(connection, repository: str, value: dict) -> None:
    connection.execute("INSERT INTO metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (_key(repository), json.dumps(value, sort_keys=True)))


def spec_digest(spec: bytes) -> str:
    return hashlib.sha256(spec).hexdigest()


def pending_retry(store: Store, repository: str) -> dict | None:
    with store.connect() as connection:
        return _read(connection, repository)["pending"]


def observe_checkboxes(store: Store, repository: str, snapshot: ProjectSnapshot, latest: IdeaRun | None) -> set[str]:
    """Record an explicit checked-to-unchecked edge once, never ordinary pending work."""
    with store.transaction() as connection:
        state = _read(connection, repository)
        pending = state["pending"]
        digest = spec_digest(snapshot.spec)
        files = {item.key: item for item in snapshot.files}
        if pending and (pending["spec_digest"] != digest or not latest or pending["parent"] != latest.id):
            pending = None
        if pending:
            pending["sources"] = {key: value for key, value in pending["sources"].items()
                                  if key in files and not files[key].checked and files[key].content_hash == value}
            if not pending["sources"]:
                pending = None
        if latest and latest.spec_content == snapshot.spec and latest.state != IdeaRunState.SUPERSEDED:
            for item in snapshot.files:
                before = state["boxes"].get(item.key)
                if before is None:
                    # Upgrade path: only a recorded completion can establish a
                    # previous checked state before this feature's first pass.
                    row = connection.execute(
                        "SELECT content_hash,done FROM vault_checklist WHERE repository=? AND source=?",
                        (repository, item.key),
                    ).fetchone()
                    before = {"hash": row["content_hash"], "checked": bool(row["done"])} if row else {}
                if not item.checked and before.get("checked") and before.get("hash") == item.content_hash:
                    if pending is None:
                        pending = {"id": str(uuid.uuid4()), "parent": latest.id, "spec_digest": digest,
                                   "sources": {}, "requested_at": utcnow()}
                    pending["sources"][item.key] = item.content_hash
        state["pending"] = pending
        _write(connection, repository, state)
        return set(pending["sources"]) if pending else set()


def remember_checkboxes(store: Store, repository: str, snapshot: ProjectSnapshot) -> None:
    """Call only after the guarded source write succeeds (or requires no write)."""
    with store.transaction() as connection:
        state = _read(connection, repository)
        state["boxes"] = {item.key: {"hash": item.content_hash, "checked": item.checked} for item in snapshot.files}
        _write(connection, repository, state)


def consume_retry(store: Store, snapshot: IdeaSnapshot, provider: str) -> IdeaRun | None:
    """Create a new run against today's Git head; preserve every prior attempt."""
    repository = snapshot.repository
    with store.transaction() as connection:
        controls = _read(connection, repository)
        request = controls["pending"]
        if not request or request["spec_digest"] != spec_digest(snapshot.spec_content):
            return None
        latest = connection.execute("SELECT * FROM idea_runs WHERE repository=? ORDER BY rowid DESC LIMIT 1",
                                    (repository,)).fetchone()
        if latest is None or latest["id"] != request["parent"]:
            return None
        if latest["state"] not in TERMINAL or connection.execute(
            "SELECT 1 FROM leases WHERE repository=?", (repository,),
        ).fetchone():
            return None
        project = connection.execute("SELECT * FROM vault_projects WHERE repository=?", (repository,)).fetchone()
        if not project or project["mode"] != "idea" or project["desired_mode"] != "idea" or project["error"]:
            return None
        if not request["sources"] or latest["spec_content"] != snapshot.spec_content:
            return None
        for key, digest in request["sources"].items():
            row = connection.execute("SELECT content_hash FROM vault_checklist WHERE repository=? AND source=?",
                                     (repository, key)).fetchone()
            if row is None or row[0] != digest:
                return None
        run_id, now = str(uuid.uuid4()), utcnow()
        connection.execute(
            """INSERT INTO idea_runs(id,repository,spec_hash,spec_content,runtime_content,previous_progress,
               base_commit,default_branch,implementation_provider,state,phase,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,'queued','checkbox-retry',?,?)""",
            (run_id, repository, snapshot.spec_hash, snapshot.spec_content, snapshot.runtime_content,
             snapshot.previous_progress, snapshot.base_commit, snapshot.default_branch, provider, now, now),
        )
        connection.execute("UPDATE idea_projects SET latest_observed_spec_hash=?,updated_at=? WHERE repository=?",
                           (snapshot.spec_hash, now, repository))
        for key in request["sources"]:
            connection.execute("UPDATE vault_checklist SET done=0,completed_commit=NULL WHERE repository=? AND source=?",
                               (repository, key))
        connection.execute("INSERT INTO idea_run_events(run_id,at,kind,detail_json) VALUES (?,?,'checkbox-retry',?)",
                           (run_id, now, json.dumps(request, sort_keys=True)))
        controls["pending"] = None
        _write(connection, repository, controls)
    return store.get_idea_run_by_id(run_id)


def retry_sources(store: Store, run_id: str) -> set[str]:
    for event in store.idea_events(run_id):
        if event["kind"] == "checkbox-retry":
            return set(json.loads(event["detail_json"])["sources"])
    return set()


def task_runs(store: Store, snapshot: ProjectSnapshot, runs: list[IdeaRun]) -> dict[str, IdeaRun]:
    """A targeted retry does not replace other tasks' prior outcomes."""
    result = {}
    for run in reversed(runs):
        if run.spec_content != snapshot.spec:
            continue
        selected = retry_sources(store, run.id)
        for item in snapshot.files:
            if item.key not in result and (not selected or item.key in selected):
                result[item.key] = run
    return result
