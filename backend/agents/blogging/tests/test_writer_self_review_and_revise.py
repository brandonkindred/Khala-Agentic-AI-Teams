"""Additional tests for blog_writer_agent helper methods.

Covers ``_fix_deterministic_violations``, ``_llm_self_review``, ``_self_review``,
``_format_feedback_item_line``, and the ``revise()`` no-op paths. The
``shared.content_planning_loop`` planning helpers (``post_validate_plan``,
``is_planner_self_eval_satisfied``, ``build_generate_plan_prompt``, ``build_refine_plan_prompt``)
are tested directly in ``test_content_planning_loop.py`` — not duplicated here.
"""

from __future__ import annotations

import json
import logging

import pytest


def _make_agent_with_guidelines():
    from .conftest import make_writer_agent

    return make_writer_agent(
        writing_style_guide_content="Style Guide", brand_spec_content="Brand Spec"
    )


def test_writer_fix_deterministic_violations(monkeypatch) -> None:
    """A clean LLM response with a draft marker applies the fixed draft."""
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
    """An unexpected programming error (not an LLM error) propagates unhandled."""
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
    """An empty JSON array review response returns the draft unchanged."""
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


def test_writer_llm_self_review_with_markdown_fenced_array(monkeypatch) -> None:
    """Issues array wrapped in markdown fences must still be extracted correctly.

    Verifies that the shared ``extract_json_from_response`` helper strips
    fences and parses the enclosed JSON array, so a cleanly fenced review
    response is handled correctly.
    """
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    state = {"i": 0}

    def fake(self, prompt, system_prompt=""):
        state["i"] += 1
        if state["i"] == 1:
            issues = json.dumps([{"location": "intro", "issue": "vague", "fix": "be specific"}])
            return f"Here is my review:\n```json\n{issues}\n```"
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


def test_writer_llm_self_review_markdown_link_before_fenced_array(monkeypatch) -> None:
    """Markdown brackets before a fenced issues array must not block extraction.

    Naive first-``[`` / last-``]`` slicing grabs the Markdown link and fails;
    ``extract_json_from_response`` reads the fenced JSON array and applies fixes.
    """
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    state = {"i": 0}
    review_payload = (
        "See [docs](https://example.com/guide) for context.\n\n"
        "```json\n"
        '[{"location": "intro", "issue": "vague", "fix": "be specific"}]\n'
        "```"
    )

    def fake(self, prompt, system_prompt=""):
        state["i"] += 1
        if state["i"] == 1:
            return review_payload
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake)
    out = a._llm_self_review("draft text")
    assert "Better draft" in out
    assert state["i"] == 2


def test_writer_llm_self_review_numeric_citation_before_array_applies_fixes(monkeypatch) -> None:
    """A numeric citation bracket like ``[1]`` must not be mistaken for the issues array.

    Regression test: ``[1]`` is a syntactically valid JSON array, but its
    elements don't match the expected object schema, so the scanner must keep
    looking for the real issues array instead of short-circuiting on it.
    """
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    state = {"i": 0}
    review_payload = (
        "Per the style guide [1], here are the issues:\n"
        '[{"location": "intro", "issue": "vague", "fix": "be specific"}]'
    )

    def fake(self, prompt, system_prompt=""):
        state["i"] += 1
        if state["i"] == 1:
            return review_payload
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake)
    out = a._llm_self_review("draft text")
    assert "Better draft" in out
    assert state["i"] == 2


def test_writer_llm_self_review_unrelated_dict_array_before_issues_applies_fixes(
    monkeypatch,
) -> None:
    """A fenced JSON object containing an unrelated dict array must not hide a later issues array.

    Regression test: the scanner must not stop at the fenced object (or its nested
    ``references`` list, which lacks ``issue`` keys) and must keep looking for the
    real issues array instead of short-circuiting on it.
    """
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    state = {"i": 0}
    review_payload = (
        'Example metadata:\n```json\n{"references": [{"title": "source"}]}\n```\n'
        "Actual issues:\n"
        '[{"location": "intro", "issue": "vague", "fix": "be specific"}]'
    )

    def fake(self, prompt, system_prompt=""):
        state["i"] += 1
        if state["i"] == 1:
            return review_payload
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake)
    out = a._llm_self_review("draft text")
    assert "Better draft" in out
    assert state["i"] == 2


