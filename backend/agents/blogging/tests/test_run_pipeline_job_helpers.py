"""Dedicated unit tests for the public helpers extracted into
``shared.run_pipeline_job`` during the per-phase Temporal decomposition:
``build_brief_input``, ``make_job_updater``, ``start_pipeline_heartbeat``,
``mark_job_cancelled``, and ``finalize_blog_job``.

These exercise each helper directly (rather than only through a full pipeline run)
so their contracts are covered in isolation.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import patch_job_event_bus_publish

# ---------------------------------------------------------------------------
# build_brief_input
# ---------------------------------------------------------------------------


def test_build_brief_input_populates_fields_and_defaults() -> None:
    """A minimal request yields a ResearchBriefInput with the default max_results."""
    from shared.run_pipeline_job import build_brief_input

    brief = build_brief_input({"brief": "  Write about testing  ", "audience": "devs"})
    assert brief.brief == "Write about testing"
    assert brief.audience == "devs"
    assert brief.max_results == 20  # documented default


def test_build_brief_input_appends_title_concept_and_normalizes_audience() -> None:
    """title_concept is folded into the brief text and a dict audience is normalized."""
    from shared.run_pipeline_job import build_brief_input

    brief = build_brief_input(
        {
            "brief": "Topic",
            "title_concept": "  Catchy Title  ",
            "audience": {"profession": "engineers"},
            "max_results": 5,
        }
    )
    assert "Topic" in brief.brief
    assert "Title concept: Catchy Title" in brief.brief
    assert brief.max_results == 5
    assert isinstance(brief.audience, str) and "engineers" in brief.audience


# ---------------------------------------------------------------------------
# start_blog_job idempotency (retry-safety)
# ---------------------------------------------------------------------------


def test_start_blog_job_is_idempotent_across_retries(patched_blog_job_store_client) -> None:
    """Calling start_blog_job twice (as a Temporal retry would) never raises and
    preserves the original started_at — it merges status=running onto the existing
    row rather than re-creating it or resetting the start time."""
    from shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    bjs.start_blog_job(job_id)
    first = bjs.get_blog_job(job_id)
    assert first["status"] == "running"
    started_at = first["started_at"]

    # A retry after a "successful start" must not raise and must keep started_at.
    bjs.start_blog_job(job_id)
    second = bjs.get_blog_job(job_id)
    assert second["status"] == "running"
    assert second["started_at"] == started_at


# ---------------------------------------------------------------------------
# make_job_updater
# ---------------------------------------------------------------------------


def test_make_job_updater_writes_store_and_publishes(
    monkeypatch, patched_blog_job_store_client
) -> None:
    """The updater writes kwargs to the store and broadcasts an ``update`` SSE event."""
    from shared import blog_job_store as bjs
    from shared import run_pipeline_job as rpj

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    published: list[tuple[str, dict]] = []

    def fake_publish(jid, payload, event_type="update"):
        published.append((event_type, dict(payload)))

    patch_job_event_bus_publish(monkeypatch, fake_publish)

    updater = rpj.make_job_updater(job_id)
    updater(status_text="working", progress=0.5)

    assert published and published[0][0] == "update"
    assert published[0][1]["status_text"] == "working"
    # The store write also landed.
    assert bjs.get_blog_job(job_id)["status_text"] == "working"


def test_make_job_updater_reraises_cancelled(monkeypatch) -> None:
    """A CancelledError from the store write propagates (cancellation must not be swallowed)."""
    from shared import run_pipeline_job as rpj
    from temporalio.exceptions import CancelledError

    def angry_update(job_id, **kwargs):
        raise CancelledError()

    monkeypatch.setattr(rpj, "_resolve_update_blog_job", lambda: angry_update)

    updater = rpj.make_job_updater("job-x")
    with pytest.raises(CancelledError):
        updater(status_text="x")


def test_make_job_updater_swallows_store_error(monkeypatch) -> None:
    """A non-cancel store failure is swallowed so a status update never breaks the run."""
    from shared import run_pipeline_job as rpj

    def angry_update(job_id, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(rpj, "_resolve_update_blog_job", lambda: angry_update)
    monkeypatch.setattr(rpj, "_import_shared", lambda name: (_ for _ in ()).throw(ImportError()))

    updater = rpj.make_job_updater("job-x")
    updater(status_text="x")  # must not raise


# ---------------------------------------------------------------------------
# start_pipeline_heartbeat
# ---------------------------------------------------------------------------


def test_start_pipeline_heartbeat_returns_none_without_store(monkeypatch) -> None:
    """With no job store available, the heartbeat is a no-op returning None."""
    from shared import run_pipeline_job as rpj

    monkeypatch.setattr(rpj, "_resolve_update_blog_job", lambda: None)
    assert rpj.start_pipeline_heartbeat("job-x") is None


def test_start_pipeline_heartbeat_starts_and_stops(monkeypatch) -> None:
    """With a store available, a started BackgroundHeartbeat is returned and can be stopped."""
    from shared import run_pipeline_job as rpj

    monkeypatch.setattr(rpj, "_resolve_update_blog_job", lambda: lambda *a, **k: None)
    hb = rpj.start_pipeline_heartbeat("job-abcdefghijkl")
    assert hb is not None
    hb.stop()  # idempotent teardown; must not raise


# ---------------------------------------------------------------------------
# mark_job_cancelled
# ---------------------------------------------------------------------------


def test_mark_job_cancelled_sets_status_and_returns_true(
    monkeypatch, patched_blog_job_store_client
) -> None:
    """The job is marked cancelled, a terminal event is published, and True is returned."""
    from shared import blog_job_store as bjs
    from shared import run_pipeline_job as rpj

    events: list[str] = []
    monkeypatch.setattr(rpj, "_publish_terminal", lambda jid, ev, **kw: events.append(ev))

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    assert rpj.mark_job_cancelled(job_id) is True
    assert bjs.get_blog_job(job_id)["status"] == "cancelled"
    assert events == ["cancelled"]


# ---------------------------------------------------------------------------
# finalize_blog_job
# ---------------------------------------------------------------------------


def _pipeline_doubles():
    from _content_plan_test_utils import make_pipeline_doubles

    ppr, draft, _ = make_pipeline_doubles()
    return ppr, draft


def test_finalize_blog_job_pass_completes(monkeypatch, patched_blog_job_store_client) -> None:
    """status PASS completes the job (COMPLETED) and returns the completed status."""
    from shared import blog_job_store as bjs
    from shared import run_pipeline_job as rpj

    monkeypatch.setattr(rpj, "_publish_terminal", lambda *a, **kw: None)
    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    ppr, draft = _pipeline_doubles()
    result = rpj.finalize_blog_job(job_id, ppr, draft, "PASS")

    assert result == bjs.JOB_STATUS_COMPLETED
    assert bjs.get_blog_job(job_id)["status"] == bjs.JOB_STATUS_COMPLETED


def test_finalize_blog_job_non_pass_needs_review(
    monkeypatch, patched_blog_job_store_client
) -> None:
    """A non-PASS status finalizes as NEEDS_REVIEW rather than COMPLETED."""
    from shared import blog_job_store as bjs
    from shared import run_pipeline_job as rpj

    monkeypatch.setattr(rpj, "_publish_terminal", lambda *a, **kw: None)
    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    ppr, draft = _pipeline_doubles()
    result = rpj.finalize_blog_job(job_id, ppr, draft, "NEEDS_HUMAN_REVIEW")

    assert result == bjs.JOB_STATUS_NEEDS_REVIEW
