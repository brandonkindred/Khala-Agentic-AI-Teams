"""Cancellation handling tests for shared.run_pipeline_job."""

from __future__ import annotations

from typing import Any


def test_run_blog_full_pipeline_job_marks_cancelled_on_temporal_cancel(monkeypatch) -> None:
    """Temporal cancellation should mark job cancelled, not failed."""
    from agent_implementations import blog_writing_process_v2  # noqa: I001

    from blogging.shared import blog_job_store as blogging_job_store
    from shared import blog_job_store
    from shared.run_pipeline_job import run_blog_full_pipeline_job

    class CancelledError(Exception):
        __module__ = "temporalio.exceptions"

    def _raise_cancel(*args: Any, **kwargs: Any) -> Any:
        raise CancelledError("Cancelled")

    updates: list[dict[str, Any]] = []
    fail_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(blog_writing_process_v2, "run_pipeline", _raise_cancel)
    for store in (blog_job_store, blogging_job_store):
        monkeypatch.setattr(store, "start_blog_job", lambda *a, **k: None)
        monkeypatch.setattr(
            store,
            "update_blog_job",
            lambda job_id, **kwargs: updates.append({"job_id": job_id, **kwargs}),
        )
        monkeypatch.setattr(
            store,
            "fail_blog_job",
            lambda job_id, **kwargs: fail_calls.append({"job_id": job_id, **kwargs}),
        )

    run_blog_full_pipeline_job(
        "job-cancel-test",
        {
            "brief": "Test cancellation behavior.",
            "run_gates": False,
            "max_results": 1,
        },
    )

    assert any(u.get("status") == "cancelled" for u in updates)
    assert any(u.get("status_text") == "Pipeline cancelled" for u in updates)
    assert fail_calls == []