def test_writer_llm_self_review_mixed_array_keeps_valid_dict_issue(monkeypatch) -> None:
    """A non-dict element alongside a valid issue dict must not drop the real issue.

    Regression test: ``_extract_json_array_from_text`` used to reject the whole
    array unless every element was a dict, discarding a valid issue whenever
    the model's array also contained a stray non-dict entry.
    """
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    state = {"i": 0}
    review_payload = (
        "Here are the issues: "
        '[{"location": "intro", "issue": "vague", "fix": "be specific"}, "malformed"]'
    )

    def fake(self, prompt, system_prompt=""):
        state["i"] += 1
        if state["i"] == 1:
            return review_payload
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake)
    out = a._llm_self_review("draft text")
    assert "Better draft" in out
    assert state["i"] == 2


def test_writer_llm_self_review_direct_list_without_issue_key_returns_draft(monkeypatch) -> None:
    """A clean top-level JSON array whose objects lack an ``issue`` key is treated as no issues.

    Regression test: when ``extract_json_from_response`` parses the whole response
    directly into a list (no rescan involved), elements missing the required
    ``issue`` key must be filtered out just like the prose-rescan fallback does
    via ``required_keys``, not passed through to the fix prompt as-is.
    """
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    calls = {"n": 0}

    def fake(self, prompt, system_prompt=""):
        calls["n"] += 1
        return '[{"title": "source"}, {"title": "other"}]'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake)
    out = a._llm_self_review("draft text")
    assert out == "draft text"
    assert calls["n"] == 1


def test_writer_llm_self_review_direct_list_drops_entries_without_issue_key(monkeypatch) -> None:
    """A direct-parsed list keeps only elements with an ``issue`` key, applying fixes for those.

    Regression test: a mix of a valid issue dict and an unrelated dict (no
    ``issue`` key) parsed directly as a top-level array must still surface the
    real issue instead of either dropping it or passing the unrelated dict
    through to the fix prompt.
    """
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    state = {"i": 0}
    review_payload = (
        '[{"title": "unrelated"}, {"location": "intro", "issue": "vague", "fix": "be specific"}]'
    )

    def fake(self, prompt, system_prompt=""):
        state["i"] += 1
        if state["i"] == 1:
            return review_payload
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake)
    out = a._llm_self_review("draft text")
    assert "Better draft" in out
    assert state["i"] == 2


def test_writer_llm_self_review_non_list_json_returns_draft(monkeypatch) -> None:
    """A JSON object (not an array) is treated as no issues."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    calls = {"n": 0}

    def fake(self, prompt, system_prompt=""):
        calls["n"] += 1
        return '{"status": "ok"}'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake)
    out = a._llm_self_review("draft text")
    assert out == "draft text"
    assert calls["n"] == 1


def test_writer_llm_self_review_object_with_nested_arrays_returns_draft(monkeypatch) -> None:
    """A parsed JSON object with nested arrays must not be rescanned as issues."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    calls = {"n": 0}
    payload = json.dumps(
        {
            "status": "ok",
            "references": [{"title": "source"}],
            "warnings": [],
        }
    )

    def fake(self, prompt, system_prompt=""):
        calls["n"] += 1
        return payload

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake)
    out = a._llm_self_review("draft text")
    assert out == "draft text"
    assert calls["n"] == 1


