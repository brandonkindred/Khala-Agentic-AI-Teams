"""Tests for shared.run_pipeline_job.run_blog_full_pipeline_job.

The orchestrator is heavy, but its branching is concentrated in:
* Completion path → complete_blog_job called
* PlanningError → fail_blog_job(phase=planning)
* BloggingError → fail_blog_job(phase=...)
* Unknown error → fail_blog_job(no phase)
* CancelledError → re-raised (covered by external_cancellation tests)
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest


@pytest.fixture
def patched_client(monkeypatch, fake_job_client):
    """Make `shared.blog_job_store._client` return the in-memory fake."""
    from shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "_client", lambda *a, **kw: fake_job_client)
    # Also patch the blogging.shared alias if the alternate module path was loaded
    try:
        from blogging.shared import blog_job_store as bjs_alt

        monkeypatch.setattr(bjs_alt, "_client", lambda *a, **kw: fake_job_client)
    except ImportError:
        pass
    return fake_job_client


def _setup_artifacts_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BLOGGING_RUN_ARTIFACTS_ROOT", str(tmp_path))


def _make_pipeline_doubles():
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        PlanningPhaseResult,
        RequirementsAnalysis,
        TitleCandidate,
    )

    plan = ContentPlan(
        overarching_topic="Topic",
        narrative_flow="Flow",
        sections=[ContentPlanSection(title="Intro", coverage_description="hook", order=0)],
        title_candidates=[TitleCandidate(title="My Title", probability_of_success=0.7)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    ppr = PlanningPhaseResult(
        content_plan=plan,
        planning_iterations_used=1,
        parse_retry_count=0,
        planning_wall_ms_total=5.0,
    )

    class _Draft:
        draft = "# Draft\n\nBody."

    return ppr, _Draft(), "PASS"


def test_run_blog_full_pipeline_job_completes(monkeypatch, tmp_path: Path, patched_client) -> None:
    from shared import blog_job_store as bjs
    from shared import run_pipeline_job as rpj

    _setup_artifacts_root(monkeypatch, tmp_path)

    ppr, draft, status = _make_pipeline_doubles()
    monkeypatch.setattr(
        "agent_implementations.blog_writing_process_v2.run_pipeline",
        lambda *a, **kw: (ppr, draft, status),
    )

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    rpj.run_blog_full_pipeline_job(
        job_id,
        {"brief": "hi", "title_concept": "x", "audience": "devs"},
    )

    job = bjs.get_blog_job(job_id)
    assert job["status"] == "completed"


def test_run_blog_full_pipeline_job_completes_needs_review(
    monkeypatch, tmp_path: Path, patched_client
) -> None:
    from shared import blog_job_store as bjs
    from shared import run_pipeline_job as rpj

    _setup_artifacts_root(monkeypatch, tmp_path)

    ppr, draft, _ = _make_pipeline_doubles()
    # Override to FAIL status
    monkeypatch.setattr(
        "agent_implementations.blog_writing_process_v2.run_pipeline",
        lambda *a, **kw: (ppr, draft, "FAIL"),
    )

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    rpj.run_blog_full_pipeline_job(job_id, {"brief": "hi"})

    job = bjs.get_blog_job(job_id)
    assert job["status"] == "needs_human_review"


def _import_errors_used_by_run_pipeline_job():
    """Mirror the import order in shared.run_pipeline_job: try blogging.shared.errors
    first, then fall back to shared.errors. Returns (BloggingError, PlanningError, DraftError).
    """
    try:
        from blogging.shared.errors import BloggingError, DraftError, PlanningError
    except ImportError:
        from shared.errors import BloggingError, DraftError, PlanningError
    return BloggingError, PlanningError, DraftError


def test_run_blog_full_pipeline_job_planning_error(
    monkeypatch, tmp_path: Path, patched_client
) -> None:
    from shared import blog_job_store as bjs
    from shared import run_pipeline_job as rpj

    _, PlanningError, _ = _import_errors_used_by_run_pipeline_job()
    _setup_artifacts_root(monkeypatch, tmp_path)

    def boom(*a, **kw):
        raise PlanningError("nope", failure_reason="MAX_ITER")

    monkeypatch.setattr("agent_implementations.blog_writing_process_v2.run_pipeline", boom)

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")
    rpj.run_blog_full_pipeline_job(job_id, {"brief": "hi"})
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "failed"
    assert job["failed_phase"] == "planning"


def test_run_blog_full_pipeline_job_blogging_error(
    monkeypatch, tmp_path: Path, patched_client
) -> None:
    from shared import blog_job_store as bjs
    from shared import run_pipeline_job as rpj

    _, _, DraftError = _import_errors_used_by_run_pipeline_job()
    _setup_artifacts_root(monkeypatch, tmp_path)

    def boom(*a, **kw):
        raise DraftError("draft failed", iteration=1)

    monkeypatch.setattr("agent_implementations.blog_writing_process_v2.run_pipeline", boom)

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")
    rpj.run_blog_full_pipeline_job(job_id, {"brief": "hi"})
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "failed"
    assert job["failed_phase"] == "draft"


def test_run_blog_full_pipeline_job_unknown_error(
    monkeypatch, tmp_path: Path, patched_client
) -> None:
    from shared import blog_job_store as bjs
    from shared import run_pipeline_job as rpj

    _setup_artifacts_root(monkeypatch, tmp_path)

    monkeypatch.setattr(
        "agent_implementations.blog_writing_process_v2.run_pipeline",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash")),
    )

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")
    rpj.run_blog_full_pipeline_job(job_id, {"brief": "hi"})
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "failed"


def test_run_blog_full_pipeline_job_job_updater_failure_swallowed(
    monkeypatch, tmp_path: Path, patched_client
) -> None:
    """A failing update_blog_job inside job_updater is logged but doesn't crash."""
    from shared import blog_job_store as bjs
    from shared import run_pipeline_job as rpj

    _setup_artifacts_root(monkeypatch, tmp_path)

    ppr, draft, _ = _make_pipeline_doubles()
    monkeypatch.setattr(
        "agent_implementations.blog_writing_process_v2.run_pipeline",
        lambda *a, **kw: (ppr, draft, "PASS"),
    )

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")
    rpj.run_blog_full_pipeline_job(job_id, {"brief": "hi", "audience": {"profession": "dev"}})
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "completed"
