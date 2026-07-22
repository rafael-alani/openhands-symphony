from __future__ import annotations

from conftest import issue

from symphony.intake import branch_name
from symphony.models import JobState
from symphony.reports import ReportWriter
from symphony.store import Store


def test_markdown_report_keeps_prior_failure_after_later_transition(tmp_path):
    store = Store(tmp_path / "state.db")
    snapshot = issue()
    job, _ = store.ensure_job(
        snapshot,
        "codex",
        None,
        False,
        branch_name(snapshot.number, snapshot.title),
        snapshot.repository,
    )
    store.transition(
        job.id,
        JobState.FAILED,
        phase="provider-tool-failure",
        terminal_reason="exact provider launch error",
    )
    store.request_control(snapshot.repository, snapshot.number, "retry")
    current = store.get_job_by_id(job.id)
    assert current

    markdown_path, _ = ReportWriter(tmp_path / "reports", store).write(current)
    markdown = markdown_path.read_text()

    assert "## Event history" in markdown
    assert "exact provider launch error" in markdown
