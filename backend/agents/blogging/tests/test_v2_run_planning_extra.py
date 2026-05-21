"""Direct tests for ``blog_writing_process_v2.run_planning`` and its helpers."""

from __future__ import annotations

from pathlib import Path

import pytest


def _make_planning_result(iterations: int = 1, critic_report: dict | None = None):
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        PlanningPhaseResult,
        RequirementsAnalysis,
        TitleCandidate,
    )

    plan = ContentPlan(
        overarching_topic="Topic",
        narrative_flow="flow",
        sections=[
            ContentPlanSection(title="Intro", coverage_description="hook", order=0),
            ContentPlanSection(title="Body", coverage_description="meat", order=1),
            ContentPlanSection(title="Conclusion", coverage_description="wrap", order=2),
        ],
        title_candidates=[TitleCandidate(title="My Title", probability_of_success=0.7)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    return PlanningPhaseResult(
        content_plan=plan,
        planning_iterations_used=iterations,
        parse_retry_count=0,
        planning_wall_ms_total=10.0,
        plan_critic_report=critic_report,
    )


def test_run_planning_writes_artifacts_and_returns_result(monkeypatch, tmp_path: Path) -> None:
    import agent_implementations.blog_writing_process_v2 as v2
    from blog_research_agent.models import ResearchBriefInput
    from shared.content_profile import ContentProfile, resolve_length_policy

    ppr = _make_planning_result(iterations=2, critic_report={"status": "PASS"})

    class _FakeAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, *_a, **_kw):
            return ppr

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakeAgent)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "brand")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "style")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: object())

    work_dir = tmp_path / "wd"
    work_dir.mkdir(parents=True, exist_ok=True)

    result = v2.run_planning(
        ResearchBriefInput(brief="b", max_results=5),
        work_dir=work_dir,
        llm_client=object(),
        length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
        series_context=None,
        job_updater=None,
    )

    assert result is ppr
    assert (work_dir / "content_plan.json").exists()
    assert (work_dir / "outline.md").exists()
    assert (work_dir / "plan_critic_report.json").exists()


def test_run_planning_without_work_dir_or_critic_report(monkeypatch) -> None:
    import agent_implementations.blog_writing_process_v2 as v2
    from blog_research_agent.models import ResearchBriefInput
    from shared.content_profile import ContentProfile, resolve_length_policy

    ppr = _make_planning_result(critic_report=None)

    class _FakeAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, *_a, **_kw):
            return ppr

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakeAgent)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "brand")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "style")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: None)

    result = v2.run_planning(
        ResearchBriefInput(brief="b", max_results=5),
        work_dir=None,
        llm_client=object(),
        length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
        series_context=None,
        job_updater=None,
    )
    assert result is ppr


def test_run_planning_job_updater_swallows_errors(monkeypatch, tmp_path: Path) -> None:
    import agent_implementations.blog_writing_process_v2 as v2
    from blog_research_agent.models import ResearchBriefInput
    from shared.content_profile import ContentProfile, resolve_length_policy

    ppr = _make_planning_result(critic_report=None)

    class _FakeAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, *_a, **_kw):
            return ppr

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakeAgent)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: None)

    def boom(**kw):
        raise RuntimeError("updater down")

    result = v2.run_planning(
        ResearchBriefInput(brief="b", max_results=5),
        work_dir=tmp_path / "wd2",
        llm_client=object(),
        length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
        series_context=None,
        job_updater=boom,
    )
    assert result is ppr


def test_run_planning_propagates_blogging_error(monkeypatch) -> None:
    import agent_implementations.blog_writing_process_v2 as v2
    from blog_research_agent.models import ResearchBriefInput
    from shared.content_profile import ContentProfile, resolve_length_policy
    from shared.errors import PlanningError

    class _FakeAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, *_a, **_kw):
            raise PlanningError("planner died", failure_reason="X")

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakeAgent)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: None)

    with pytest.raises(PlanningError):
        v2.run_planning(
            ResearchBriefInput(brief="b", max_results=5),
            work_dir=None,
            llm_client=object(),
            length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
            series_context=None,
            job_updater=None,
        )