def test_writer_llm_self_review_fenced_object_before_array_applies_fixes(monkeypatch) -> None:
    """An unrelated fenced object must not hide a later issues array."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    state = {"i": 0}
    review_payload = (
        'Example metadata:\n```json\n{"status": "example"}\n```\n'
        "Actual issues:\n"
        '[{"location": "intro", "issue": "vague", "fix": "be specific"}]'
    )

    def fake(self, prompt, system_prompt=""):
        state["i"] += 1
        if state["i"] == 1:
            return review_payload
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake)
    out = a._llm_self_review("draft text")
    assert "Better draft" in out
    assert state["i"] == 2


def test_writer_llm_self_review_prose_prefixed_array_applies_fixes(monkeypatch) -> None:
    """Unfenced prose before a valid issues array must still apply fixes."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    state = {"i": 0}
    review_payload = (
        'Here are the issues: [{"location": "intro", "issue": "vague", "fix": "be specific"}]'
    )

    def fake(self, prompt, system_prompt=""):
        state["i"] += 1
        if state["i"] == 1:
            return review_payload
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake)
    out = a._llm_self_review("draft text")
    assert "Better draft" in out
    assert state["i"] == 2


def test_writer_llm_self_review_markdown_link_before_unfenced_array(monkeypatch) -> None:
    """Markdown links before an unfenced issues array must not block extraction."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    state = {"i": 0}
    review_payload = (
        "See [docs](https://example.com/guide) for context.\n"
        '[{"location": "intro", "issue": "vague", "fix": "be specific"}]'
    )

    def fake(self, prompt, system_prompt=""):
        state["i"] += 1
        if state["i"] == 1:
            return review_payload
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake)
    out = a._llm_self_review("draft text")
    assert "Better draft" in out
    assert state["i"] == 2


def test_writer_llm_self_review_unexpected_error_propagates(monkeypatch) -> None:
    """An unexpected programming error (not an LLM error) propagates unhandled."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()

    def boom(self, prompt, system_prompt=""):
        raise RuntimeError("programming bug")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)

    with pytest.raises(RuntimeError, match="programming bug"):
        a._llm_self_review("orig")


