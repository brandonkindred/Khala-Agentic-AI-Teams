"""
Shared test fixtures and helpers for blogging agent tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def setup_artifacts_root(monkeypatch, tmp_path: Path) -> None:
    """Point ``BLOGGING_RUN_ARTIFACTS_ROOT`` at a test-owned temp directory."""
    monkeypatch.setenv("BLOGGING_RUN_ARTIFACTS_ROOT", str(tmp_path))


def make_pipeline_doubles():
    """Build a ``(PlanningPhaseResult, draft, status)`` triple for a passing pipeline run."""
    from agents.blogging.shared.content_plan import (
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


# Job-store helpers captured by reference inside ``api/main`` at import time. The
# ``patched_client`` fixture rebinds each to the fake-backed implementation so
# every endpoint hits the in-memory store.
_BLOG_JOB_HELPERS = (
    "create_blog_job",
    "delete_blog_job",
    "get_blog_job",
    "list_blog_jobs",
    "update_blog_job",
    "start_blog_job",
    "complete_blog_job",
    "fail_blog_job",
    "approve_blog_job",
    "unapprove_blog_job",
    "submit_title_selection",
    "submit_title_ratings",
    "submit_story_user_message",
    "skip_current_story_gap",
    "submit_blog_answers",
    "submit_draft_feedback",
    "is_waiting_for_draft_feedback",
)


@pytest.fixture
def patched_client(monkeypatch, fake_job_client) -> Any:
    """Back the blogging API with the in-memory fake job store.

    Replaces the ``blog_job_store`` module client and rebinds the job-store helper
    references captured inside ``api/main`` at import time, so every endpoint hits
    the fake. Imports the shared app module lazily so test modules that never use
    this fixture do not pay the ``api/main`` import cost.
    """
    from _api_test_utils import api_main
    from agents.blogging.shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "_client", lambda *a, **kw: fake_job_client)
    for name in _BLOG_JOB_HELPERS:
        helper = getattr(bjs, name, None)
        if helper is not None:
            monkeypatch.setattr(api_main, name, helper)
    return fake_job_client


@pytest.fixture
def client(patched_client) -> Any:
    """A ``TestClient`` for the blogging app, backed by the fake job store."""
    from _api_test_utils import app
    from fastapi.testclient import TestClient

    return TestClient(app)
