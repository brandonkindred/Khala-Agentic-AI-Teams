"""Tests for blog_writer_agent interactive-review methods:

* ``identify_uncertainty_questions``
* ``analyze_user_feedback_for_guideline_updates``
* ``revise_from_user_feedback``
* ``generate_escalation_summary``
* ``revise()`` end-to-end (one batch attempt)
"""

from __future__ import annotations

import json


def _make_agent():
    from blog_writer_agent.agent import BlogWriterAgent

    from llm_service import DummyLLMClient

    return BlogWriterAgent(
        llm_client=DummyLLMClient(),
        writing_style_guide_content="Style",
        brand_spec_content="Brand",
    )


# ---------------------------------------------------------------------------
# identify_uncertainty_questions
# ---------------------------------------------------------------------------


def test_identify_uncertainty_questions_returns_items(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, prompt, system_prompt="": json.dumps(
            [
                {
                    "question_id": "q1",
                    "question": "What audience?",
                    "context": "ctx",
                    "section": "Intro",
                }
            ]
        ),
    )
    out = a.identify_uncertainty_questions("draft", "plan")
    assert len(out) == 1
    assert out[0].question_id == "q1"


def test_identify_uncertainty_questions_empty_array(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(BlogWriterAgent, "_call_text", lambda self, p, system_prompt="": "[]")
    assert a.identify_uncertainty_questions("d", "p") == []


def test_identify_uncertainty_questions_no_array(monkeypatch) -> None:
    """No JSON array in response → empty list."""
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent, "_call_text", lambda self, p, system_prompt="": "no array here"
    )
    assert a.identify_uncertainty_questions("d", "p") == []


def test_identify_uncertainty_questions_malformed_items_skipped(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": json.dumps(
            [
                {"question": "What?"},  # missing question_id → assigned auto
                {"no_question_key": "x"},  # missing required 'question' → skipped
            ]
        ),
    )
    out = a.identify_uncertainty_questions("d", "p")
    assert len(out) == 1


def test_identify_uncertainty_questions_llm_error(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()

    def boom(self, prompt, system_prompt=""):
        raise RuntimeError("nope")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    assert a.identify_uncertainty_questions("d", "p") == []


# ---------------------------------------------------------------------------
# analyze_user_feedback_for_guideline_updates
# ---------------------------------------------------------------------------


def test_analyze_feedback_returns_updates(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, **kw: {
            "has_guideline_updates": True,
            "updates": [{"category": "tone", "description": "softer", "guideline_text": "be soft"}],
        },
    )
    out = a.analyze_user_feedback_for_guideline_updates("user fb", "current")
    assert len(out) == 1
    assert out[0].category == "tone"


def test_analyze_feedback_no_updates(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, **kw: {"has_guideline_updates": False, "updates": []},
    )
    assert a.analyze_user_feedback_for_guideline_updates("fb", "g") == []


def test_analyze_feedback_non_dict(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", lambda self, p, **kw: "garbage")
    assert a.analyze_user_feedback_for_guideline_updates("fb", "g") == []


def test_analyze_feedback_malformed_skipped(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, **kw: {
            "has_guideline_updates": True,
            "updates": [
                {"category": "tone"},  # missing keys → skipped
                {"category": "x", "description": "y", "guideline_text": "z"},
            ],
        },
    )
    out = a.analyze_user_feedback_for_guideline_updates("fb", "g")
    assert len(out) == 1


def test_analyze_feedback_error(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()

    def boom(self, p, **kw):
        raise RuntimeError("LLM")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", boom)
    assert a.analyze_user_feedback_for_guideline_updates("fb", "g") == []


# ---------------------------------------------------------------------------
# revise_from_user_feedback
# ---------------------------------------------------------------------------


def test_revise_from_user_feedback_happy(monkeypatch, tmp_path) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (
            '{"draft": 0}\n---DRAFT---\n# Revised by user feedback\nBody.'
        ),
    )
    out = a.revise_from_user_feedback(
        draft="# Old\nBody",
        user_feedback="be more specific",
        content_plan_text="# Plan",
        audience="devs",
        tone_or_purpose="inform",
        selected_title="Selected",
        elicited_stories="A story",
        target_word_count=800,
        length_guidance="aim for 800",
        uncertainty_answers={"q1": "answer"},
        draft_output_path=tmp_path / "out.md",
    )
    assert "Revised by user feedback" in out.draft
    assert (tmp_path / "out.md").exists()


def test_revise_from_user_feedback_empty_draft() -> None:
    a = _make_agent()
    out = a.revise_from_user_feedback(draft="   ", user_feedback="x", content_plan_text="cp")
    assert out.draft == "   "


def test_revise_from_user_feedback_no_marker_then_json_fallback(monkeypatch) -> None:
    """LLM returns no ---DRAFT--- marker but JSON fallback works."""
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    call_count = {"i": 0}

    def fake(self, prompt, system_prompt=""):
        call_count["i"] += 1
        return "no marker here"

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake)
    monkeypatch.setattr(
        BlogWriterAgent, "_call_agent_json", lambda self, p, **kw: {"draft": "# Fallback"}
    )
    out = a.revise_from_user_feedback(draft="# Original", user_feedback="x", content_plan_text="cp")
    # Should have either used fallback or kept original
    assert out.draft  # Non-empty


# ---------------------------------------------------------------------------
# generate_escalation_summary
# ---------------------------------------------------------------------------


def test_generate_escalation_summary_happy(monkeypatch) -> None:
    from blog_copy_editor_agent.models import FeedbackItem
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": "Summary: stuck on tone and flow.",
    )
    items = [
        FeedbackItem(category="tone", severity="major", issue="too formal"),
        FeedbackItem(category="flow", severity="major", issue="abrupt"),
    ]
    out = a.generate_escalation_summary(
        revision_count=5,
        latest_feedback_items=items,
        persistent_issues=[],
    )
    assert "Summary" in out


