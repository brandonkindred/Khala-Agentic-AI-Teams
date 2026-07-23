"""Additional tests for blog_writer_agent helper methods.

Covers ``_fix_deterministic_violations``, ``_llm_self_review``, ``_self_review``,
``_format_feedback_item_line``, and the ``revise()`` no-op paths. The
``shared.content_planning_loop`` planning helpers (``post_validate_plan``,
``is_planner_self_eval_satisfied``, ``build_generate_plan_prompt``, ``build_refine_plan_prompt``)
are tested directly in ``test_content_planning_loop.py`` — not duplicated here.
"""

from __future__ import annotations

import json

import pytest


def _make_agent_with_guidelines():
    from .conftest import make_writer_agent

    return make_writer_agent(
        writing_style_guide_content="Style Guide", brand_spec_content="Brand Spec"
    )


def test_writer_fix_deterministic_violations(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, prompt, system_prompt="": (
            '{"draft": 0}\n---DRAFT---\n# Fixed draft\nClean text.'
        ),
    )
    out = a._fix_deterministic_violations("original draft", ["Em dash found"])
    assert "Fixed draft" in out


def test_writer_fix_deterministic_violations_unexpected_error_propagates(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()

    def boom(self, prompt, system_prompt=""):
        raise RuntimeError("programming bug")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)

    with pytest.raises(RuntimeError, match="programming bug"):
        a._fix_deterministic_violations("orig", ["x"])


def test_writer_fix_deterministic_violations_empty_response(monkeypatch) -> None:
    """If LLM returns nothing extractable, keep original."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    monkeypatch.setattr(BlogWriterAgent, "_call_text", lambda *a, **kw: "no marker text")
    assert a._fix_deterministic_violations("orig", ["v"]) == "orig"


def test_writer_llm_self_review_no_issues(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    monkeypatch.setattr(BlogWriterAgent, "_call_text", lambda self, prompt, system_prompt="": "[]")
    out = a._llm_self_review("draft text")
    assert out == "draft text"


def test_writer_llm_self_review_with_issues(monkeypatch) -> None:
    """When review returns issues, the agent applies fixes via a second call."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    state = {"i": 0}

    def fake(self, prompt, system_prompt=""):
        state["i"] += 1
        if state["i"] == 1:
            return json.dumps([{"location": "intro", "issue": "vague", "fix": "be specific"}])
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake)
    out = a._llm_self_review("draft text")
    assert "Better draft" in out


def test_writer_llm_self_review_no_array(monkeypatch) -> None:
    """No JSON array → return draft unchanged."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    monkeypatch.setattr(
        BlogWriterAgent, "_call_text", lambda self, prompt, system_prompt="": "just text"
    )
    out = a._llm_self_review("draft text")
    assert out == "draft text"


def test_writer_llm_self_review_unexpected_error_propagates(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()

    def boom(self, prompt, system_prompt=""):
        raise RuntimeError("programming bug")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)

    with pytest.raises(RuntimeError, match="programming bug"):
        a._llm_self_review("orig")


def test_writer_self_review_combines_both(monkeypatch) -> None:
    """_self_review runs deterministic + LLM passes."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, prompt, system_prompt="": '{"draft": 0}\n---DRAFT---\n# Result\nGood text.',
    )
    # Force at least one violation
    draft = "In today's fast-paced world—Studies show."
    out = a._self_review(draft)
    assert "—" not in out
    assert "Good text" in out


def test_writer_format_feedback_item_line() -> None:
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem

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
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _make_agent_with_guidelines()
    plan = make_content_plan(
        overarching_topic="X",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    out = a.revise(
        ReviseWriterInput(
            draft="   ",
            feedback_items=[],
            feedback_summary="",
            content_plan=plan,
        )
    )
    # revise() strips only to check for emptiness; it returns the original,
    # unstripped draft as-is when that check trips.
    assert out.draft == "   "


def test_writer_revise_no_feedback_items() -> None:
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _make_agent_with_guidelines()
    plan = make_content_plan(
        overarching_topic="X",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
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
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_json_raw",
        lambda self, prompt, system_prompt="": '```json\n{"a": 1}\n```',
    )
    data = a._call_agent_json("prompt")
    assert data == {"a": 1}


def test_writer_fix_deterministic_violations_rate_limit_reraises(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMRateLimitError

    a = _make_agent_with_guidelines()

    def boom(self, prompt, system_prompt=""):
        raise LLMRateLimitError("rate limited")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with pytest.raises(LLMRateLimitError, match="rate limited"):
        a._fix_deterministic_violations("orig", ["x"])


def test_writer_fix_deterministic_violations_temporary_reraises(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMTemporaryError

    a = _make_agent_with_guidelines()

    def boom(self, prompt, system_prompt=""):
        raise LLMTemporaryError("temporary")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with pytest.raises(LLMTemporaryError, match="temporary"):
        a._fix_deterministic_violations("orig", ["x"])


def test_writer_llm_self_review_rate_limit_reraises(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMRateLimitError

    a = _make_agent_with_guidelines()

    def boom(self, prompt, system_prompt=""):
        raise LLMRateLimitError("rate limited")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with pytest.raises(LLMRateLimitError, match="rate limited"):
        a._llm_self_review("orig")


def test_writer_llm_self_review_temporary_reraises(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMTemporaryError

    a = _make_agent_with_guidelines()

    def boom(self, prompt, system_prompt=""):
        raise LLMTemporaryError("temporary")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with pytest.raises(LLMTemporaryError, match="temporary"):
        a._llm_self_review("orig")


def test_writer_fix_deterministic_violations_soft_fails_permanent_error(
    monkeypatch, caplog
) -> None:
    import logging

    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMPermanentError

    a = _make_agent_with_guidelines()

    def boom(self, prompt, system_prompt=""):
        raise LLMPermanentError("permanent")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with caplog.at_level(logging.ERROR):
        out = a._fix_deterministic_violations("orig", ["x"])
    assert out == "orig"
    assert any("Deterministic fix LLM call failed" in r.message for r in caplog.records)
    assert any(r.exc_info is not None for r in caplog.records)


def test_writer_llm_self_review_soft_fails_permanent_error(monkeypatch, caplog) -> None:
    import logging

    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMPermanentError

    a = _make_agent_with_guidelines()

    def boom(self, prompt, system_prompt=""):
        raise LLMPermanentError("permanent")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with caplog.at_level(logging.ERROR):
        out = a._llm_self_review("orig")
    assert out == "orig"
    assert any("LLM self-review failed" in r.message for r in caplog.records)
    assert any(r.exc_info is not None for r in caplog.records)


def test_writer_llm_self_review_soft_fails_json_decode(monkeypatch, caplog) -> None:
    import logging

    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, prompt, system_prompt="": "[not-valid-json",
    )
    with caplog.at_level(logging.ERROR):
        out = a._llm_self_review("orig")
    assert out == "orig"
    assert any("LLM self-review failed" in r.message for r in caplog.records)
