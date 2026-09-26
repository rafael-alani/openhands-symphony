from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from urllib.parse import unquote, urlsplit

import pytest
from test_vault_project import project

from symphony.models import IdeaRunState, IdeaSnapshot, ValidationResult
from symphony.vault import VaultBridge, read_note, replace_note
from symphony.vault_markup import VaultError, annotation, strip_annotations
from symphony.vault_project import compile_project


def observe(bridge, note, store):
    repository = store.vault_projects()[0]["repository"]
    spec = compile_project(read_note(note), repository, bridge.base_config.vault.path).spec
    run, _, _ = store.ensure_idea_run(IdeaSnapshot(
        repository, hashlib.sha1(spec).hexdigest(), spec, b"", b"", "b" * 40, "main"), "codex")
    return repository, run


@pytest.mark.parametrize("row", ["- [ ] [[Feature]]", "| [ ] | [[Feature]] | keep it local |"])
@pytest.mark.parametrize("ending", ["\n", "\r\n", ""])
def test_status_projection_preserves_source_and_never_changes_spec(tmp_path, row, ending):
    bridge, note, child, store, backend = project(tmp_path, row)
    note.write_bytes(b"\xef\xbb\xbf" + note.read_text().rstrip().replace("\n", ending or "\n").encode() + ending.encode())
    before = note.read_bytes()
    child_before = child.read_bytes()
    assert bridge.reconcile() == []
    repository, run = observe(bridge, note, store)
    spec = backend.specs[repository]
    source = strip_annotations(note.read_bytes().decode("utf-8-sig"))
    assert source.encode() == before[3:].replace(b"---", b"---" + (b"\r\n" if ending == "\r\n" else b"\n")
                                               + f"repo: {repository}".encode(), 1)
    assert "[Repository]" in note.read_text()
    assert "[Current status]" in note.read_text()
    assert "[Latest result]" in note.read_text()
    for _ in range(3):
        assert bridge.reconcile() == []
        stable = note.read_bytes()
        assert bridge.reconcile() == []
        assert note.read_bytes() == stable
        assert backend.specs[repository] == spec
        bridge.guard_spec(repository, spec)
    assert child.read_bytes() == child_before
    assert note.read_bytes().startswith(b"\xef\xbb\xbf")
    assert strip_annotations(note.read_bytes().decode("utf-8-sig")) == source


@pytest.mark.parametrize("state,label", [(IdeaRunState.QUEUED, "Queued"), (IdeaRunState.RUNNING, "Running"),
    (IdeaRunState.QUESTION, "Needs guidance"), (IdeaRunState.FAILED, "Failed"), (IdeaRunState.PUBLISHED, "Completed")])
def test_task_links_follow_recorded_run_and_completion(tmp_path, state, label):
    bridge, note, _, store, backend = project(tmp_path)
    bridge.reconcile()
    repository, run = observe(bridge, note, store)
    if state == IdeaRunState.RUNNING:
        store.claim_next_idea("worker", 60, 2, {"codex": 2})
    elif state != IdeaRunState.QUEUED:
        store.claim_next_idea("worker", 60, 2, {"codex": 2})
        store.transition_idea_run(run.id, state, phase=str(state),
                                  published_commit="c" * 40 if state == IdeaRunState.PUBLISHED else None)
    assert bridge.reconcile() == []
    text = note.read_text()
    assert f"[{label}]" in text
    assert "STATUS.md#Task%20" in text
    status = bridge.base_config.vault.path / "_symphony" / repository.replace("/", "--") / "STATUS.md"
    assert f"**Feature.md** — {label}" in status.read_text()
    if state == IdeaRunState.PUBLISHED:
        assert "- [x] [[Feature]]" in text
        assert f"/blob/{'c' * 40}/idea/PROGRESS.md" in text
    assert "symphony-task:" not in backend.specs[repository].decode()


def test_new_requirement_never_inherits_old_completion_link(tmp_path):
    bridge, note, child, store, backend = project(tmp_path)
    bridge.reconcile()
    repository, run = observe(bridge, note, store)
    store.claim_next_idea("worker", 60, 2, {"codex": 2})
    store.transition_idea_run(run.id, IdeaRunState.PUBLISHED, published_commit="c" * 40)
    bridge.reconcile()
    assert "[Result]" in note.read_text()
    child.write_text(child.read_text() + "\nNew requirement\n")
    assert bridge.reconcile() == []
    assert "[Pending]" in note.read_text()
    assert "- [ ] [[Feature]]" in note.read_text()
    assert "[Result]" not in note.read_text()
    assert backend.specs[repository] != run.spec_content


def test_partial_validation_does_not_claim_completed_task(tmp_path):
    bridge, note, _, store, _ = project(tmp_path)
    bridge.reconcile()
    _, run = observe(bridge, note, store)
    store.claim_next_idea("worker", 60, 2, {"codex": 2})
    store.record_idea_validation(run.id, run.attempt, ValidationResult(("false",), 1, "start", "end", "failed"))
    store.transition_idea_run(run.id, IdeaRunState.PUBLISHED, published_commit="c" * 40)
    bridge.reconcile()
    assert "Partial — validation needs attention" in note.read_text()
    assert "- [ ] [[Feature]]" in note.read_text()
    assert "[Completed]" not in note.read_text()


