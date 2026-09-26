from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_vault_project import project
from test_vault_status import observe

from symphony.ideas_coordinator import IdeasCoordinator
from symphony.models import IdeaRunState, IdeaSnapshot, ValidationResult
from symphony.vault import VaultBridge
from symphony.vault_history import run_history
from symphony.vault_markup import VaultError, strip_annotations
from symphony.vault_retry import consume_retry, pending_retry, retry_sources
from symphony.workspace import WorkspaceError


def finished(tmp_path, state=IdeaRunState.PUBLISHED, attempts=1):
    bridge, note, child, store, backend = project(tmp_path, "- [ ] [[Feature]]\n- [ ] [[Other]]")
    (note.parent / "Other.md").write_text("Keep the other feature.\n")
    assert bridge.reconcile() == []
    repository, run = observe(bridge, note, store)
    for attempt in range(attempts):
        store.claim_next_idea("worker", 60, 2, {"codex": 2})
        current = store.begin_idea_attempt(run.id, conversation_id=f"conversation-{attempt + 1}")
        store.record_idea_validation(run.id, current.attempt, ValidationResult(
            ("quality-gate",), 0 if state == IdeaRunState.PUBLISHED else 1, "start", "end", "test evidence"))
        store.transition_idea_run(run.id, state if attempt == attempts - 1 else IdeaRunState.QUEUED,
                                  phase="published" if state == IdeaRunState.PUBLISHED else "provider-failed",
                                  question="retained prior error" if state == IdeaRunState.FAILED else "",
                                  published_commit="c" * 40 if state == IdeaRunState.PUBLISHED else None)
    assert bridge.reconcile() == []
    prior = store.get_idea_run_by_id(run.id)
    snapshot = IdeaSnapshot(repository, prior.spec_hash, prior.spec_content, prior.runtime_content,
                            b"latest Git result\n", "d" * 40, "main")
    return bridge, note, child, store, backend, prior, snapshot


def uncheck(note, label="Feature"):
    note.write_text(note.read_text().replace(f"- [x] [[{label}]]", f"- [ ] [[{label}]]"))


@pytest.mark.parametrize("state", [IdeaRunState.PUBLISHED, IdeaRunState.FAILED, IdeaRunState.QUESTION])
def test_uncheck_creates_one_new_run_and_preserves_old_attempts(tmp_path, state):
    bridge, note, child, store, backend, prior, snapshot = finished(tmp_path, state, attempts=3)
    old_events, old_checks = store.idea_events(prior.id), store.idea_validations(prior.id)
    old_child = child.read_bytes()
    assert "- [x] [[Feature]]" in note.read_text()
    uncheck(note)
    source = strip_annotations(note.read_text())
    assert bridge.reconcile() == []
    assert "[Retry requested]" in note.read_text()
    assert backend.specs[snapshot.repository] == prior.spec_content
    bridge = VaultBridge(bridge.base_config, store, backend)  # process restart retains the request
    assert bridge.reconcile() == []
    bridge.guard_retry(snapshot.repository, snapshot.spec_content)
    retry = consume_retry(store, snapshot, "codex")
    assert retry.id != prior.id and retry.state == IdeaRunState.QUEUED and retry.attempt == 0
    assert retry.base_commit == "d" * 40 and retry.previous_progress == b"latest Git result\n"
    assert retry_sources(store, retry.id) == {"Feature.md"}
    assert consume_retry(store, snapshot, "codex") is None
    for _ in range(3):
        assert bridge.reconcile() == []
        assert consume_retry(store, snapshot, "codex") is None
    assert len(store.list_idea_runs()) == 2
    assert store.get_idea_run_by_id(prior.id) == prior
    assert store.idea_events(prior.id) == old_events
    assert store.idea_validations(prior.id) == old_checks
    assert strip_annotations(note.read_text()) == source
    assert child.read_bytes() == old_child
    context = bridge.checklist_context(snapshot.repository, snapshot.spec_content, retry.id)
    assert "- [ ] Feature.md" in context
    if state == IdeaRunState.PUBLISHED:
        assert "- [x] Other.md" in context
    else:
        assert "Deferred (not selected): Other.md" in context
    assert "Run history" not in backend.specs[snapshot.repository].decode()


