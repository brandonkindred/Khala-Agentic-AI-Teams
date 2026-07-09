"""Guard tests for the blogging team's Pattern-A Temporal exports and the
run_pipeline -> stage-function decomposition.

Keeps the ``WORKFLOWS``/``ACTIVITIES`` exports, the worker registration, and the
four ``@activity.defn`` names in sync — the seam that silently breaks a workflow
(an unregistered activity hangs forever) if an activity is added without wiring.
"""

from __future__ import annotations


def test_pattern_a_exports_workflows_and_activities() -> None:
    """Every ``@activity.defn`` in the package is exported via ACTIVITIES."""
    from temporalio import activity

    from blogging import temporal as t

    assert t.WORKFLOWS == [t.BlogFullPipelineWorkflow]
    assert len(t.ACTIVITIES) == 4

    names = {activity._Definition.must_from_callable(a).name for a in t.ACTIVITIES}
    assert names == {"blog_plan_stage", "blog_draft_stage", "blog_gates_stage", "blog_finalize"}


def test_activities_match_constants() -> None:
    """The exported activity names line up with the name constants."""
    from temporalio import activity

    from blogging import temporal as t
    from blogging.temporal import constants

    names = {activity._Definition.must_from_callable(a).name for a in t.ACTIVITIES}
    assert names == {
        constants.ACTIVITY_PLAN_STAGE,
        constants.ACTIVITY_DRAFT_STAGE,
        constants.ACTIVITY_GATES_STAGE,
        constants.ACTIVITY_FINALIZE,
    }


def test_worker_registers_exported_lists(monkeypatch) -> None:
    """create_blogging_worker registers exactly the exported WORKFLOWS/ACTIVITIES."""
    from blogging import temporal as t
    from blogging.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)
    monkeypatch.setattr(worker, "_activity_executor", None)

    captured: dict = {}

    class _FakeWorker:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(worker, "Worker", _FakeWorker)

    from unittest.mock import MagicMock

    worker.create_blogging_worker(client=MagicMock())
    assert list(captured["workflows"]) == list(t.WORKFLOWS)
    assert list(captured["activities"]) == list(t.ACTIVITIES)
    assert captured["max_concurrent_activities"] == 4


def test_run_pipeline_invokes_three_stages_in_order(monkeypatch, tmp_path) -> None:
    """run_pipeline is a thin sequencer over the three extracted stage functions."""
    import importlib

    v2 = importlib.import_module("blogging.agent_implementations.blog_writing_process_v2")
    calls: list[str] = []

    def _planning(ctx):
        calls.append("planning")
        ctx.planning_phase_result = "ppr"
        ctx.plan = "plan"
        ctx.elicited_stories_text = None
        return None

    def _draft(ctx):
        calls.append("draft")
        ctx.draft_result = "draft"
        return None

    def _gates(ctx):
        calls.append("gates")
        ctx.status = "PASS"
        return None

    monkeypatch.setattr(v2, "run_planning_stage", _planning)
    monkeypatch.setattr(v2, "run_draft_stage", _draft)
    monkeypatch.setattr(v2, "run_gates_stage", _gates)

    from blog_research_agent.models import ResearchBriefInput

    ppr, draft, status = v2.run_pipeline(
        ResearchBriefInput(brief="hi", max_results=5),
        work_dir=tmp_path / "wd",
        llm_client=object(),
    )
    assert calls == ["planning", "draft", "gates"]
    assert (ppr, draft, status) == ("ppr", "draft", "PASS")


def test_run_pipeline_short_circuits_on_planning_abort(monkeypatch, tmp_path) -> None:
    """A planning abort tuple short-circuits before draft/gates run."""
    import importlib

    v2 = importlib.import_module("blogging.agent_implementations.blog_writing_process_v2")
    calls: list[str] = []

    monkeypatch.setattr(
        v2, "run_planning_stage", lambda ctx: calls.append("planning") or ("ppr", None, "FAIL")
    )
    monkeypatch.setattr(v2, "run_draft_stage", lambda ctx: calls.append("draft"))
    monkeypatch.setattr(v2, "run_gates_stage", lambda ctx: calls.append("gates"))

    from blog_research_agent.models import ResearchBriefInput

    ppr, draft, status = v2.run_pipeline(
        ResearchBriefInput(brief="hi", max_results=5),
        work_dir=tmp_path / "wd",
        llm_client=object(),
    )
    assert calls == ["planning"]
    assert (ppr, draft, status) == ("ppr", None, "FAIL")


