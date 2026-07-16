"""Guard tests for the blogging team's Pattern-A Temporal exports and the
run_pipeline -> stage-function decomposition.

Keeps the ``WORKFLOWS``/``ACTIVITIES`` exports, the worker registration, and the
four ``@activity.defn`` names in sync — the seam that silently breaks a workflow
(an unregistered activity hangs forever) if an activity is added without wiring.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def test_pattern_a_exports_workflows_and_activities() -> None:
    """Every ``@activity.defn`` in the package is exported via ACTIVITIES."""
    from temporalio import activity

    from blogging import temporal as t

    assert t.WORKFLOWS == [t.BlogFullPipelineWorkflow]
    assert len(t.ACTIVITIES) == 5

    names = {activity._Definition.must_from_callable(a).name for a in t.ACTIVITIES}
    assert names == {
        "blog_plan_stage",
        "blog_draft_stage",
        "blog_gates_stage",
        "blog_finalize",
        "run_blog_full_pipeline",
    }


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
        constants.ACTIVITY_FULL_PIPELINE,
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


def test_load_required_guidelines_raises_when_missing(monkeypatch) -> None:
    """Missing guideline files raise DraftError instead of silently emptying agents."""
    import importlib

    import pytest

    v2 = importlib.import_module("blogging.agent_implementations.blog_writing_process_v2")
    monkeypatch.setattr(v2, "load_style_file", lambda *a, **kw: "")
    with pytest.raises(v2.DraftError):
        v2._load_required_guidelines("run gate-driven rewrites")


def test_load_required_guidelines_returns_contents(monkeypatch) -> None:
    """Both guideline files present -> their contents are returned as a pair."""
    import importlib

    v2 = importlib.import_module("blogging.agent_implementations.blog_writing_process_v2")
    monkeypatch.setattr(v2, "load_style_file", lambda *a, **kw: "ok")
    assert v2._load_required_guidelines("start drafting") == ("ok", "ok")


def test_load_required_guidelines_phase_override(monkeypatch) -> None:
    """The phase kwarg overrides DraftError's hardcoded phase so a gates-stage
    guidelines failure is attributed to gates, not to the draft stage."""
    import importlib

    import pytest

    v2 = importlib.import_module("blogging.agent_implementations.blog_writing_process_v2")
    monkeypatch.setattr(v2, "load_style_file", lambda *a, **kw: "")
    with pytest.raises(v2.DraftError) as exc_info:
        v2._load_required_guidelines("run gate-driven rewrites", phase="gates")
    assert exc_info.value.phase == "gates"


def test_is_last_attempt_outside_activity_context() -> None:
    """No activity context (direct/thread use) -> treated as the last attempt."""
    from blogging.temporal import activities as acts

    assert acts._is_last_attempt() is True


def test_is_last_attempt_reads_scheduled_retry_policy(monkeypatch) -> None:
    """The check reads maximum_attempts off the scheduled policy (activity.info())."""
    from types import SimpleNamespace

    import temporalio.activity as ta

    from blogging.temporal import activities as acts

    def _info(attempt, max_attempts):
        return SimpleNamespace(
            attempt=attempt, retry_policy=SimpleNamespace(maximum_attempts=max_attempts)
        )

    monkeypatch.setattr(ta, "info", lambda: _info(1, 3))
    assert acts._is_last_attempt() is False
    monkeypatch.setattr(ta, "info", lambda: _info(3, 3))
    assert acts._is_last_attempt() is True


def test_is_last_attempt_unlimited_policy_never_last(monkeypatch) -> None:
    """maximum_attempts <= 0 (unlimited retries) -> never the last attempt; and a
    missing retry_policy is likewise treated as unlimited (defer to Temporal)."""
    from types import SimpleNamespace

    import temporalio.activity as ta

    from blogging.temporal import activities as acts

    monkeypatch.setattr(
        ta,
        "info",
        lambda: SimpleNamespace(attempt=9, retry_policy=SimpleNamespace(maximum_attempts=0)),
    )
    assert acts._is_last_attempt() is False
    monkeypatch.setattr(ta, "info", lambda: SimpleNamespace(attempt=9, retry_policy=None))
    assert acts._is_last_attempt() is False


def test_finalize_retry_policy_derived_from_default() -> None:
    """FINALIZE_RETRY_POLICY is the default policy with a capped attempt count, so a
    backoff retune of DEFAULT_RETRY_POLICY carries over automatically."""
    from blogging.temporal import workflows as wf

    assert wf.FINALIZE_RETRY_POLICY.maximum_attempts == 3
    assert wf.FINALIZE_RETRY_POLICY.initial_interval == wf.DEFAULT_RETRY_POLICY.initial_interval
    assert (
        wf.FINALIZE_RETRY_POLICY.backoff_coefficient == wf.DEFAULT_RETRY_POLICY.backoff_coefficient
    )


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
    """External cancellation -> job marked cancelled (error terminal, never re-raised)."""
    import importlib

    from blogging.temporal import activities as acts

    rpj = importlib.import_module("blogging.shared.run_pipeline_job")
    marked: dict = {}
    monkeypatch.setattr(rpj, "_is_external_cancellation", lambda e: True)
    monkeypatch.setattr(rpj, "mark_job_cancelled", lambda jid: marked.setdefault("job", jid))

    acts._fail_activity("j1", ValueError("x"), "planning")
    assert marked["job"] == "j1"


def test_fail_activity_hard_error_fails_job(monkeypatch) -> None:
    """A hard error fails the job with the coarse stage name when the exception has no phase."""
    import importlib

    from blogging.temporal import activities as acts

    rpj = importlib.import_module("blogging.shared.run_pipeline_job")
    failed: dict = {}
    monkeypatch.setattr(rpj, "_is_external_cancellation", lambda e: False)
    monkeypatch.setattr(
        rpj, "_fail_job", lambda jid, msg, **kw: failed.update(jid=jid, msg=msg, kw=kw)
    )
    monkeypatch.setattr(rpj, "_publish_terminal", lambda *a, **kw: None)

    acts._fail_activity("j1", ValueError("boom"), "gates")
    assert failed["jid"] == "j1"
    assert failed["kw"]["failed_phase"] == "gates"


def test_fail_activity_prefers_exception_phase(monkeypatch) -> None:
    """The exception's own phase attribute wins over the coarse stage name."""
    import importlib

    from blogging.temporal import activities as acts

    rpj = importlib.import_module("blogging.shared.run_pipeline_job")
    failed: dict = {}
    monkeypatch.setattr(rpj, "_is_external_cancellation", lambda e: False)
    monkeypatch.setattr(rpj, "_fail_job", lambda jid, msg, **kw: failed.update(kw))
    monkeypatch.setattr(rpj, "_publish_terminal", lambda *a, **kw: None)

    exc = ValueError("brand violation")
    exc.phase = "compliance"
    acts._fail_activity("j1", exc, "gates")
    assert failed["failed_phase"] == "compliance"


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