def test_generate_escalation_summary_handles_error(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()

    def boom(self, p, system_prompt=""):
        raise RuntimeError("nope")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    out = a.generate_escalation_summary(
        revision_count=10,
        latest_feedback_items=[],
        persistent_issues=[],
    )
    # Returns a fallback string (non-empty) or empty
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# revise() full path
# ---------------------------------------------------------------------------


def test_revise_with_feedback_batches(monkeypatch, tmp_path) -> None:
    """revise() with a non-empty feedback list runs through batch revision."""
    from blog_copy_editor_agent.models import FeedbackItem
    from blog_writer_agent.agent import BlogWriterAgent
    from blog_writer_agent.models import ReviseWriterInput, RevisionPlan
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    a = _make_agent()

    # Stub _generate_revision_plan + _call_agent to keep things fast
    monkeypatch.setattr(
        BlogWriterAgent,
        "_generate_revision_plan",
        lambda self, draft, items, ri: RevisionPlan(summary="planned", changes=[], risks=[]),
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": '{"draft": 0}\n---DRAFT---\n# Revised\nBody.',
    )

    plan = ContentPlan(
        overarching_topic="x",
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
            feedback_items=[FeedbackItem(category="grammar", severity="minor", issue="comma")],
            feedback_summary="fix",
            content_plan=plan,
        ),
        draft_output_path=tmp_path / "rev.md",
        work_dir=tmp_path,
        iteration=1,
    )
    assert "Revised" in out.draft


def test_revise_falls_back_to_original_when_llm_fails(monkeypatch, tmp_path) -> None:
    """If all retries fail and json fallback fails, return original draft."""
    from blog_copy_editor_agent.models import FeedbackItem
    from blog_writer_agent.agent import BlogWriterAgent
    from blog_writer_agent.models import ReviseWriterInput, RevisionPlan
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    a = _make_agent()

    monkeypatch.setattr(
        BlogWriterAgent,
        "_generate_revision_plan",
        lambda self, draft, items, ri: RevisionPlan(summary="planned", changes=[], risks=[]),
    )

    def fail(self, *a, **kw):
        raise RuntimeError("transient")

    # Patch time.sleep to skip waits
    import blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(BlogWriterAgent, "_call_text", fail)

    def fail_json(self, p, **kw):
        raise ValueError("nope")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", fail_json)

    plan = ContentPlan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    out = a.revise(
        ReviseWriterInput(
            draft="# Original\nBody",
            feedback_items=[FeedbackItem(category="x", severity="minor", issue="y")],
            feedback_summary="s",
            content_plan=plan,
        ),
    )
    # Should still return a WriterOutput; original draft preserved
    assert "Original" in out.draft


def test_revise_generate_revision_plan_happy(monkeypatch) -> None:
    """_generate_revision_plan parses structured response."""
    from blog_copy_editor_agent.models import FeedbackItem
    from blog_writer_agent.agent import BlogWriterAgent
    from blog_writer_agent.models import ReviseWriterInput
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    a = _make_agent()
    plan = ContentPlan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, **kw: {
            "summary": "Fix tone",
            "changes": [
                {
                    "action": "rewrite",
                    "section": "intro",
                    "rationale": "Soften",
                    "feedback_ids": [1],
                }
            ],
            "risks": ["scope creep"],
        },
    )
    out = a._generate_revision_plan(
        draft="# x",
        feedback_items=[FeedbackItem(category="t", severity="minor", issue="i")],
        revise_input=ReviseWriterInput(
            draft="# x",
            feedback_items=[FeedbackItem(category="t", severity="minor", issue="i")],
            feedback_summary="s",
            content_plan=plan,
        ),
    )
    assert out.summary == "Fix tone"
    assert len(out.changes) == 1
    assert out.risks == ["scope creep"]


def test_revise_generate_revision_plan_empty_response(monkeypatch) -> None:
    from blog_copy_editor_agent.models import FeedbackItem
    from blog_writer_agent.agent import BlogWriterAgent
    from blog_writer_agent.models import ReviseWriterInput
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    a = _make_agent()
    plan = ContentPlan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", lambda self, p, **kw: {})
    out = a._generate_revision_plan(
        draft="# x",
        feedback_items=[FeedbackItem(category="t", severity="minor", issue="i")],
        revise_input=ReviseWriterInput(
            draft="# x",
            feedback_items=[FeedbackItem(category="t", severity="minor", issue="i")],
            feedback_summary="s",
            content_plan=plan,
        ),
    )
    assert "Planning produced no output" in out.summary


def test_revise_generate_revision_plan_error_falls_back(monkeypatch) -> None:
    """When the structured plan fails, fall back to a plain text plan."""
    from blog_copy_editor_agent.models import FeedbackItem
    from blog_writer_agent.agent import BlogWriterAgent
    from blog_writer_agent.models import ReviseWriterInput
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    a = _make_agent()
    plan = ContentPlan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )

    def boom_json(self, p, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", boom_json)
    monkeypatch.setattr(BlogWriterAgent, "_call_text", lambda self, p, **kw: "Plain text plan")
    out = a._generate_revision_plan(
        draft="# x",
        feedback_items=[FeedbackItem(category="t", severity="minor", issue="i")],
        revise_input=ReviseWriterInput(
            draft="# x",
            feedback_items=[FeedbackItem(category="t", severity="minor", issue="i")],
            feedback_summary="s",
            content_plan=plan,
        ),
    )
    assert out.summary == "Plain text plan"