def test_automatic_checkbox_projection_does_not_request_retry(tmp_path):
    bridge, note, _, store, _, prior, snapshot = finished(tmp_path, IdeaRunState.FAILED)
    for _ in range(4):
        assert bridge.reconcile() == []
        assert pending_retry(store, snapshot.repository) is None
        assert consume_retry(store, snapshot, "codex") is None
    assert len(store.list_idea_runs()) == 1
    assert "[Failed]" in note.read_text() and "[Completed]" not in note.read_text()
    with store.connect() as connection:
        assert not any(row[0] for row in connection.execute("SELECT done FROM vault_checklist"))
    assert store.get_idea_run_by_id(prior.id) == prior


def test_plain_unchecked_task_does_not_create_a_retry_command(tmp_path):
    bridge, note, _, store, _ = project(tmp_path)
    bridge.reconcile()
    repository, _ = observe(bridge, note, store)
    for _ in range(3):
        assert bridge.reconcile() == []
        assert pending_retry(store, repository) is None
    assert "- [ ] [[Feature]]" in note.read_text()


@pytest.mark.parametrize("edit", ["recheck", "content", "remove"])
def test_changed_or_canceled_checkbox_command_does_not_retry_old_input(tmp_path, edit):
    bridge, note, child, store, _, _, snapshot = finished(tmp_path)
    uncheck(note)
    bridge.reconcile()
    if edit == "recheck":
        note.write_text(note.read_text().replace("- [ ] [[Feature]]", "- [x] [[Feature]]"))
    elif edit == "content":
        child.write_text("New input needs a normal new-spec run.\n")
    else:
        note.write_text("\n".join(line for line in note.read_text().splitlines() if not line.startswith("- [ ] [[Feature]]")) + "\n")
    with pytest.raises(VaultError):
        bridge.guard_retry(snapshot.repository, snapshot.spec_content)
    assert bridge.reconcile() == []
    assert pending_retry(store, snapshot.repository) is None
    assert consume_retry(store, snapshot, "codex") is None
    assert len(store.list_idea_runs()) == 1


def test_multiple_unchecks_and_concurrent_intake_create_only_one_run(tmp_path):
    bridge, note, _, store, _, _, snapshot = finished(tmp_path)
    uncheck(note)
    uncheck(note, "Other")
    bridge.reconcile()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: consume_retry(store, snapshot, "codex"), range(2)))
    retried = [run for run in results if run is not None]
    assert len(retried) == 1
    assert retry_sources(store, retried[0].id) == {"Feature.md", "Other.md"}
    assert len(store.list_idea_runs()) == 2


def test_retry_acceptance_serializes_with_another_vault_projection(tmp_path):
    bridge, note, _, store, _, _, snapshot = finished(tmp_path)
    uncheck(note)
    bridge.reconcile()
    assert store.acquire_operation_lock("vault-intake", "timer", 3600)
    assert bridge.consume_retry(snapshot, "codex") is None
    assert len(store.list_idea_runs()) == 1
    store.release_operation_lock("vault-intake", "timer")
    assert bridge.consume_retry(snapshot, "codex") is not None
    assert bridge.consume_retry(snapshot, "codex") is None
    assert store.acquire_operation_lock("vault-intake", "timer", 3600)


@pytest.mark.parametrize("mode", ["paused", "github"])
def test_paused_and_github_projects_do_not_consume_retry(tmp_path, mode):
    bridge, note, _, store, _, _, snapshot = finished(tmp_path)
    uncheck(note)
    bridge.reconcile()
    note.write_text(note.read_text().replace("symphony: idea", f"symphony: {mode}"))
    assert bridge.reconcile() == []
    assert consume_retry(store, snapshot, "codex") is None
    assert len(store.list_idea_runs()) == 1