def test_manually_checked_task_is_not_claimed_as_published(tmp_path):
    bridge, note, _, _, _ = project(tmp_path, "- [x] [[Feature]]")
    bridge.reconcile()
    assert "[Checked]" in note.read_text()
    assert "[Completed]" not in note.read_text()
    assert "[Result]" not in note.read_text()


@pytest.mark.parametrize("edit", ["payload", "marker", "duplicate"])
def test_edited_or_malformed_owned_markup_is_preserved_and_paused(tmp_path, edit):
    bridge, note, _, store, backend = project(tmp_path)
    bridge.reconcile()
    text = note.read_text()
    if edit == "payload":
        text = text.replace("Current status", "My personal words")
    elif edit == "marker":
        text = text.replace("<!-- /symphony-status -->", "")
    else:
        text += annotation("status", "\nAnother block\n")
    note.write_text(text)
    before = note.read_bytes()
    calls = len(backend.calls)
    assert bridge.reconcile()
    assert note.read_bytes() == before
    assert len(backend.calls) == calls
    assert store.vault_projects()[0]["mode"] == "paused"
    repository = store.vault_projects()[0]["repository"]
    status = bridge.base_config.vault.path / "_symphony" / repository.replace("/", "--") / "STATUS.md"
    assert "Needs attention" in status.read_text()
    assert "## Task " in status.read_text()


def test_status_write_preserves_two_concurrent_edits_in_recovery(tmp_path, monkeypatch):
    from symphony import graduation

    _, path, _, _, _ = project(tmp_path)
    note = read_note(path)
    first, second = note.raw + b"\nFirst human edit", note.raw + b"\nSecond human edit"
    real_exchange = graduation._atomic_exchange
    calls = 0

    def race(left, right):
        nonlocal calls
        calls += 1
        left.write_bytes(first if calls == 1 else second)
        real_exchange(left, right)

    monkeypatch.setattr(graduation, "_atomic_exchange", race)
    backup = tmp_path / "originals"
    with pytest.raises(VaultError, match="concurrent bytes preserved"):
        replace_note(note, note.raw + annotation("status", "\nStatus\n").encode(), backup)
    assert path.read_bytes() == first
    for content in (note.raw, first, second):
        assert (backup / f"{hashlib.sha256(content).hexdigest()}.md").read_bytes() == content


def test_status_edits_during_reconcile_do_not_overwrite_human_changes(tmp_path, monkeypatch):
    bridge, note, _, store, _ = project(tmp_path)
    bridge.reconcile()
    observe(bridge, note, store)
    original = bridge._safe_output
    edited = note.read_bytes() + b"\nMy new prose"

    def racing_output(relative, content):
        original(relative, content)
        if relative.name == "STATUS.md" and relative.parent.name != ".":
            note.write_bytes(edited)

    monkeypatch.setattr(bridge, "_safe_output", racing_output)
    assert bridge.reconcile()
    assert note.read_bytes() == edited


def test_disabling_checkbox_management_still_updates_status_links(tmp_path):
    bridge, note, _, store, _ = project(tmp_path)
    bridge.base_config = replace(bridge.base_config, vault=replace(bridge.base_config.vault, manage_checkboxes=False))
    bridge.reconcile()
    _, run = observe(bridge, note, store)
    store.claim_next_idea("worker", 60, 2, {"codex": 2})
    store.transition_idea_run(run.id, IdeaRunState.PUBLISHED, published_commit="c" * 40)
    bridge.reconcile()
    assert "[Completed]" in note.read_text()
    assert "- [ ] [[Feature]]" in note.read_text()
    reloaded = VaultBridge(bridge.base_config, store, bridge.backend)
    assert reloaded.reconcile() == []


def test_final_task_without_newline_and_every_generated_local_link_resolves(tmp_path):
    bridge, note, _, store, _ = project(tmp_path)
    note.write_text(note.read_text().replace("- [ ] [[Feature]]", "") + "- [ ] [[Feature]]")
    bridge.reconcile()
    repository, run = observe(bridge, note, store)
    store.claim_next_idea("worker", 60, 2, {"codex": 2})
    store.transition_idea_run(run.id, IdeaRunState.PUBLISHED, published_commit="c" * 40)
    for _ in range(2):
        assert bridge.reconcile() == []
    source = strip_annotations(note.read_text())
    assert source.endswith("- [x] [[Feature]]")
    for url in re.findall(r"\]\(([^)]+)\)", note.read_text()):
        parsed = urlsplit(url)
        if parsed.scheme:
            assert url.startswith(f"https://github.com/{repository}")
            continue
        target = (note.parent / unquote(parsed.path)).resolve()
        assert target.is_file()
        if parsed.fragment:
            assert f"## {unquote(parsed.fragment)}\n" in target.read_text()


@pytest.mark.parametrize("mode,label", [("paused", "Paused"), ("github", "GitHub issue workflow")])
def test_workflow_change_refreshes_original_note_without_rescheduling(tmp_path, mode, label):
    bridge, note, _, store, backend = project(tmp_path)
    bridge.reconcile()
    note.write_text(note.read_text().replace("symphony: idea", f"symphony: {mode}"))
    assert bridge.reconcile() == []
    assert f"Symphony: {label}" in note.read_text()
    assert not store.list_idea_runs()