# ---------------------------------------------------------------------------
# activity helpers — _build_pipeline_context / _fail_activity
# ---------------------------------------------------------------------------


def test_build_pipeline_context_seeds_inputs(monkeypatch, tmp_path) -> None:
    """_build_pipeline_context resolves the LLM/length/updater and honors request flags."""
    monkeypatch.setenv("BLOGGING_RUN_ARTIFACTS_ROOT", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "dummy")

    from blogging.temporal import activities as acts

    ctx = acts._build_pipeline_context(
        "job-xyz",
        {"brief": "hi", "max_results": 5, "run_gates": False, "max_rewrite_iterations": 7},
    )
    assert ctx.job_id == "job-xyz"
    assert ctx.run_gates is False
    assert ctx.max_rewrite_iterations == 7
    assert callable(ctx.job_updater)
    assert ctx.work_dir.exists()


def test_fail_activity_external_cancellation_marks_cancelled(monkeypatch) -> None:
    """External cancellation -> job marked cancelled, returns True (caller swallows)."""
    import importlib

    from blogging.temporal import activities as acts

    rpj = importlib.import_module("blogging.shared.run_pipeline_job")
    marked: dict = {}
    monkeypatch.setattr(rpj, "_is_external_cancellation", lambda e: True)
    monkeypatch.setattr(rpj, "mark_job_cancelled", lambda jid: marked.setdefault("job", jid))

    assert acts._fail_activity("j1", ValueError("x"), "planning") is True
    assert marked["job"] == "j1"


def test_fail_activity_hard_error_fails_job(monkeypatch) -> None:
    """A hard error fails the job (with phase) and returns False so the caller re-raises."""
    import importlib

    from blogging.temporal import activities as acts

    rpj = importlib.import_module("blogging.shared.run_pipeline_job")
    failed: dict = {}
    monkeypatch.setattr(rpj, "_is_external_cancellation", lambda e: False)
    monkeypatch.setattr(
        rpj, "_fail_job", lambda jid, msg, **kw: failed.update(jid=jid, msg=msg, kw=kw)
    )
    monkeypatch.setattr(rpj, "_publish_terminal", lambda *a, **kw: None)

    assert acts._fail_activity("j1", ValueError("boom"), "gates") is False
    assert failed["jid"] == "j1"
    assert failed["kw"]["failed_phase"] == "gates"


def test_plan_stage_activity_reraises_cancelled(monkeypatch, tmp_path) -> None:
    """A CancelledError from the stage propagates (Temporal owns cancellation)."""
    import importlib

    import pytest
    from temporalio.exceptions import CancelledError

    from blogging.temporal import activities as acts

    v2 = importlib.import_module("blogging.agent_implementations.blog_writing_process_v2")
    bjs = importlib.import_module("blogging.shared.blog_job_store")
    rpj = importlib.import_module("blogging.shared.run_pipeline_job")

    from types import SimpleNamespace

    ctx = SimpleNamespace(job_updater=lambda **kw: None, work_dir=tmp_path)
    monkeypatch.setattr(acts, "_build_pipeline_context", lambda job_id, req: ctx)
    monkeypatch.setattr(bjs, "start_blog_job", lambda job_id: None)
    monkeypatch.setattr(rpj, "start_pipeline_heartbeat", lambda job_id: None)

    def boom(c):
        raise CancelledError("cancel")

    monkeypatch.setattr(v2, "run_planning_stage", boom)
    with pytest.raises(CancelledError):
        acts.plan_stage_activity("j1", {"brief": "x"})


# ---------------------------------------------------------------------------
# workflow orchestration — patch workflow.execute_activity (no Temporal env)
# ---------------------------------------------------------------------------


