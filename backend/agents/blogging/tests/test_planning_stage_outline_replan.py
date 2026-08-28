"""Tests for the outline re-planning feedback loop in ``run_planning_stage``.

Covers the fix for the loop swallowing ``PlanningError``/transient LLM-transport
errors during re-planning: those must propagate for Temporal retry, while truly
unexpected errors still degrade to "keep current plan".
"""

from __future__ import annotations

import json

import pytest

from ._content_plan_test_utils import make_content_plan, make_planning_phase_result


def _make_ctx(monkeypatch, *, plan_content_side_effect):
    """Build a PipelineContext wired to hit the outline re-planning loop once.

    ``plan_content_side_effect`` is raised by the re-planning ``plan_content`` call
    triggered by the first (non-approving) feedback round.
    """
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.agent_implementations.pipeline.context import PipelineContext
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate
    from agents.blogging.shared.content_profile import resolve_length_policy

    plan = make_content_plan(
        overarching_topic="Topic",
        narrative_flow="flow",
        sections=[
            ContentPlanSection(title="Intro", coverage_description="hook", order=0),
            ContentPlanSection(title="Body", coverage_description="meat", order=1),
        ],
        title_candidates=[TitleCandidate(title="My Title", probability_of_success=0.7)],
    )
    ppr = make_planning_phase_result(
        plan, planning_iterations_used=1, planning_wall_ms_total=10.0, plan_critic_report=None
    )

    monkeypatch.setattr(v2, "run_planning", lambda *_a, **_kw: ppr)

    class _FakeAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, *_a, **_kw):
            raise plan_content_side_effect

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakeAgent)

    # No story-gap elicitation for this test.
    from agents.blogging import ghost_writer_agent

    monkeypatch.setattr(
        ghost_writer_agent.GhostWriterElicitationAgent,
        "find_story_gaps",
        lambda self, _plan: [],
    )
    from agents.blogging.shared import story_bank

    monkeypatch.setattr(story_bank, "find_relevant_stories", lambda *_a, **_kw: [])

    # Outline approval: one round of non-approving feedback, then the re-plan call
    # (mocked above) fails before a second round is ever requested.
    from agents.blogging.agent_implementations.pipeline import planning_stage

    monkeypatch.setattr(planning_stage, "_wait_for_hitl", lambda *_a, **_kw: False)

    import agents.blogging.shared.blog_job_store as blog_job_store

    monkeypatch.setattr(blog_job_store, "request_draft_feedback", lambda *_a, **_kw: None)
    monkeypatch.setattr(blog_job_store, "is_waiting_for_draft_feedback", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        blog_job_store,
        "get_user_draft_feedback",
        lambda *_a, **_kw: {"approved": False, "feedback": "please expand section 2"},
    )

    return PipelineContext(
        brief=ResearchBriefInput(brief="A post about testing"),
        work_dir=None,
        llm_client=object(),
        length_policy=resolve_length_policy(),
        series_context=None,
        job_id="job-1",
        job_updater=lambda **_kw: None,
        draft_editor_iterations=1,
        max_rewrite_iterations=1,
        run_gates=False,
    )


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: __import__(
            "agents.blogging.shared.errors", fromlist=["PlanningError"]
        ).PlanningError("max parse retries exceeded"),
        lambda: __import__(
            "llm_service.interface", fromlist=["LLMRateLimitError"]
        ).LLMRateLimitError("rate limited"),
        lambda: __import__(
            "llm_service.interface", fromlist=["LLMTemporaryError"]
        ).LLMTemporaryError("transient failure"),
    ],
)
def test_replan_failure_propagates_for_retryable_errors(monkeypatch, exc_factory) -> None:
    from agents.blogging.agent_implementations.pipeline.planning_stage import (
        run_planning_stage,
    )

    exc = exc_factory()
    ctx = _make_ctx(monkeypatch, plan_content_side_effect=exc)

    with pytest.raises(type(exc)):
        run_planning_stage(ctx)


def test_replan_failure_swallows_unexpected_error_and_keeps_current_plan(monkeypatch) -> None:
    """A truly unexpected error during re-planning still degrades gracefully."""
    from agents.blogging.agent_implementations.pipeline.planning_stage import (
        run_planning_stage,
    )

    ctx = _make_ctx(monkeypatch, plan_content_side_effect=ValueError("boom"))

    # Second feedback poll approves, so the loop exits after the swallowed failure.
    import agents.blogging.shared.blog_job_store as blog_job_store

    responses = iter(
        [
            {"approved": False, "feedback": "please expand section 2"},
            {"approved": True, "feedback": ""},
        ]
    )
    monkeypatch.setattr(
        blog_job_store, "get_user_draft_feedback", lambda *_a, **_kw: next(responses)
    )

    result = run_planning_stage(ctx)

    assert result is None
    assert ctx.plan is not None
    assert ctx.status == "PASS"


