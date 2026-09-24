from __future__ import annotations

import pytest

from symphony.ideas_progress import SectionResult, affected_sections, mirrored_spec, render_progress, sections

SPEC = (
    b"---\nsymphony: idea\nrepo: solo/idea\n---\n\n"
    b"# Planner\n\nKeep all data local.\n\n"
    b"## Meals\nChoose a meal.\n\n"
    b"## Shopping\nShow the ingredients.\n"
)


@pytest.mark.parametrize(
    "current",
    [
        SPEC.replace(b"Keep all data local.", b"Synchronize data across devices."),
        SPEC[: SPEC.index(b"## Shopping")],
        SPEC[: SPEC.index(b"## Meals")]
        + b"## Shopping\nShow the ingredients.\n\n## Meals\nChoose a meal.\n",
    ],
)
def test_brief_removal_and_order_changes_refresh_all_surviving_results(current):
    assert affected_sections(SPEC, current) == sections(current)


def test_one_wish_change_retains_unaffected_sections():
    current = SPEC.replace(b"Choose a meal.", b"Choose three meals.")
    assert [section.slug for section in affected_sections(SPEC, current)] == ["meals"]


def test_new_section_retains_existing_results():
    current = SPEC + b"## Calendar\nSchedule dinners.\n"
    assert [section.slug for section in affected_sections(SPEC, current)] == ["calendar"]


@pytest.mark.parametrize("spec", [SPEC, SPEC.replace(b"\n", b"\r\n")])
def test_progress_mirror_preserves_source_bytes(spec):
    results = {section.slug: SectionResult("done", "Implemented.") for section in sections(spec)}
    assert mirrored_spec(render_progress(spec, results)) == spec
