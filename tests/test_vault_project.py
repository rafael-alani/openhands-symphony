from __future__ import annotations

from dataclasses import replace

import pytest
from test_vault import setup

from symphony.models import IdeaRunState, IdeaSnapshot, ValidationResult
from symphony.vault import VaultBridge, VaultError, read_note
from symphony.vault_markup import strip_annotations
from symphony.vault_project import compile_project


def project(tmp_path, checklist="- [ ] [[Feature]]"):
    bridge, note, store, backend = setup(tmp_path, body=(
        "# Dinner planner\n\nA quick useful app.\n\n" + checklist + "\n\n"
        "%% Begin Waypoint %%\n- [[Not a requirement]]\n%% End Waypoint %%\n"
    ))
    child = note.parent / "Feature.md"
    child.write_text("# Feature\n\n## Details\nLet users save a meal.\n")
    return bridge, note, child, store, backend


@pytest.mark.parametrize("row", [
    "- [ ] [[Feature]]",
    "- [ ] [[Feature.md|Meal saving]] — keep it local",
    "| [ ] | [[Feature\\|Meal saving]] | keep it local |",
    "| Feature | [ ] | [Meal saving](Feature.md) |",
    "- [x] [Meal saving](<Feature.md>)",
])
def test_checklist_and_table_read_subfiles_without_extra_yaml(tmp_path, row):
    bridge, note, child, store, backend = project(tmp_path, row)
    assert bridge.reconcile() == []
    repo = store.vault_projects()[0]["repository"]
    spec = backend.specs[repo]
    assert b"Let users save a meal." in spec
    assert b"## File: Feature.md" in spec
    assert b"Not a requirement" not in spec
    assert "Feature.md" in bridge.checklist_context(repo, spec)
    assert child.read_text().startswith("# Feature")


def test_waypoint_changes_and_status_ticks_do_not_trigger_new_spec(tmp_path):
    bridge, note, child, store, backend = project(tmp_path)
    bridge.reconcile()
    repo = store.vault_projects()[0]["repository"]
    spec = backend.specs[repo]
    note.write_text(note.read_text().replace("Not a requirement", "Another index entry").replace("- [ ]", "- [x]"))
    child.write_text(child.read_text() + "\n%% Begin Waypoint %%\n- [[Navigation]]\n%% End Waypoint %%\n")
    bridge.guard_spec(repo, spec)
    assert bridge.reconcile() == []
    assert backend.specs[repo] == spec


@pytest.mark.parametrize("body", ["\n", "%% Waypoint %%\n",
                                 "%% Begin Waypoint %%\n- [[General Idea]]\n%% End Waypoint %%\n"])
def test_empty_project_waits_for_explicit_brief_and_recovers_on_note_edit(tmp_path, body):
    bridge, note, store, backend = setup(tmp_path, body=body)
    (note.parent / "General Idea.md").write_text("Use the documented Immich API to archive external photos.\n")
    original = note.read_bytes()
    for _ in range(2):
        errors = bridge.reconcile()
        assert any("outside Waypoint" in error for error in errors)
        assert not backend.repos
        assert not backend.specs
        assert note.read_bytes() == original
        assert store.vault_projects()[0]["mode"] == "paused"
    status = (bridge.base_config.vault.path / "_symphony/STATUS.md").read_text()
    assert "outside Waypoint" in status

    note.write_text(note.read_text() + "\n- [ ] [[General Idea]]\n")
    assert bridge.reconcile() == []
    project = store.vault_projects()[0]
    assert project["mode"] == "idea"
    assert len(backend.repos) == 1
    assert b"Use the documented Immich API" in backend.specs[project["repository"]]


def test_checklist_reopens_changed_file_and_survives_restart(tmp_path):
    bridge, note, child, store, backend = project(tmp_path)
    assert bridge.reconcile() == []
    repo = store.vault_projects()[0]["repository"]
    compiled = compile_project(read_note(note), repo, bridge.base_config.vault.path)
    store.complete_vault_checklist(repo, [(item.key, item.content_hash) for item in compiled.files], "published-commit")
    bridge = VaultBridge(bridge.base_config, store, backend)
    assert bridge.reconcile() == []
    assert "- [x] [[Feature]]" in note.read_text()
    previous = backend.specs[repo]
    child.write_text(child.read_text() + "Allow editing the saved meal.\n")
    with pytest.raises(VaultError, match="changed"):
        bridge.guard_spec(repo, previous)
    assert bridge.reconcile() == []
    assert "- [ ] [[Feature]]" in note.read_text()
    assert backend.specs[repo] != previous
    assert "- [ ] Feature.md" in bridge.checklist_context(repo, backend.specs[repo])


