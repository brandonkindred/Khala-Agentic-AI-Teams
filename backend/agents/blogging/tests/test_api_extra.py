"""More API tests for blogging /full-pipeline sync, resume/restart happy paths,
medium stats runner, and the run_pipeline_with_tracking error branches.

Reuses the same module-import pattern as ``test_api_unit.py`` so the FastAPI
app uses ``FakeJobServiceClient`` and the heavy ``run_pipeline`` is patched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from ._api_test_utils import api_main as _api_main
from ._api_test_utils import create_job as _create_job
from .conftest import setup_artifacts_root

# ``api_main``/``app`` load and the ``patched_client``/``client`` fixtures live in
# ``_api_test_utils`` and ``conftest.py`` — shared across the API test modules.

# Title used by ``_make_pipeline_doubles``; referenced by the assertion in
# ``test_full_pipeline_sync_success`` so the coupling is explicit.
_EXPECTED_TITLE = "My Title"


def _raise(exc: Exception):
    """Return a function that raises ``exc`` when called (any args).

    Clearer than the ``(_ for _ in ()).throw(exc)`` generator idiom for stubbing
    a callable that should blow up.
    """

    def _fn(*args: Any, **kwargs: Any):
        raise exc

    return _fn


# ---------------------------------------------------------------------------
# /full-pipeline (sync)
# ---------------------------------------------------------------------------


def _make_pipeline_doubles():
    """Build minimal planning_phase_result + draft_result + status fake."""
    from ._content_plan_test_utils import make_pipeline_doubles

    return make_pipeline_doubles(
        title=_EXPECTED_TITLE, probability=0.8, planning_wall_ms_total=10.0
    )


def test_full_pipeline_sync_success(client: TestClient, monkeypatch) -> None:
    """POST /full-pipeline returns success when run_pipeline returns PASS."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2

    ppr, draft, status = _make_pipeline_doubles()
    monkeypatch.setattr(v2, "run_pipeline", lambda *a, **kw: (ppr, draft, status))

    body = {"brief": "How to ship faster", "title_concept": "engineering"}
    r = client.post("/full-pipeline", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "PASS"
    assert data["title_choices"][0]["title"] == _EXPECTED_TITLE
    assert "# Intro" in data["outline"] or "Intro" in data["outline"]


def test_full_pipeline_sync_planning_error(client: TestClient, monkeypatch) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.shared.errors import PlanningError

    def boom(*a, **kw):
        raise PlanningError("could not converge", failure_reason="MAX_ITERATIONS_REACHED")

    monkeypatch.setattr(v2, "run_pipeline", boom)

    r = client.post("/full-pipeline", json={"brief": "x"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "planning_failed"
    assert detail["failure_reason"] == "MAX_ITERATIONS_REACHED"


def test_full_pipeline_sync_unknown_error(client: TestClient, monkeypatch) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(v2, "run_pipeline", _raise(RuntimeError("crash")))

    r = client.post("/full-pipeline", json={"brief": "x"})
    assert r.status_code == 500
    assert "crash" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Resume / restart with valid payload — happy path
# ---------------------------------------------------------------------------


def test_resume_job_happy(client: TestClient, monkeypatch) -> None:
    """Resume an interrupted job that has a stored request_payload."""
    from agents.blogging.shared import blog_job_store as bjs

    job_id = _create_job()
    bjs.update_blog_job(
        job_id,
        status="interrupted",
        request_payload={"brief": "x"},
    )

    # Intercept the bounded async-job pool so we don't actually run the pipeline.
    monkeypatch.setattr(_api_main, "_submit_async_job", lambda fn, *a, **kw: None)

    r = client.post(f"/job/{job_id}/resume")
    assert r.status_code == 200
    assert r.json()["job_id"] == job_id
    # Status updated to running
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "running"


def test_restart_job_happy(client: TestClient, monkeypatch) -> None:
    """Restart uses `agents.blogging.shared.blog_job_store`, backed by the in-memory fake."""
    from agents.blogging.shared import blog_job_store as bjs

    job_id = _create_job()
    bjs.update_blog_job(job_id, status="completed", request_payload={"brief": "x"})

    # Intercept the bounded async-job pool so we don't actually run the pipeline.
    monkeypatch.setattr(_api_main, "_submit_async_job", lambda fn, *a, **kw: None)

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
    from agents.blogging.shared import blog_job_store as bjs

    monkeypatch.setattr(_api_main, "medium_stats_integration_eligible", lambda: (True, ""))

    # Stub Medium agent collect()
    from agents.blogging.blog_medium_stats_agent.models import MediumStatsReport

    sentinel = MediumStatsReport(posts=[])

    class _Stub:
        def collect(self, cfg):
            return sentinel

    monkeypatch.setattr(_api_main, "BlogMediumStatsAgent", lambda: _Stub())

    # Pre-create job with a work_dir
    job_id = _create_job()
    work_dir = tmp_path / "wd"
    work_dir.mkdir()
    bjs.update_blog_job(job_id, work_dir=str(work_dir))

    # Build a payload object
    from agents.blogging.shared.medium_stats_api import MediumStatsRequest

    _api_main._run_medium_stats_async_job(job_id, MediumStatsRequest())
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "completed"
    assert (work_dir / "medium_stats_report.json").exists()


def test_medium_stats_async_runner_failure_path(
    client: TestClient, monkeypatch, tmp_path: Path
) -> None:
    from agents.blogging.shared import blog_job_store as bjs

    monkeypatch.setattr(_api_main, "medium_stats_integration_eligible", lambda: (False, "no creds"))

    job_id = _create_job()
    work_dir = tmp_path / "wd"
    work_dir.mkdir()
    bjs.update_blog_job(job_id, work_dir=str(work_dir))

    from agents.blogging.shared.medium_stats_api import MediumStatsRequest

    _api_main._run_medium_stats_async_job(job_id, MediumStatsRequest())
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "failed"
    assert "no creds" in job["error"]


def test_medium_stats_async_runner_missing_work_dir(client: TestClient, monkeypatch) -> None:
    """When the job has no work_dir, runner records failure."""
    from agents.blogging.shared import blog_job_store as bjs

    monkeypatch.setattr(_api_main, "medium_stats_integration_eligible", lambda: (True, ""))

    job_id = _create_job()
    # No work_dir set — bjs creates it as None which is treated as missing
    from agents.blogging.shared.medium_stats_api import MediumStatsRequest

    _api_main._run_medium_stats_async_job(job_id, MediumStatsRequest())
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "failed"


# ---------------------------------------------------------------------------
# Async pipeline runner — patched pipeline raises in different ways
# ---------------------------------------------------------------------------


def test_run_pipeline_with_tracking_completes(
    client: TestClient, monkeypatch, tmp_path: Path
) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.shared import blog_job_store as bjs

    ppr, draft, status = _make_pipeline_doubles()
    monkeypatch.setattr(v2, "run_pipeline", lambda *a, **kw: (ppr, draft, status))

    setup_artifacts_root(monkeypatch, tmp_path)
    job_id = _create_job()

    # Build a request
    req = _api_main.FullPipelineRequest(brief="hi")
    _api_main._run_pipeline_with_tracking(job_id, req)
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "completed"


def test_run_pipeline_with_tracking_planning_error(
    client: TestClient, monkeypatch, tmp_path: Path
) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.shared import blog_job_store as bjs
    from agents.blogging.shared.errors import PlanningError

    setup_artifacts_root(monkeypatch, tmp_path)
    monkeypatch.setattr(v2, "run_pipeline", _raise(PlanningError("nope", failure_reason="x")))

    job_id = _create_job()
    req = _api_main.FullPipelineRequest(brief="hi")
    _api_main._run_pipeline_with_tracking(job_id, req)
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "failed"
    assert job["failed_phase"] == "planning"


def test_run_pipeline_with_tracking_unknown_error(
    client: TestClient, monkeypatch, tmp_path: Path
) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.shared import blog_job_store as bjs

    setup_artifacts_root(monkeypatch, tmp_path)
    monkeypatch.setattr(v2, "run_pipeline", _raise(RuntimeError("kaboom")))

    job_id = _create_job()
    req = _api_main.FullPipelineRequest(brief="hi")
    _api_main._run_pipeline_with_tracking(job_id, req)
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "failed"


def test_run_pipeline_with_tracking_skips_already_terminal_job(
    client: TestClient, monkeypatch
) -> None:
    """The already-terminal preflight check still short-circuits ahead of the delegated call."""
    from agents.blogging.shared import run_pipeline_job as rpj

    called = False

    def _fail_if_called(job_id, request_dict):
        nonlocal called
        called = True

    monkeypatch.setattr(rpj, "run_blog_full_pipeline_job", _fail_if_called)

    job_id = _create_job(status="failed")
    req = _api_main.FullPipelineRequest(brief="hi")
    _api_main._run_pipeline_with_tracking(job_id, req)

    assert called is False


def test_run_pipeline_with_tracking_catches_delegate_exception(
    client: TestClient, monkeypatch
) -> None:
    """An exception escaping the delegated call still fails the job instead of propagating."""
    from agents.blogging.shared import blog_job_store as bjs
    from agents.blogging.shared import run_pipeline_job as rpj

    monkeypatch.setattr(rpj, "run_blog_full_pipeline_job", _raise(RuntimeError("boom")))

    job_id = _create_job()
    req = _api_main.FullPipelineRequest(brief="hi")
    _api_main._run_pipeline_with_tracking(job_id, req)

    job = bjs.get_blog_job(job_id)
    assert job["status"] == "failed"
    assert "boom" in job["error"]


def test_run_pipeline_with_tracking_delegates_request_dict(
    client: TestClient, monkeypatch, tmp_path: Path
) -> None:
    """The async runner's core call is a delegation to run_blog_full_pipeline_job,
    with the request handed over as an equivalent ``model_dump(mode="json")`` dict."""
    from agents.blogging.shared import run_pipeline_job as rpj

    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        rpj,
        "run_blog_full_pipeline_job",
        lambda job_id, request_dict: captured.append((job_id, request_dict)),
    )

    job_id = _create_job()
    req = _api_main.FullPipelineRequest(
        brief="Write about caching",
        title_concept="Cache Me If You Can",
        audience="backend engineers",
        tone_or_purpose="informative",
        max_results=7,
        run_gates=False,
        max_rewrite_iterations=2,
        length_notes="keep it tight",
        target_word_count=800,
    )
    _api_main._run_pipeline_with_tracking(job_id, req)

    assert captured == [(job_id, req.model_dump(mode="json"))]
    delegated_job_id, delegated_dict = captured[0]
    assert delegated_job_id == job_id
    assert delegated_dict["brief"] == "Write about caching"
    assert delegated_dict["title_concept"] == "Cache Me If You Can"
    assert delegated_dict["audience"] == "backend engineers"
    assert delegated_dict["run_gates"] is False
    assert delegated_dict["max_rewrite_iterations"] == 2
    assert delegated_dict["target_word_count"] == 800


# ---------------------------------------------------------------------------
# Medium stats sync 200
# ---------------------------------------------------------------------------


def test_medium_stats_sync_happy(client: TestClient, monkeypatch) -> None:
    from agents.blogging.blog_medium_stats_agent.models import MediumStatsReport

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
    import agents.blogging.shared.job_event_bus as bus

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


def test_all_local_models_rebuilt() -> None:
    """Every BaseModel defined in api/models has resolved annotations after import.

    Guards against the auto-scan regressing to a hand-maintained list: if a new
    model were added to api/models and left unresolved, it would show up here.
    """
    import agents.blogging.api.models as _api_models
    from pydantic import BaseModel as _BM

    local_models = [
        obj
        for obj in vars(_api_models).values()
        if isinstance(obj, type) and issubclass(obj, _BM) and obj.__module__ == _api_models.__name__
    ]
    # Sanity: the module defines multiple request/response DTOs.
    assert len(local_models) > 1, "expected multiple locally-defined models"
    unresolved = [m.__name__ for m in local_models if not m.__pydantic_complete__]
    assert not unresolved, f"models with unresolved annotations: {unresolved}"


def test_previously_missing_dtos_are_usable() -> None:
    """Instantiate DTOs that were absent from the old hand-maintained rebuild list.

    ``__pydantic_complete__`` only proves ``model_rebuild`` ran; this constructs
    the models — including one with a nested forward reference to another local
    model — to prove their annotations actually resolve and validate at runtime.
    """
    select = _api_main.SelectTitleRequest(title="A title")
    assert select.title == "A title"

    rate = _api_main.RateTitlesRequest(
        ratings=[_api_main.TitleRatingItem(title="A title", rating="like")]
    )
    assert rate.ratings[0].rating == "like"