def test_writer_self_review_combines_both(monkeypatch) -> None:
    """_self_review runs both the deterministic pass and the LLM pass."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, prompt, system_prompt="": '{"draft": 0}\n---DRAFT---\n# Result\nGood text.',
    )
    calls = []
    original_fix = BlogWriterAgent._fix_deterministic_violations

    def spy_fix(self, draft, violations, allowed_claims_section=""):
        calls.append(draft)
        return original_fix(self, draft, violations, allowed_claims_section)

    monkeypatch.setattr(BlogWriterAgent, "_fix_deterministic_violations", spy_fix)
    # Force at least one violation
    draft = "In today's fast-paced world—Studies show."
    out = a._self_review(draft)
    assert calls == [draft]
    assert "—" not in out
    assert "Good text" in out


def test_writer_format_feedback_item_line() -> None:
    """A well-formed FeedbackItem renders as a single formatted line."""
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
    assert line2 == "1. [minor] x: i"  # severity bracket present; location omitted
    assert "Suggestion:" not in line2


def test_build_revise_all_items_prompt_persistent_issues_getattr() -> None:
    """Sparse persistent-issue objects must not AttributeError during prompt build."""
    from types import SimpleNamespace

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
    sparse = SimpleNamespace()  # no optional attrs
    complete = SimpleNamespace(
        severity="major",
        category="clarity",
        location="intro",
        issue="vague opening",
        suggestion="add concrete example",
        occurrence_count=3,
    )
    revise_input = ReviseWriterInput.model_construct(
        draft="# Draft\n\nBody.",
        feedback_items=[],
        content_plan=plan,
        persistent_issues=[sparse, complete],
        length_guidance="",
        target_word_count=1000,
    )
    prompt = a._build_revise_all_items_prompt(
        draft="# Draft\n\nBody.",
        feedback_items=[],
        revision_plan="Fix persistent issues.",
        style_guide_text="Style Guide",
        revise_input=revise_input,
    )
    assert "PERSISTENT ISSUES" in prompt
    assert "[unknown]" in prompt
    assert "(flagged 0 times)" in prompt
    assert "REQUIRED FIX" in prompt
    assert "[intro]" in prompt
    assert "add concrete example" in prompt
    # Sparse line: no location bracket segment and no REQUIRED FIX tied to item 1 alone —
    # complete item supplies REQUIRED FIX / [intro]; sparse must still render defaults.
    assert "1. [unknown]" in prompt
    assert "2. [major] clarity [intro] (flagged 3 times): vague opening" in prompt


def test_build_revise_all_items_prompt_previous_feedback_items_getattr() -> None:
    """Sparse previous-feedback objects must not AttributeError during prompt build."""
    from types import SimpleNamespace

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
    sparse = SimpleNamespace()  # no optional attrs
    complete = SimpleNamespace(
        severity="minor",
        category="grammar",
        location="para 2",
        issue="missing comma",
    )
    revise_input = ReviseWriterInput.model_construct(
        draft="# Draft\n\nBody.",
        feedback_items=[],
        content_plan=plan,
        previous_feedback_items=[sparse, complete],
        length_guidance="",
        target_word_count=1000,
    )
    prompt = a._build_revise_all_items_prompt(
        draft="# Draft\n\nBody.",
        feedback_items=[],
        revision_plan="Fix feedback.",
        style_guide_text="Style Guide",
        revise_input=revise_input,
    )
    assert "RECENTLY RESOLVED FEEDBACK" in prompt
    assert "1. [unknown]" in prompt
    assert "2. [minor] grammar [para 2]: missing comma" in prompt


def test_build_revise_all_items_prompt_tone_and_audience_no_extra_blank_lines() -> None:
    """Tone/Purpose and Audience prefix lines must not introduce blank lines.

    ``prompt_parts`` is joined with ``"\\n".join``, so a prefix string that
    itself ends with ``\\n`` produces a spurious blank line once joined.
    """
    from agents.blogging.blog_writer_agent.agent import REVISION_TASK_INSTRUCTIONS
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
    revise_input = ReviseWriterInput(
        draft="# Draft\n\nBody.",
        feedback_items=[],
        content_plan=plan,
        tone_or_purpose="technical overview",
        audience="Platform and SRE teams",
        length_guidance="",
        target_word_count=1000,
    )
    prompt = a._build_revise_all_items_prompt(
        draft="# Draft\n\nBody.",
        feedback_items=[],
        revision_plan="Fix things.",
        style_guide_text="Style Guide",
        revise_input=revise_input,
    )
    expected_prefix = (
        "Tone/Purpose: technical overview\n"
        "Audience: Platform and SRE teams\n"
        f"{REVISION_TASK_INSTRUCTIONS}"
    )
    assert prompt.startswith(expected_prefix)


def test_writer_format_feedback_item_line_missing_required_raises() -> None:
    """Duck-typed items missing severity/category/issue raise ValueError, not AttributeError."""
    from types import SimpleNamespace

    a = _make_agent_with_guidelines()
    incomplete = SimpleNamespace(location="para 1", suggestion="fix it")
    with pytest.raises(ValueError, match="missing required fields"):
        a._format_feedback_item_line(incomplete, 1)


def test_writer_format_feedback_item_line_duck_typed() -> None:
    """Non-FeedbackItem objects with the required attributes format successfully."""
    from types import SimpleNamespace

    a = _make_agent_with_guidelines()
    item = SimpleNamespace(
        severity="must_fix",
        category="clarity",
        issue="unclear antecedent",
        location="para 3",
        suggestion="name the subject",
    )
    line = a._format_feedback_item_line(item, 2)
    assert line.startswith("2. [must_fix] clarity [para 3]: unclear antecedent")
    assert "Suggestion: name the subject" in line


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


def test_writer_revise_none_draft_is_treated_as_empty() -> None:
    """Defensively normalize a model-constructed None draft to an empty string."""
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput

    a = _make_agent_with_guidelines()
    revise_input = ReviseWriterInput.model_construct(draft=None, feedback_items=[])
    assert a.revise(revise_input).draft == ""


def test_writer_revise_no_feedback_items() -> None:
    """An empty feedback_items list leaves the draft unchanged."""
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
    """Markdown code fences around a JSON response are stripped before parsing."""
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
    """LLMRateLimitError propagates unwrapped so the retry funnel can catch it."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMRateLimitError

    a = _make_agent_with_guidelines()

    def boom(self, prompt, system_prompt=""):
        raise LLMRateLimitError("rate limited")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with pytest.raises(LLMRateLimitError, match="rate limited"):
        a._fix_deterministic_violations("orig", ["x"])


