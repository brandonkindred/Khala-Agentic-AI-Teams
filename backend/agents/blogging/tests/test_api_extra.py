"""More API tests for blogging /full-pipeline sync, resume/restart happy paths,
medium stats runner, and the run_pipeline_with_tracking error branches.

Reuses the same module-import pattern as ``test_api_unit.py`` so the FastAPI
app uses ``FakeJobServiceClient`` and the heavy ``run_pipeline`` is patched.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

_blogging_root = Path(__file__).resolve().parent.parent
if str(_blogging_root) not in sys.path:
    sys.path.insert(0, str(_blogging_root))

_spec = importlib.util.spec_from_file_location(
    "blogging_api_main_unit",  # reuse same name as test_api_unit so the module is shared
    _blogging_root / "api" / "main.py",
)
_api_main = sys.modules.get("blogging_api_main_unit")
if _api_main is None:
    _api_main = importlib.util.module_from_spec(_spec)
    sys.modules["blogging_api_main_unit"] = _api_main
    _spec.loader.exec_module(_api_main)
    for _cls_name in (
        "SelectTitleRequest",
        "TitleRatingItem",
        "RateTitlesRequest",
        "StoryResponseRequest",
        "BlogAnswersRequest",
        "DraftFeedbackRequest",
    ):
        _cls = getattr(_api_main, _cls_name, None)
        if _cls is not None:
            _cls.model_rebuild(_types_namespace={**_api_main.__dict__})
app = _api_main.app


@pytest.fixture
def patched_client(monkeypatch, fake_job_client) -> Any:
    from shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "_client", lambda *a, **kw: fake_job_client)
    for name in (
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
    ):
        helper = getattr(bjs, name, None)
        if helper is not None:
            monkeypatch.setattr(_api_main, name, helper)
    return fake_job_client


@pytest.fixture
def client(patched_client) -> TestClient:
    return TestClient(app)


def _create(brief: str = "brief", **fields: Any) -> str:
    from shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, brief)
    if fields:
        bjs.update_blog_job(job_id, **fields)
    return job_id


# ---------------------------------------------------------------------------
# /full-pipeline (sync)
# ---------------------------------------------------------------------------


def _make_pipeline_doubles():
    """Build minimal planning_phase_result + draft_result + status fake."""
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
        title_candidates=[TitleCandidate(title="My Title", probability_of_success=0.8)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    ppr = PlanningPhaseResult(
        content_plan=plan,
        planning_iterations_used=1,
        parse_retry_count=0,
        planning_wall_ms_total=10.0,
    )

    class _Draft:
        draft = "# Draft\n\nBody."

    return ppr, _Draft(), "PASS"


def test_full_pipeline_sync_success(client: TestClient, monkeypatch) -> None:
    """POST /full-pipeline returns success when run_pipeline returns PASS."""
    import agent_implementations.blog_writing_process_v2 as v2

    ppr, draft, status = _make_pipeline_doubles()
    monkeypatch.setattr(v2, "run_pipeline", lambda *a, **kw: (ppr, draft, status))

    body = {"brief": "How to ship faster", "title_concept": "engineering"}
    r = client.post("/full-pipeline", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "PASS"
    assert data["title_choices"][0]["title"] == "My Title"
    assert "# Intro" in data["outline"] or "Intro" in data["outline"]


def test_full_pipeline_sync_planning_error(client: TestClient, monkeypatch) -> None:
    import agent_implementations.blog_writing_process_v2 as v2
    from shared.errors import PlanningError

    def boom(*a, **kw):
        raise PlanningError("could not converge", failure_reason="MAX_ITERATIONS_REACHED")

    monkeypatch.setattr(v2, "run_pipeline", boom)

    r = client.post("/full-pipeline", json={"brief": "x"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "planning_failed"
    assert detail["failure_reason"] == "MAX_ITERATIONS_REACHED"


def test_full_pipeline_sync_unknown_error(client: TestClient, monkeypatch) -> None:
    import agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(
        v2, "run_pipeline", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash"))
    )

    r = client.post("/full-pipeline", json={"brief": "x"})
    assert r.status_code == 500
    assert "crash" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Resume / restart with valid payload — happy path
# ---------------------------------------------------------------------------


def test_resume_job_happy(client: TestClient, monkeypatch) -> None:
    """Resume an interrupted job that has a stored request_payload."""
    from shared import blog_job_store as bjs

    job_id = _create()
    bjs.update_blog_job(
        job_id,
        status="interrupted",
        request_payload={"brief": "x"},
    )

    # Threading replacement so we don't actually run the pipeline
    class _NoOpThread:
        def __init__(self, target=None, args=(), daemon=False, **kw):
            pass

        def start(self):
            pass

    monkeypatch.setattr(_api_main.threading, "Thread", _NoOpThread)

    r = client.post(f"/job/{job_id}/resume")
    assert r.status_code == 200
    assert r.json()["job_id"] == job_id
    # Status updated to running
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "running"


def test_restart_job_happy(client: TestClient, monkeypatch, fake_job_client) -> None:
    """Restart uses both `shared.blog_job_store` and `blogging.shared.blog_job_store`
    aliases — patch both so the in-memory fake client backs everything."""
    from shared import blog_job_store as bjs

    job_id = _create()
    bjs.update_blog_job(job_id, status="completed", request_payload={"brief": "x"})

    # Also patch the alternative module path used by api/main.py for reset_blog_job
    try:
        from blogging.shared import blog_job_store as bjs_alt

        monkeypatch.setattr(bjs_alt, "_client", lambda *a, **kw: fake_job_client)
    except ImportError:
        pass

    class _NoOpThread:
        def __init__(self, target=None, args=(), daemon=False, **kw):
            pass

        def start(self):
            pass

    monkeypatch.setattr(_api_main.threading, "Thread", _NoOpThread)

    r = client.post(f"/job/{job_id}/restart")
    assert r.status_code == 200
    job = bjs.get_blog_job(job_id)
    # After reset, status is pending
    assert job["status"] == "pending"


# ---------------------------------------------------------------------------
# Medium stats background runner
# ---------------------------------------------------------------------------


def test_medium_stats_async_runner_happy(client: TestClient, monkeypatch, tmp_path: Path) -> None:
    """The async background runner writes the artifact + marks job completed."""
    from shared import blog_job_store as bjs

    monkeypatch.setattr(_api_main, "medium_stats_integration_eligible", lambda: (True, ""))

    # Stub Medium agent collect()
    from blog_medium_stats_agent.models import MediumStatsReport

    sentinel = MediumStatsReport(posts=[])

    class _Stub:
        def collect(self, cfg):
            return sentinel

    monkeypatch.setattr(_api_main, "BlogMediumStatsAgent", lambda: _Stub())

    # Pre-create job with a work_dir
    job_id = _create()
    work_dir = tmp_path / "wd"
    work_dir.mkdir()
    bjs.update_blog_job(job_id, work_dir=str(work_dir))

    # Build a payload object
    from shared.medium_stats_api import MediumStatsRequest

    _api_main._run_medium_stats_async_job(job_id, MediumStatsRequest())
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "completed"
    assert (work_dir / "medium_stats_report.json").exists()


def test_medium_stats_async_runner_failure_path(
    client: TestClient, monkeypatch, tmp_path: Path
) -> None:
    from shared import blog_job_store as bjs

    monkeypatch.setattr(_api_main, "medium_stats_integration_eligible", lambda: (False, "no creds"))

    job_id = _create()
    work_dir = tmp_path / "wd"
    work_dir.mkdir()
    bjs.update_blog_job(job_id, work_dir=str(work_dir))

    from shared.medium_stats_api import MediumStatsRequest

    _api_main._run_medium_stats_async_job(job_id, MediumStatsRequest())
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "failed"
    assert "no creds" in job["error"]


def test_medium_stats_async_runner_missing_work_dir(client: TestClient, monkeypatch) -> None:
    """When the job has no work_dir, runner records failure."""
    from shared import blog_job_store as bjs

    monkeypatch.setattr(_api_main, "medium_stats_integration_eligible", lambda: (True, ""))

    job_id = _create()
    # No work_dir set — bjs creates it as None which is treated as missing
    from shared.medium_stats_api import MediumStatsRequest

    _api_main._run_medium_stats_async_job(job_id, MediumStatsRequest())
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "failed"


# ---------------------------------------------------------------------------
# Async pipeline runner — patched pipeline raises in different ways
# ---------------------------------------------------------------------------


def test_run_pipeline_with_tracking_completes(
    client: TestClient, monkeypatch, tmp_path: Path
) -> None:
    import agent_implementations.blog_writing_process_v2 as v2
    from shared import blog_job_store as bjs

    ppr, draft, status = _make_pipeline_doubles()
    monkeypatch.setattr(v2, "run_pipeline", lambda *a, **kw: (ppr, draft, status))

    monkeypatch.setattr(_api_main, "RUN_ARTIFACTS_BASE", tmp_path)
    job_id = _create()

    # Build a request
    req = _api_main.FullPipelineRequest(brief="hi")
    _api_main._run_pipeline_with_tracking(job_id, req)
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "completed"


def test_run_pipeline_with_tracking_planning_error(
    client: TestClient, monkeypatch, tmp_path: Path
) -> None:
    import agent_implementations.blog_writing_process_v2 as v2
    from shared import blog_job_store as bjs
    from shared.errors import PlanningError

    monkeypatch.setattr(_api_main, "RUN_ARTIFACTS_BASE", tmp_path)
    monkeypatch.setattr(
        v2,
        "run_pipeline",
        lambda *a, **kw: (_ for _ in ()).throw(PlanningError("nope", failure_reason="x")),
    )

    job_id = _create()
    req = _api_main.FullPipelineRequest(brief="hi")
    _api_main._run_pipeline_with_tracking(job_id, req)
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "failed"
    assert job["failed_phase"] == "planning"


def test_run_pipeline_with_tracking_unknown_error(
    client: TestClient, monkeypatch, tmp_path: Path
) -> None:
    import agent_implementations.blog_writing_process_v2 as v2
    from shared import blog_job_store as bjs

    monkeypatch.setattr(_api_main, "RUN_ARTIFACTS_BASE", tmp_path)
    monkeypatch.setattr(
        v2, "run_pipeline", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("kaboom"))
    )

    job_id = _create()
    req = _api_main.FullPipelineRequest(brief="hi")
    _api_main._run_pipeline_with_tracking(job_id, req)
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "failed"


# ---------------------------------------------------------------------------
# Medium stats sync 200
# ---------------------------------------------------------------------------


def test_medium_stats_sync_happy(client: TestClient, monkeypatch) -> None:
    from blog_medium_stats_agent.models import MediumStatsReport

    monkeypatch.setattr(_api_main, "medium_stats_integration_eligible", lambda: (True, ""))

    class _Stub:
        def collect(self, cfg):
            return MediumStatsReport(posts=[])

    monkeypatch.setattr(_api_main, "BlogMediumStatsAgent", lambda: _Stub())
    r = client.post("/medium-stats", json={})
    assert r.status_code == 200


def test_medium_stats_sync_runtime_error(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "medium_stats_integration_eligible", lambda: (True, ""))

    class _Stub:
        def collect(self, cfg):
            raise RuntimeError("transient")

    monkeypatch.setattr(_api_main, "BlogMediumStatsAgent", lambda: _Stub())
    r = client.post("/medium-stats", json={})
    assert r.status_code == 503


def test_medium_stats_sync_unknown_error(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "medium_stats_integration_eligible", lambda: (True, ""))

    class _Stub:
        def collect(self, cfg):
            raise ValueError("bad input")

    monkeypatch.setattr(_api_main, "BlogMediumStatsAgent", lambda: _Stub())
    r = client.post("/medium-stats", json={})
    assert r.status_code == 500


# ---------------------------------------------------------------------------
# Helper: _publish_terminal_event swallows exceptions
# ---------------------------------------------------------------------------


def test_publish_terminal_event_swallows() -> None:
    # Just call — should never raise
    _api_main._publish_terminal_event("nonexistent", "complete", status="ok")


def test_publish_terminal_event_swallows_publish_failure(monkeypatch) -> None:
    """Publishes an event but cleanup_job is missing → swallow."""
    import shared.job_event_bus as bus

    def boom(*a, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(bus, "publish", boom)
    _api_main._publish_terminal_event("anything", "error", error="x")


# ---------------------------------------------------------------------------
# _rebuild_api_models — explicit smoke test (no exception)
# ---------------------------------------------------------------------------


def test_rebuild_api_models_idempotent() -> None:
    _api_main._rebuild_api_models()
    _api_main._rebuild_api_models()  # idempotent