def test_run_stage_transient_error_reraises_when_not_last_attempt(monkeypatch) -> None:
    """A transient LLM error re-raises (deferred to Temporal retry) on a non-final attempt."""
    import pytest

    from blogging.shared import run_pipeline_job as rpj
    from blogging.temporal import activities as acts
    from llm_service import LLMTemporaryError

    monkeypatch.setattr(rpj, "start_pipeline_heartbeat", lambda job_id: None)
    monkeypatch.setattr(acts, "_is_last_attempt", lambda: False)
    failed: list = []
    monkeypatch.setattr(acts, "_fail_activity", lambda *a, **kw: failed.append(a))

    def body():
        raise LLMTemporaryError("provider 503")

    with pytest.raises(LLMTemporaryError):
        acts._run_stage("j1", "gates", lambda: {"status": "FAIL"}, body)
    # Not funneled to a terminal failure — Temporal will retry the stage.
    assert failed == []


def test_run_stage_transient_error_funnels_fail_dto_on_last_attempt(monkeypatch) -> None:
    """On the final Temporal attempt, a transient LLM error is funneled to a FAIL DTO."""
    import pytest  # noqa: F401

    from blogging.shared import run_pipeline_job as rpj
    from blogging.temporal import activities as acts
    from llm_service import LLMRateLimitError

    monkeypatch.setattr(rpj, "start_pipeline_heartbeat", lambda job_id: None)
    monkeypatch.setattr(acts, "_is_last_attempt", lambda: True)
    failed: list = []
    monkeypatch.setattr(
        acts,
        "_fail_activity",
        lambda job_id, exc, failed_phase: failed.append((job_id, exc, failed_phase)),
    )

    def body():
        raise LLMRateLimitError("429")

    result = acts._run_stage("j1", "gates", lambda: {"status": "FAIL"}, body)
    assert result == {"status": "FAIL"}
    assert len(failed) == 1
    job_id, exc, failed_phase = failed[0]
    assert (job_id, failed_phase) == ("j1", "gates")
    # The terminal transient failure is recorded with a clear provider-availability
    # message (not the raw "429"), while the original error is preserved as the cause.
    assert "temporarily unavailable" in str(exc)
    assert isinstance(exc.cause, LLMRateLimitError)


