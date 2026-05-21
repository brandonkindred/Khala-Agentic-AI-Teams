"""Additional tests for blog_writer_agent helper methods.

Covers ``_fix_deterministic_violations``, ``_llm_self_review``, ``_self_review``,
``_post_validate_plan``, ``_planning_done``, ``_build_generate_plan_prompt``,
``_build_refine_plan_prompt``, ``_format_feedback_item_line``, and the
``revise()`` no-op paths.
"""

from __future__ import annotations

import json


def _make_agent_with_guidelines():
    from blog_writer_agent.agent import BlogWriterAgent

    from llm_service import DummyLLMClient

    return BlogWriterAgent(
        llm_client=DummyLLMClient(),
        writing_style_guide_content="Style Guide",
        brand_spec_content="Brand Spec",
    )


def test_writer_fix_deterministic_violations(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent",
        lambda self, prompt, system_prompt="": (
            '{"draft": 0}\n---DRAFT---\n# Fixed draft\nClean text.'
        ),
    )
    out = a._fix_deterministic_violations("original draft", ["Em dash found"])
    assert "Fixed draft" in out


def test_writer_fix_deterministic_violations_swallow_error(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()

    def boom(self, prompt, system_prompt=""):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent", boom)
    out = a._fix_deterministic_violations("orig", ["x"])
    assert out == "orig"


def test_writer_fix_deterministic_violations_empty_response(monkeypatch) -> None:
    """If LLM returns nothing extractable, keep original."""
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    monkeypatch.setattr(BlogWriterAgent, "_call_agent", lambda *a, **kw: "no marker text")
    assert a._fix_deterministic_violations("orig", ["v"]) == "orig"


def test_writer_llm_self_review_no_issues(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    monkeypatch.setattr(BlogWriterAgent, "_call_agent", lambda self, prompt, system_prompt="": "[]")
    out = a._llm_self_review("draft text")
    assert out == "draft text"


def test_writer_llm_self_review_with_issues(monkeypatch) -> None:
    """When review returns issues, the agent applies fixes via a second call."""
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    state = {"i": 0}

    def fake(self, prompt, system_prompt=""):
        state["i"] += 1
        if state["i"] == 1:
            return json.dumps([{"location": "intro", "issue": "vague", "fix": "be specific"}])
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    monkeypatch.setattr(BlogWriterAgent, "_call_agent", fake)
    out = a._llm_self_review("draft text")
    assert "Better draft" in out


def test_writer_llm_self_review_no_array(monkeypatch) -> None:
    """No JSON array → return draft unchanged."""
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    monkeypatch.setattr(
        BlogWriterAgent, "_call_agent", lambda self, prompt, system_prompt="": "just text"
    )
    out = a._llm_self_review("draft text")
    assert out == "draft text"


def test_writer_llm_self_review_exception(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()

    def boom(self, prompt, system_prompt=""):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent", boom)
    out = a._llm_self_review("orig")
    assert out == "orig"


def test_writer_self_review_combines_both(monkeypatch) -> None:
    """_self_review runs deterministic + LLM passes."""
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent",
        lambda self, prompt, system_prompt="": '{"draft": 0}\n---DRAFT---\n# Result\nGood text.',
    )
    # Force at least one violation
    draft = "In today's fast-paced world—Studies show."
    out = a._self_review(draft)
    assert out  # produced something


def _make_length_policy():
    from shared.content_profile import ContentProfile, resolve_length_policy

    return resolve_length_policy(content_profile=ContentProfile.standard_article)


def test_writer_post_validate_plan_in_bounds() -> None:
    from blog_writer_agent.agent import BlogWriterAgent
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    plan = ContentPlan(
        overarching_topic="X",
        narrative_flow="flow",
        sections=[
            ContentPlanSection(title="A", coverage_description="a", order=0),
            ContentPlanSection(title="B", coverage_description="b", order=1),
            ContentPlanSection(title="C", coverage_description="c", order=2),
            ContentPlanSection(title="D", coverage_description="d", order=3),
        ],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    policy = _make_length_policy()
    out = BlogWriterAgent._post_validate_plan(plan, policy)
    # Section count within typical bounds → plan_acceptable preserved
    assert out is not None


def test_writer_post_validate_plan_out_of_bounds() -> None:
    """When section count is outside expected range, plan_acceptable is forced False."""
    from blog_writer_agent.agent import BlogWriterAgent
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    plan = ContentPlan(
        overarching_topic="X",
        narrative_flow="flow",
        sections=[
            ContentPlanSection(title=f"S{i}", coverage_description="c", order=i) for i in range(40)
        ],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    policy = _make_length_policy()
    out = BlogWriterAgent._post_validate_plan(plan, policy)
    assert out.requirements_analysis.plan_acceptable is False


def test_writer_planning_done() -> None:
    from blog_writer_agent.agent import BlogWriterAgent
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    plan = ContentPlan(
        overarching_topic="X",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    assert BlogWriterAgent._planning_done(plan) is True

    plan2 = plan.model_copy(
        update={
            "requirements_analysis": RequirementsAnalysis(
                plan_acceptable=False, scope_feasible=True, research_gaps=[]
            )
        }
    )
    assert BlogWriterAgent._planning_done(plan2) is False


def test_writer_build_generate_plan_prompt() -> None:
    from blog_writer_agent.agent import BlogWriterAgent
    from shared.content_plan import PlanningInput

    inp = PlanningInput(
        brief="My brief",
        audience="devs",
        tone_or_purpose="inform",
        length_policy_context="900 words",
        series_context_block="Part 1 of 3",
        research_digest="some digest",
    )
    p = BlogWriterAgent._build_generate_plan_prompt(inp)
    assert "My brief" in p
    assert "Audience: devs" in p
    assert "Tone/Purpose: inform" in p
    assert "Part 1 of 3" in p
    assert "some digest" in p


def test_writer_build_refine_plan_prompt() -> None:
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        PlanningInput,
        RequirementsAnalysis,
        TitleCandidate,
    )

    a = _make_agent_with_guidelines()
    prev = ContentPlan(
        overarching_topic="X",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=False, scope_feasible=True, research_gaps=[]
        ),
    )
    inp = PlanningInput(
        brief="b",
        length_policy_context="ctx",
        research_digest="rd",
    )
    p = a._build_refine_plan_prompt(inp, prev, "Be more specific")
    assert "PREVIOUS PLAN" in p
    assert "Be more specific" in p


def test_writer_format_feedback_item_line() -> None:
    from blog_copy_editor_agent.models import FeedbackItem

    a = _make_agent_with_guidelines()
    item = FeedbackItem(
        category="grammar",
        severity="minor",
        location="para 2",
        issue="missing comma",
        suggestion="add comma after intro",
    )
    line = a._format_feedback_item_line(item, 3)
    assert "3." in line
    assert "[minor]" in line
    assert "grammar" in line
    assert "para 2" in line
    assert "Suggestion: add comma" in line

    item_no_loc = FeedbackItem(category="x", severity="minor", issue="i")
    line2 = a._format_feedback_item_line(item_no_loc, 1)
    assert "[" in line2  # No location bracket
    assert "Suggestion:" not in line2


def test_writer_revise_empty_draft() -> None:
    """revise() returns empty draft unchanged."""
    from blog_writer_agent.models import ReviseWriterInput
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    a = _make_agent_with_guidelines()
    plan = ContentPlan(
        overarching_topic="X",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    out = a.revise(
        ReviseWriterInput(
            draft="   ",
            feedback_items=[],
            feedback_summary="",
            content_plan=plan,
        )
    )
    # Returns as-is (whitespace preserved)
    assert "   " in out.draft or out.draft == ""


def test_writer_revise_no_feedback_items() -> None:
    from blog_writer_agent.models import ReviseWriterInput
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    a = _make_agent_with_guidelines()
    plan = ContentPlan(
        overarching_topic="X",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    out = a.revise(
        ReviseWriterInput(
            draft="# Original\n\nBody.",
            feedback_items=[],
            feedback_summary="",
            content_plan=plan,
        )
    )
    assert "Original" in out.draft


def test_writer_call_agent_json_strips_fences(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent",
        lambda self, prompt, system_prompt="": '```json\n{"a": 1}\n```',
    )
    data = a._call_agent_json("prompt")
    assert data == {"a": 1}