def test_retry_during_active_work_waits_for_run_to_finish(tmp_path):
    bridge, note, child, store, _, _, snapshot = finished(tmp_path)
    child.write_text(child.read_text() + "New feature requirement\n")
    bridge.reconcile()
    repository, active = observe(bridge, note, store)
    store.claim_next_idea("worker", 60, 2, {"codex": 2})
    assert bridge.reconcile() == []
    uncheck(note, "Other")  # the previously completed task is still checked
    assert bridge.reconcile() == []
    current = replace(snapshot, spec_content=active.spec_content, spec_hash=active.spec_hash)
    assert consume_retry(store, current, "codex") is None
    store.transition_idea_run(active.id, IdeaRunState.PUBLISHED, published_commit="e" * 40)
    assert bridge.reconcile() == []
    retry = consume_retry(store, current, "codex")
    assert retry is not None and retry_sources(store, retry.id) == {"Other.md"}
    assert "- [x] Feature.md" in bridge.checklist_context(repository, active.spec_content)


def test_disabled_checkbox_management_does_not_accept_unchecks(tmp_path):
    bridge, note, _, store, _, _, snapshot = finished(tmp_path)
    bridge.base_config = replace(bridge.base_config, vault=replace(bridge.base_config.vault, manage_checkboxes=False))
    uncheck(note)
    assert bridge.reconcile() == []
    assert pending_retry(store, snapshot.repository) is None
    status = bridge.base_config.vault.path / "_symphony" / snapshot.repository.replace("/", "--") / "STATUS.md"
    assert "## Retrying a task" not in status.read_text()


def test_upgrade_can_recognize_unchecked_recorded_completion_without_a_prior_control_ledger(tmp_path):
    bridge, note, _, store, _, _, snapshot = finished(tmp_path)
    with store.transaction() as connection:
        connection.execute("DELETE FROM metadata WHERE key=?", ("vault-checkbox-controls:" + snapshot.repository,))
    uncheck(note)
    assert bridge.reconcile() == []
    assert consume_retry(store, snapshot, "codex") is not None
    assert bridge.reconcile() == []
    assert consume_retry(store, snapshot, "codex") is None


@pytest.mark.parametrize("row", ["- [ ] [[Feature]]", "| [ ] | [[Feature]] | keep this instruction |"])
def test_checkbox_retry_preserves_bom_crlf_waypoint_and_human_source(tmp_path, row):
    bridge, note, child, store, _ = project(tmp_path, row)
    note.write_bytes(b"\xef\xbb\xbf" + note.read_bytes().replace(b"\n", b"\r\n"))
    child_before = child.read_bytes()
    bridge.reconcile()
    repository, prior = observe(bridge, note, store)
    store.claim_next_idea("worker", 60, 2, {"codex": 2})
    store.transition_idea_run(prior.id, IdeaRunState.PUBLISHED, published_commit="c" * 40)
    bridge.reconcile()
    note.write_bytes(note.read_bytes().replace(b"[x]", b"[ ]", 1))
    source = strip_annotations(note.read_bytes().decode("utf-8-sig"))
    assert bridge.reconcile() == []
    assert note.read_bytes().startswith(b"\xef\xbb\xbf")
    assert strip_annotations(note.read_bytes().decode("utf-8-sig")) == source
    assert child.read_bytes() == child_before
    snapshot = IdeaSnapshot(repository, prior.spec_hash, prior.spec_content, b"", b"", "d" * 40, "main")
    assert consume_retry(store, snapshot, "codex") is not None


def test_completion_recovery_does_not_recheck_a_queued_retry(tmp_path):
    bridge, note, _, store, backend, _, snapshot = finished(tmp_path)
    uncheck(note)
    bridge.reconcile()
    retry = consume_retry(store, snapshot, "codex")
    restarted = VaultBridge(bridge.base_config, store, backend)
    for _ in range(3):
        assert restarted.reconcile() == []
        assert "- [ ] [[Feature]]" in note.read_text()
        assert "[Queued]" in note.read_text()
    store.claim_next_idea("worker", 60, 2, {"codex": 2})
    store.transition_idea_run(retry.id, IdeaRunState.PUBLISHED, published_commit="f" * 40)
    assert restarted.reconcile() == []
    assert "- [x] [[Feature]]" in note.read_text()
    assert "[Completed]" in note.read_text()
    assert f"/blob/{'f' * 40}/idea/PROGRESS.md" in note.read_text()