# ---------------------------------------------------------------------------
# workflow orchestration — patch workflow.execute_activity (no Temporal env)
# ---------------------------------------------------------------------------


def _run_workflow(monkeypatch, statuses, patched=True):
    """Drive BlogFullPipelineWorkflow.run with a stubbed execute_activity.

    ``statuses`` maps activity function name -> DTO returned by that activity;
    ``patched`` is what the stubbed ``workflow.patched`` reports (False replays the
    pre-decomposition history path). Returns the ordered activity names scheduled.
    """
    import asyncio

    from blogging.temporal import workflows as wf

    calls: list[str] = []

    async def fake_execute(activity, args=None, **kwargs):
        name = getattr(activity, "__name__", str(activity))
        calls.append(name)
        return statuses.get(name, {})

    monkeypatch.setattr(wf.workflow, "execute_activity", fake_execute)
    monkeypatch.setattr(wf.workflow, "patched", lambda _id: patched)
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


def test_workflow_skips_finalize_when_gates_fail(monkeypatch) -> None:
    """A gates FAIL (job cancelled/failed mid-stage, draft=None) must skip finalize."""
    calls = _run_workflow(
        monkeypatch,
        {
            "plan_stage_activity": {"status": "PASS"},
            "draft_stage_activity": {"status": "PASS"},
            "gates_stage_activity": {"status": "FAIL", "draft": None},
        },
    )
    assert calls == ["plan_stage_activity", "draft_stage_activity", "gates_stage_activity"]


def test_workflow_finalizes_on_needs_human_review(monkeypatch) -> None:
    """NEEDS_HUMAN_REVIEW is a quality outcome, not an abort — finalize still runs."""
    calls = _run_workflow(
        monkeypatch,
        {
            "plan_stage_activity": {"status": "PASS"},
            "draft_stage_activity": {"status": "PASS"},
            "gates_stage_activity": {"status": "NEEDS_HUMAN_REVIEW", "draft": {"draft": "d"}},
        },
    )
    assert calls[-1] == "finalize_job_activity"


def test_workflow_unpatched_replay_runs_legacy_monolith(monkeypatch) -> None:
    """Pre-decomposition histories replay the single-activity path deterministically."""
    calls = _run_workflow(monkeypatch, {"run_full_pipeline_activity": None}, patched=False)
    assert calls == ["run_full_pipeline_activity"]


# ---------------------------------------------------------------------------
# DTO serialization contract — round-trip real pydantic models across the boundary
# ---------------------------------------------------------------------------


def _real_planning_phase_result():
    from _content_plan_test_utils import make_requirements_analysis
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        PlanningPhaseResult,
        TitleCandidate,
    )

    plan = ContentPlan(
        overarching_topic="Topic",
        narrative_flow="Flow",
        sections=[ContentPlanSection(title="Intro", coverage_description="hook", order=0)],
        title_candidates=[TitleCandidate(title="My Title", probability_of_success=0.7)],
        requirements_analysis=make_requirements_analysis(),
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