def _run_workflow(monkeypatch, statuses):
    """Drive BlogFullPipelineWorkflow.run with a stubbed execute_activity.

    ``statuses`` maps activity function name -> DTO returned by that activity.
    Returns the ordered list of activity names the workflow scheduled.
    """
    import asyncio

    from blogging.temporal import workflows as wf

    calls: list[str] = []

    async def fake_execute(activity, args=None, **kwargs):
        name = getattr(activity, "__name__", str(activity))
        calls.append(name)
        return statuses.get(name, {})

    monkeypatch.setattr(wf.workflow, "execute_activity", fake_execute)
    asyncio.run(wf.BlogFullPipelineWorkflow().run("j1", {"brief": "x"}))
    return calls


def test_workflow_runs_all_four_activities(monkeypatch) -> None:
    """Happy path: planning -> draft -> gates -> finalize."""
    calls = _run_workflow(
        monkeypatch,
        {
            "plan_stage_activity": {"status": "PASS"},
            "draft_stage_activity": {"status": "PASS"},
            "gates_stage_activity": {"status": "PASS"},
            "finalize_job_activity": None,
        },
    )
    assert calls == [
        "plan_stage_activity",
        "draft_stage_activity",
        "gates_stage_activity",
        "finalize_job_activity",
    ]


def test_workflow_short_circuits_when_planning_not_pass(monkeypatch) -> None:
    """A non-PASS planning status stops the workflow before draft/gates/finalize."""
    calls = _run_workflow(monkeypatch, {"plan_stage_activity": {"status": "FAIL"}})
    assert calls == ["plan_stage_activity"]


def test_workflow_short_circuits_when_draft_not_pass(monkeypatch) -> None:
    """A non-PASS draft status stops the workflow before gates/finalize."""
    calls = _run_workflow(
        monkeypatch,
        {
            "plan_stage_activity": {"status": "PASS"},
            "draft_stage_activity": {"status": "FAIL"},
        },
    )
    assert calls == ["plan_stage_activity", "draft_stage_activity"]


# ---------------------------------------------------------------------------
# DTO serialization contract — round-trip real pydantic models across the boundary
# ---------------------------------------------------------------------------


def _real_planning_phase_result():
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
    return PlanningPhaseResult(
        content_plan=plan,
        planning_iterations_used=1,
        parse_retry_count=0,
        planning_wall_ms_total=10.0,
    )


def test_planning_dto_round_trips_real_model() -> None:
    """A real PlanningPhaseResult survives model_dump(mode='json') -> DTO -> model_validate."""
    from shared.content_plan import PlanningPhaseResult

    from blogging.temporal.phase_models import PlanningStageResult

    ppr = _real_planning_phase_result()
    dto = PlanningStageResult(
        planning_phase_result=ppr.model_dump(mode="json"),
        elicited_stories_text="a story",
        status="PASS",
    ).model_dump()

    # Cross the (JSON) boundary and rebuild, exactly as the draft/gates activities do.
    rehydrated = PlanningStageResult.model_validate(dto)
    ppr2 = PlanningPhaseResult.model_validate(rehydrated.planning_phase_result)
    assert ppr2.content_plan.title_candidates[0].title == "My Title"
    assert ppr2.planning_iterations_used == 1
    assert rehydrated.elicited_stories_text == "a story"


def test_draft_and_gates_dto_round_trip_real_model() -> None:
    """A real WriterOutput survives the DraftStageResult/GatesStageResult boundary."""
    from blog_writer_agent.models import WriterOutput

    from blogging.temporal.phase_models import DraftStageResult, GatesStageResult

    draft = WriterOutput(draft="# Title\nBody paragraph.")
    draft_dto = DraftStageResult(draft=draft.model_dump(mode="json"), status="PASS").model_dump()
    rebuilt = WriterOutput.model_validate(DraftStageResult.model_validate(draft_dto).draft)
    assert rebuilt.draft == "# Title\nBody paragraph."

    gates_dto = GatesStageResult(
        draft=draft.model_dump(mode="json"), status="NEEDS_HUMAN_REVIEW"
    ).model_dump()
    gr = GatesStageResult.model_validate(gates_dto)
    assert gr.status == "NEEDS_HUMAN_REVIEW"
    assert WriterOutput.model_validate(gr.draft).draft == "# Title\nBody paragraph."