def test_run_planning_wraps_unknown_exception(monkeypatch) -> None:
    import agent_implementations.blog_writing_process_v2 as v2
    from blog_research_agent.models import ResearchBriefInput
    from shared.content_profile import ContentProfile, resolve_length_policy
    from shared.errors import PlanningError

    class _FakeAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, *_a, **_kw):
            raise RuntimeError("transport failed")

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakeAgent)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: None)

    with pytest.raises(PlanningError) as exc:
        v2.run_planning(
            ResearchBriefInput(brief="b", max_results=5),
            work_dir=None,
            llm_client=object(),
            length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
            series_context=None,
            job_updater=None,
        )
    assert "Planning failed" in str(exc.value)


def test_extract_plan_keywords_returns_filtered_unique() -> None:
    import agent_implementations.blog_writing_process_v2 as v2
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    plan = ContentPlan(
        overarching_topic="observability essentials",
        narrative_flow="x",
        sections=[
            ContentPlanSection(title="Tracing and Metrics", coverage_description="x", order=0),
            ContentPlanSection(title="Cost Attribution", coverage_description="x", order=1),
        ],
        title_candidates=[TitleCandidate(title="t", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    kws = v2._extract_plan_keywords(plan)
    assert "and" not in kws
    assert "observability" in kws
    assert "tracing" in kws
    assert "metrics" in kws


def test_planning_llm_client_overrides_when_model_set(monkeypatch) -> None:
    import agent_implementations.blog_writing_process_v2 as v2

    class _FakeOllama:
        def __init__(self, model="m", base_url="http://x", timeout=10):
            self.model = model
            self.base_url = base_url
            self.timeout = timeout

    monkeypatch.setattr(v2, "OllamaLLMClient", _FakeOllama)
    monkeypatch.setattr(v2, "planning_model_override", lambda: "override-model")
    base = _FakeOllama(model="orig")
    out = v2.planning_llm_client(base)
    assert isinstance(out, _FakeOllama)
    assert out.model == "override-model"


def test_planning_llm_client_no_override_returns_base(monkeypatch) -> None:
    import agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(v2, "planning_model_override", lambda: "")
    sentinel = object()
    assert v2.planning_llm_client(sentinel) is sentinel


def test_plan_critic_llm_client_overrides_when_model_set(monkeypatch) -> None:
    import agent_implementations.blog_writing_process_v2 as v2

    class _FakeOllama:
        def __init__(self, model="m", base_url="http://x", timeout=10):
            self.model = model
            self.base_url = base_url
            self.timeout = timeout

    monkeypatch.setattr(v2, "OllamaLLMClient", _FakeOllama)
    monkeypatch.setattr(v2, "plan_critic_model_override", lambda: "critic-model")
    base = _FakeOllama(model="orig")
    out = v2.plan_critic_llm_client(base)
    assert isinstance(out, _FakeOllama)
    assert out.model == "critic-model"


def test_plan_critic_llm_client_no_override_returns_base(monkeypatch) -> None:
    import agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(v2, "plan_critic_model_override", lambda: "")
    sentinel = object()
    assert v2.plan_critic_llm_client(sentinel) is sentinel


def test_build_plan_critic_agent_disabled_returns_none(monkeypatch) -> None:
    import agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(v2, "plan_critic_enabled", lambda: False)
    assert v2.build_plan_critic_agent(object()) is None


def test_build_plan_critic_agent_enabled_returns_instance(monkeypatch) -> None:
    import agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(v2, "plan_critic_enabled", lambda: True)
    monkeypatch.setattr(v2, "plan_critic_llm_client", lambda b: b)
    agent = v2.build_plan_critic_agent(object())
    assert agent is not None