def test_writer_fix_deterministic_violations_temporary_reraises(monkeypatch) -> None:
    """LLMTemporaryError propagates unwrapped so the retry funnel can catch it."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMTemporaryError

    a = _make_agent_with_guidelines()

    def boom(self, prompt, system_prompt=""):
        raise LLMTemporaryError("temporary")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with pytest.raises(LLMTemporaryError, match="temporary"):
        a._fix_deterministic_violations("orig", ["x"])


def test_writer_llm_self_review_rate_limit_reraises(monkeypatch) -> None:
    """LLMRateLimitError during self-review propagates unwrapped."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMRateLimitError

    a = _make_agent_with_guidelines()

    def boom(self, prompt, system_prompt=""):
        raise LLMRateLimitError("rate limited")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with pytest.raises(LLMRateLimitError, match="rate limited"):
        a._llm_self_review("orig")


def test_writer_llm_self_review_temporary_reraises(monkeypatch) -> None:
    """LLMTemporaryError during self-review propagates unwrapped."""
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
    """Non-transient LLM errors are soft-failed: original draft returned, error logged."""
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
    """Non-transient LLM errors during self-review are soft-failed and logged."""
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


def test_writer_llm_self_review_malformed_json_returns_draft(monkeypatch, caplog) -> None:
    """Malformed JSON that cannot be parsed as a list is treated as no issues, not an exception soft-fail."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent_with_guidelines()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, prompt, system_prompt="": "[not-valid-json",
    )
    with caplog.at_level(logging.INFO):
        out = a._llm_self_review("orig")
    assert out == "orig"
    assert any("response was not a JSON array" in r.message for r in caplog.records)
    assert not any("LLM self-review failed" in r.message for r in caplog.records)


def test_writer_fix_deterministic_violations_unwraps_wrapped_rate_limit(monkeypatch) -> None:
    """A rate-limit error wrapped in EventLoopException is unwrapped before re-raising."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMRateLimitError

    a = _make_agent_with_guidelines()
    wrapped = LLMRateLimitError("rate limited")

    def boom(self, prompt, system_prompt=""):
        raise EventLoopException(wrapped)

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with pytest.raises(LLMRateLimitError) as excinfo:
        a._fix_deterministic_violations("orig", ["x"])
    assert excinfo.value is wrapped
    assert not isinstance(excinfo.value, EventLoopException)


def test_writer_llm_self_review_unwraps_wrapped_permanent_error(monkeypatch, caplog) -> None:
    """A permanent error wrapped in EventLoopException is unwrapped before soft-failing."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMPermanentError

    a = _make_agent_with_guidelines()

    def boom(self, prompt, system_prompt=""):
        raise EventLoopException(LLMPermanentError("permanent"))

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    with caplog.at_level(logging.ERROR):
        out = a._llm_self_review("orig")
    assert out == "orig"
    assert any("LLM self-review failed" in r.message for r in caplog.records)
    assert any(r.exc_info is not None for r in caplog.records)