def test_replan_refreshes_claims_and_preserves_research_digest(monkeypatch, tmp_path) -> None:
    """A re-plan must refresh claims and retain the initial research grounding.

    Regression test for the review finding on PR #7408: run_planning's initial
    persist and run_planning_stage's outline re-plan loop must stay in sync via
    the shared _persist_content_plan_artifacts helper.
    """
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.agent_implementations.pipeline.context import PipelineContext
    from agents.blogging.blog_research_agent.allowed_claims import AllowedClaims, ClaimEntry
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate
    from agents.blogging.shared.content_profile import resolve_length_policy

    initial_plan = make_content_plan(
        overarching_topic="Topic",
        narrative_flow="flow",
        sections=[ContentPlanSection(title="Intro", coverage_description="hook", order=0)],
        title_candidates=[TitleCandidate(title="Initial Title", probability_of_success=0.7)],
    )
    refined_plan = make_content_plan(
        overarching_topic="Topic",
        narrative_flow="flow",
        sections=[ContentPlanSection(title="Intro", coverage_description="hook", order=0)],
        title_candidates=[TitleCandidate(title="Refined Title", probability_of_success=0.7)],
    )
    ppr_initial = make_planning_phase_result(
        initial_plan, planning_iterations_used=1, planning_wall_ms_total=10.0
    )
    ppr_refined = make_planning_phase_result(
        refined_plan, planning_iterations_used=1, planning_wall_ms_total=10.0
    )
    planning_digests: list[str] = []

    class _FakeAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, planning_input, **_kw):
            planning_digests.append(planning_input.research_digest)
            if "Author feedback" in planning_input.brief:
                return ppr_refined
            return ppr_initial

    from agents.blogging.blog_research_agent.models import ResearchAgentOutput, ResearchReference

    compiled_research = "# Blog Post Research\n\n## Sources\n\nEvidence."

    class _StubResearchAgent:
        def __init__(self, **_kw):
            pass

        def run(self, _brief):
            return ResearchAgentOutput(
                query_plan=[],
                references=[
                    ResearchReference(
                        url="https://example.com", title="Example", summary="Evidence"
                    )
                ],
                compiled_document=compiled_research,
            )

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakeAgent)
    monkeypatch.setattr(v2, "ResearchAgent", _StubResearchAgent)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: None)

    def _fake_extract(llm_client, compiled_document, references, topic=""):
        if "Refined Title" in compiled_document:
            claims = [ClaimEntry(id="r1", text="Refined claim.", citations=[], risk_level="low")]
        else:
            claims = [ClaimEntry(id="i1", text="Initial claim.", citations=[], risk_level="low")]
        return AllowedClaims(topic=topic, claims=claims)

    monkeypatch.setattr(v2, "extract_allowed_claims", _fake_extract)

    # No story-gap elicitation for this test.
    from agents.blogging import ghost_writer_agent

    monkeypatch.setattr(
        ghost_writer_agent.GhostWriterElicitationAgent,
        "find_story_gaps",
        lambda self, _plan: [],
    )
    from agents.blogging.shared import story_bank

    monkeypatch.setattr(story_bank, "find_relevant_stories", lambda *_a, **_kw: [])

    from agents.blogging.agent_implementations.pipeline import planning_stage

    monkeypatch.setattr(planning_stage, "_wait_for_hitl", lambda *_a, **_kw: False)

    import agents.blogging.shared.blog_job_store as blog_job_store

    monkeypatch.setattr(blog_job_store, "request_draft_feedback", lambda *_a, **_kw: None)
    monkeypatch.setattr(blog_job_store, "is_waiting_for_draft_feedback", lambda *_a, **_kw: False)
    responses = iter(
        [
            {"approved": False, "feedback": "please expand section 2"},
            {"approved": True, "feedback": ""},
        ]
    )
    monkeypatch.setattr(
        blog_job_store, "get_user_draft_feedback", lambda *_a, **_kw: next(responses)
    )

    from agents.blogging.agent_implementations.pipeline.planning_stage import run_planning_stage

    ctx = PipelineContext(
        brief=ResearchBriefInput(brief="A post about testing"),
        work_dir=tmp_path,
        llm_client=object(),
        length_policy=resolve_length_policy(),
        series_context=None,
        job_id="job-1",
        job_updater=lambda **_kw: None,
        draft_editor_iterations=1,
        max_rewrite_iterations=1,
        run_gates=False,
    )

    result = run_planning_stage(ctx)

    assert result is None
    assert ctx.plan.title_candidates[0].title == "Refined Title"
    assert planning_digests == [compiled_research, compiled_research]

    written = json.loads((tmp_path / "allowed_claims.json").read_text())
    assert written["claims"] == [
        {"id": "r1", "text": "Refined claim.", "citations": [], "risk_level": "low"}
    ]