def test_history_lives_at_bottom_of_reports_and_preserves_attempts(tmp_path):
    bridge, note, _, store, _, prior, snapshot = finished(tmp_path, IdeaRunState.FAILED, attempts=3)
    uncheck(note)
    bridge.reconcile()
    retry = consume_retry(store, snapshot, "codex")
    assert bridge.reconcile() == []
    folder = bridge.base_config.vault.path / "_symphony" / snapshot.repository.replace("/", "--")
    for name in ("STATUS.md", "PROGRESS.md"):
        text = (folder / name).read_text()
        assert text.count("## Run history") == 1
        assert f"### Run {prior.id}" in text and f"### Run {retry.id}" in text
        assert text.index(f"### Run {retry.id}") < text.index(f"### Run {prior.id}")
        assert "**Attempt 1**" in text and "**Attempt 3**" in text
        assert "retained prior error" in text and "Exit 1" in text
        assert "Retry of:" in text
        before = (folder / name).read_bytes()
        assert bridge.reconcile() == []
        assert (folder / name).read_bytes() == before
    assert "Run history" not in note.read_text()
    assert "Attempt 1" not in note.read_text()
    note.write_text(note.read_text().replace("Current status", "Edited status"))
    assert bridge.reconcile()
    assert f"### Run {prior.id}" in (folder / "STATUS.md").read_text()


def test_history_retains_immutable_result_links_and_omits_noisy_configuration(tmp_path):
    _, _, _, store, _, prior, snapshot = finished(tmp_path)
    with store.transaction() as connection:
        connection.execute("UPDATE idea_runs SET question=? WHERE id=?", ('"acp_file_secrets": [configuration]', prior.id))
    history = run_history(store, snapshot.repository, "idea/PROGRESS.md")
    assert f"/blob/{'c' * 40}/idea/PROGRESS.md" in history
    assert '"acp_file_secrets"' not in history
    assert "without a readable error summary" in history


def test_successful_targeted_retry_does_not_mark_other_failed_tasks_completed(tmp_path):
    bridge, note, _, store, _, _, snapshot = finished(tmp_path, IdeaRunState.FAILED)
    uncheck(note)
    bridge.reconcile()
    retry = consume_retry(store, snapshot, "codex")
    store.claim_next_idea("worker", 60, 2, {"codex": 2})
    store.transition_idea_run(retry.id, IdeaRunState.PUBLISHED, published_commit="f" * 40)
    assert bridge.reconcile() == []
    with store.connect() as connection:
        done = dict(connection.execute("SELECT source,done FROM vault_checklist"))
    assert done == {"Feature.md": 1, "Other.md": 0}
    lines = note.read_text().splitlines()
    assert "[Completed]" in next(line for line in lines if "[[Feature]]" in line)
    assert "[Failed]" in next(line for line in lines if "[[Other]]" in line)


@pytest.mark.parametrize("change", ["none", "remote", "local", "not-retry"])
def test_unchanged_retry_can_publish_only_the_exact_validated_base(tmp_path, change):
    bridge, note, _, store, _, _, snapshot = finished(tmp_path)
    uncheck(note)
    bridge.reconcile()
    retry = consume_retry(store, snapshot, "codex")
    if change == "not-retry":
        with store.transaction() as connection:
            connection.execute("DELETE FROM idea_run_events WHERE run_id=?", (retry.id,))
    coordinator = IdeasCoordinator.__new__(IdeasCoordinator)
    coordinator.config, coordinator.store = bridge.base_config, store
    coordinator.workspaces = SimpleNamespace(changed_paths=lambda *args: (),
        head=lambda *args: "f" * 40 if change == "local" else snapshot.base_commit)
    coordinator._guard_publication = lambda *args: replace(snapshot, base_commit="e" * 40) if change == "remote" else snapshot
    if change == "none":
        assert coordinator._publish(retry, tmp_path, question_only=False) == snapshot.base_commit
        assert any(event["kind"] == "retry-unchanged" for event in store.idea_events(retry.id))
    else:
        with pytest.raises(WorkspaceError, match="no committable progress"):
            coordinator._publish(retry, tmp_path, question_only=False)