@pytest.mark.parametrize("problem", ["missing", "outside", "symlink", "conflict", "duplicate", "ambiguous", "nested-project"])
def test_invalid_linked_inputs_pause_before_any_github_write(tmp_path, problem):
    bridge, note, child, store, backend = project(tmp_path)
    if problem == "missing":
        child.unlink()
    elif problem == "outside":
        note.write_text(note.read_text().replace("[[Feature]]", "[[../Feature]]"))
    elif problem == "symlink":
        target = tmp_path / "outside.md"
        target.write_text("private unrelated note")
        child.unlink()
        child.symlink_to(target)
    elif problem == "conflict":
        child.with_name("Feature.sync-conflict-20260919-device.md").write_text("competing text")
    elif problem == "duplicate":
        note.write_text(note.read_text() + "\n- [ ] [[Feature]]\n")
    elif problem == "ambiguous":
        child.unlink()
        for folder in ("one", "two"):
            (note.parent / folder).mkdir()
            (note.parent / folder / "Feature.md").write_text("ambiguous feature")
    elif problem == "nested-project":
        child.write_text("---\nsymphony: idea\n---\nAnother project")
    assert bridge.reconcile()
    own = next(p for p in store.vault_projects() if p["note_path"] == str(note))
    assert own["mode"] == "paused"
    assert own["repository"] not in backend.repos


def test_debounce_includes_subfile_and_race_during_github_sync_is_detected(tmp_path, monkeypatch):
    import os
    import time

    bridge, note, child, store, backend = project(tmp_path)
    old = time.time() - 1000
    os.utime(note, (old, old))
    bridge.base_config = replace(bridge.base_config, vault=replace(bridge.base_config.vault, quiet_seconds=30))
    assert bridge.reconcile() == []
    assert not backend.repos
    os.utime(child, (old, old))
    original = backend.sync_spec

    def racing_sync(*args):
        original(*args)
        child.write_text("a new concurrent requirement")

    monkeypatch.setattr(backend, "sync_spec", racing_sync)
    assert bridge.reconcile()
    assert store.vault_projects()[0]["mode"] == "paused"


def test_code_examples_are_not_followed_and_bom_crlf_survive_checkbox_update(tmp_path):
    bridge, note, child, store, backend = project(tmp_path)
    body = note.read_text() + "\n```markdown\n- [ ] [[Does not exist]]\n```\n"
    note.write_bytes(b"\xef\xbb\xbf" + body.replace("\n", "\r\n").encode())
    assert bridge.reconcile() == []
    repo = store.vault_projects()[0]["repository"]
    compiled = compile_project(read_note(note), repo, bridge.base_config.vault.path)
    assert len(compiled.files) == 1
    before = note.read_bytes()
    store.complete_vault_checklist(repo, [(compiled.files[0].key, compiled.files[0].content_hash)], "commit")
    assert bridge.reconcile() == []
    expected = strip_annotations(before.decode("utf-8-sig")).replace("- [ ] [[Feature]]", "- [x] [[Feature]]")
    assert note.read_bytes().startswith(b"\xef\xbb\xbf")
    assert strip_annotations(note.read_bytes().decode("utf-8-sig")) == expected


@pytest.mark.parametrize("outcome", ["success", "question", "failed", "partial"])
def test_checkbox_completion_uses_published_run_and_passing_validation(tmp_path, outcome):
    bridge, note, _, store, backend = project(tmp_path)
    bridge.reconcile()
    repo = store.vault_projects()[0]["repository"]
    spec = backend.specs[repo]
    run, _, _ = store.ensure_idea_run(IdeaSnapshot(repo, "a" * 40, spec, b"", b"", "b" * 40, "main"), "codex")
    claimed = store.claim_next_idea("test", 60, 2, {"codex": 2})
    if outcome == "partial":
        store.record_idea_validation(run.id, claimed.attempt, ValidationResult(("false",), 1, "start", "end", "failed"))
    state = {"question": IdeaRunState.QUESTION, "failed": IdeaRunState.FAILED}.get(outcome, IdeaRunState.PUBLISHED)
    store.transition_idea_run(run.id, state, published_commit="published-commit" if state == IdeaRunState.PUBLISHED else None)
    assert bridge.reconcile() == []
    assert ("- [x] [[Feature]]" in note.read_text()) == (outcome == "success")


def test_manual_checkbox_option_does_not_modify_status_cells(tmp_path):
    bridge, note, _, store, backend = project(tmp_path, "- [x] [[Feature]]")
    bridge.base_config = replace(bridge.base_config, vault=replace(bridge.base_config.vault, manage_checkboxes=False))
    bridge.reconcile()
    (note.parent / "Feature.md").write_text("new requirement")
    bridge.reconcile()
    assert "- [x] [[Feature]]" in note.read_text()


def test_vault_relative_links_and_nested_paths(tmp_path):
    bridge, note, child, store, backend = project(tmp_path, "- [ ] [[1. Projects & Tasks/details/Feature]]")
    (note.parent / "details").mkdir()
    child.rename(note.parent / "details/Feature.md")
    assert bridge.reconcile() == []
    repo = store.vault_projects()[0]["repository"]
    assert b"## File: details/Feature.md" in backend.specs[repo]
