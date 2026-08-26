"""Tests for ``blog_writer_agent.revision``, exercised in isolation.

These tests call the module's free functions directly with plain
``call_text``/``call_json`` stand-in callables — no ``BlogWriterAgent``
instantiation — to confirm the module has no hidden dependency on the agent
class. Prompt-content coverage mirrors (without duplicating) the equivalent
agent-level tests in ``test_writer_self_review_and_revise.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
from agents.blogging.blog_writer_agent import revision as rev
from agents.blogging.blog_writer_agent.models import ReviseWriterInput
from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

from ._content_plan_test_utils import make_content_plan


def _make_plan():
    return make_content_plan(
        overarching_topic="X",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )


def _make_revise_input(**overrides):
    defaults = dict(
        draft="# Draft\n\nBody.",
        feedback_items=[],
        content_plan=_make_plan(),
        length_guidance="",
        target_word_count=1000,
    )
    defaults.update(overrides)
    return ReviseWriterInput(**defaults)


# ---------------------------------------------------------------------------
# _format_feedback_item_line
# ---------------------------------------------------------------------------


def test_format_feedback_item_line_missing_required_raises() -> None:
    """Duck-typed items missing severity/category/issue raise ValueError, not AttributeError."""
    incomplete = SimpleNamespace(location="para 1", suggestion="fix it")
    with pytest.raises(ValueError, match="missing required fields"):
        rev._format_feedback_item_line(incomplete, 1)


def test_format_feedback_item_line_duck_typed() -> None:
    """Non-FeedbackItem objects with the required attributes format successfully."""
    item = SimpleNamespace(severity="must_fix", category="clarity", issue="vague", location=None)
    line = rev._format_feedback_item_line(item, 1)
    assert line == "1. [must_fix] clarity: vague"


def test_format_feedback_item_line_rejects_non_positive_index() -> None:
    """A zero or negative index raises ValueError before touching the item."""
    with pytest.raises(ValueError, match="positive int"):
        rev._format_feedback_item_line(SimpleNamespace(), 0)


def test_format_feedback_item_line_includes_suggestion() -> None:
    """A present ``suggestion`` field is appended as a sub-line."""
    item = SimpleNamespace(
        severity="should_fix",
        category="flow",
        issue="choppy",
        location=None,
        suggestion="smooth it out",
    )
    line = rev._format_feedback_item_line(item, 2)
    assert line == "2. [should_fix] flow: choppy\n   Suggestion: smooth it out"


# ---------------------------------------------------------------------------
# build_revision_plan_prompt
# ---------------------------------------------------------------------------


def test_build_revision_plan_prompt_embeds_schema_feedback_and_draft() -> None:
    """The prompt embeds the JSON schema instructions, feedback, and draft."""
    revise_input = _make_revise_input()
    item = FeedbackItem(category="clarity", severity="must_fix", issue="vague opening")
    prompt = rev.build_revision_plan_prompt("# Draft\n\nBody.", [item], revise_input, llm=None)
    assert '"summary"' in prompt
    assert "1. [must_fix] clarity: vague opening" in prompt
    assert "CURRENT DRAFT" in prompt
    assert "# Draft\n\nBody." in prompt


# ---------------------------------------------------------------------------
# build_revise_all_items_prompt
# ---------------------------------------------------------------------------


def test_build_revise_all_items_prompt_persistent_issues_getattr() -> None:
    """Sparse persistent-issue objects must not AttributeError during prompt build."""
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
        content_plan=_make_plan(),
        persistent_issues=[sparse, complete],
        length_guidance="",
        target_word_count=1000,
    )
    prompt = rev.build_revise_all_items_prompt(
        draft="# Draft\n\nBody.",
        feedback_items=[],
        revision_plan="Fix persistent issues.",
        style_guide_text="Style Guide",
        revise_input=revise_input,
        brand_section="Brand Spec",
        llm=None,
    )
    assert "PERSISTENT ISSUES" in prompt
    assert "[unknown]" in prompt
    assert "(flagged 0 times)" in prompt
    assert "REQUIRED FIX" in prompt
    assert "[intro]" in prompt
    assert "add concrete example" in prompt
    assert "1. [unknown]" in prompt
    assert "2. [major] clarity [intro] (flagged 3 times): vague opening" in prompt


def test_build_revise_all_items_prompt_previous_feedback_items_getattr() -> None:
    """Sparse previous-feedback objects must not AttributeError during prompt build."""
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
        content_plan=_make_plan(),
        previous_feedback_items=[sparse, complete],
        length_guidance="",
        target_word_count=1000,
    )
    prompt = rev.build_revise_all_items_prompt(
        draft="# Draft\n\nBody.",
        feedback_items=[],
        revision_plan="Fix feedback.",
        style_guide_text="Style Guide",
        revise_input=revise_input,
        brand_section="Brand Spec",
        llm=None,
    )
    assert "RECENTLY RESOLVED FEEDBACK" in prompt
    assert "1. [unknown]" in prompt
    assert "2. [minor] grammar [para 2]: missing comma" in prompt


def test_build_revise_all_items_prompt_tone_and_audience_no_extra_blank_lines() -> None:
    """Tone/Purpose and Audience prefix lines must not introduce blank lines.

    ``prompt_parts`` is joined with ``"\\n".join``, so a prefix string that
    itself ends with ``\\n`` produces a spurious blank line once joined.
    """
    from agents.blogging.blog_writer_agent.prompts import REVISION_TASK_INSTRUCTIONS

    revise_input = _make_revise_input(
        tone_or_purpose="technical overview",
        audience="Platform and SRE teams",
    )
    prompt = rev.build_revise_all_items_prompt(
        draft="# Draft\n\nBody.",
        feedback_items=[],
        revision_plan="Fix things.",
        style_guide_text="Style Guide",
        revise_input=revise_input,
        brand_section="Brand Spec",
        llm=None,
    )
    expected_prefix = (
        "Tone/Purpose: technical overview\n"
        "Audience: Platform and SRE teams\n"
        f"{REVISION_TASK_INSTRUCTIONS}"
    )
    assert prompt.startswith(expected_prefix)


def test_build_revise_all_items_prompt_selected_title_and_stories() -> None:
    """Selected title and elicited stories are each appended as their own section."""
    revise_input = _make_revise_input(
        selected_title="My Chosen Title",
        elicited_stories="A first-person story.",
    )
    prompt = rev.build_revise_all_items_prompt(
        draft="# Draft\n\nBody.",
        feedback_items=[],
        revision_plan="Fix things.",
        style_guide_text="Style Guide",
        revise_input=revise_input,
        brand_section="Brand Spec",
        llm=None,
    )
    assert "AUTHOR-CHOSEN TITLE (preserve this exact H1): My Chosen Title" in prompt
    assert "AUTHOR'S PERSONAL STORIES" in prompt
    assert "A first-person story." in prompt


def test_build_revise_all_items_prompt_default_length_block() -> None:
    """Absent ``length_guidance`` falls back to the computed target-word-count block."""
    revise_input = _make_revise_input(target_word_count=1000)
    prompt = rev.build_revise_all_items_prompt(
        draft="# Draft\n\nBody.",
        feedback_items=[],
        revision_plan="Fix things.",
        style_guide_text="Style Guide",
        revise_input=revise_input,
        brand_section="Brand Spec",
        llm=None,
    )
    assert "TARGET LENGTH: Aim for roughly 1000 words" in prompt
    assert "(acceptable range: 750–1300 words)" in prompt


def test_build_revise_all_items_prompt_includes_allowed_claims_section() -> None:
    """A non-empty allowed_claims_section is embedded in the prompt."""
    revise_input = _make_revise_input()
    claims_section = "---\nALLOWED CLAIMS...\n---\n- [c1] Some claim."
    prompt = rev.build_revise_all_items_prompt(
        draft="# Draft\n\nBody.",
        feedback_items=[],
        revision_plan="Fix things.",
        style_guide_text="Style Guide",
        revise_input=revise_input,
        brand_section="Brand Spec",
        llm=None,
        allowed_claims_section=claims_section,
    )
    assert claims_section in prompt


def test_build_revise_all_items_prompt_omits_allowed_claims_section_by_default() -> None:
    """The default (empty) allowed_claims_section adds nothing to the prompt."""
    revise_input = _make_revise_input()
    prompt = rev.build_revise_all_items_prompt(
        draft="# Draft\n\nBody.",
        feedback_items=[],
        revision_plan="Fix things.",
        style_guide_text="Style Guide",
        revise_input=revise_input,
        brand_section="Brand Spec",
        llm=None,
    )
    assert "ALLOWED CLAIMS" not in prompt


# ---------------------------------------------------------------------------
# generate_revision_plan
# ---------------------------------------------------------------------------


def test_generate_revision_plan_happy_path() -> None:
    """Valid JSON parses into a RevisionPlan with its changes and risks."""
    revise_input = _make_revise_input()

    def call_json(prompt: str, system_prompt: str = "") -> dict:
        return {
            "summary": "Tighten the intro.",
            "changes": [
                {"section": "intro", "feedback_ids": [1], "action": "rewrite", "rationale": "vague"}
            ],
            "risks": ["May shorten the post."],
        }

    def call_text(prompt: str, system_prompt: str = "") -> str:
        raise AssertionError("call_text should not be used on the happy path")

    plan = rev.generate_revision_plan(
        "# Draft", [], revise_input, call_json=call_json, call_text=call_text
    )
    assert plan.summary == "Tighten the intro."
    assert len(plan.changes) == 1
    assert plan.changes[0].section == "intro"
    assert plan.risks == ["May shorten the post."]


def test_generate_revision_plan_malformed_changes_and_risks_are_skipped(caplog) -> None:
    """Non-list ``changes``/``risks`` and malformed change dicts are defensively dropped."""
    revise_input = _make_revise_input()

    def call_json(prompt: str, system_prompt: str = "") -> dict:
        return {
            "summary": "ok",
            "changes": [{"section": "x"}, "not-a-dict"],  # missing required fields -> skipped
            "risks": "not-a-list",
        }

    def call_text(prompt: str, system_prompt: str = "") -> str:
        raise AssertionError("call_text should not be used")

    with caplog.at_level("WARNING"):
        plan = rev.generate_revision_plan(
            "# Draft", [], revise_input, call_json=call_json, call_text=call_text
        )
    assert plan.summary == "ok"
    assert plan.changes == []
    assert plan.risks == []


def test_generate_revision_plan_non_list_changes_is_dropped(caplog) -> None:
    """A non-list ``changes`` value is defensively dropped (not just non-dict entries)."""
    revise_input = _make_revise_input()

    def call_json(prompt: str, system_prompt: str = "") -> dict:
        return {"summary": "ok", "changes": "not-a-list", "risks": []}

    def call_text(prompt: str, system_prompt: str = "") -> str:
        raise AssertionError("call_text should not be used")

    with caplog.at_level("WARNING"):
        plan = rev.generate_revision_plan(
            "# Draft", [], revise_input, call_json=call_json, call_text=call_text
        )
    assert plan.changes == []


def test_generate_revision_plan_none_summary_defaults_to_empty_string() -> None:
    """A ``None`` summary is normalized to an empty string, not passed through as None."""
    revise_input = _make_revise_input()

    def call_json(prompt: str, system_prompt: str = "") -> dict:
        return {"summary": None, "changes": [], "risks": []}

    plan = rev.generate_revision_plan(
        "# Draft",
        [],
        revise_input,
        call_json=call_json,
        call_text=lambda p, s="": (_ for _ in ()).throw(AssertionError("unused")),
    )
    assert plan.summary == ""


def test_generate_revision_plan_non_string_risk_entry_falls_back_to_plain_text() -> None:
    """A non-string risk entry triggers the plain-text fallback path."""
    revise_input = _make_revise_input()

    def call_json(prompt: str, system_prompt: str = "") -> dict:
        return {"summary": "ok", "changes": [], "risks": [123]}

    def call_text(prompt: str, system_prompt: str = "") -> str:
        return "Plain text plan from bad risk."

    plan = rev.generate_revision_plan(
        "# Draft", [], revise_input, call_json=call_json, call_text=call_text
    )
    assert plan.summary == "Plain text plan from bad risk."


def test_generate_revision_plan_fallback_unexpected_error_propagates() -> None:
    """An unexpected programming error from the plain-text fallback propagates unhandled."""
    from llm_service import LLMJsonParseError

    revise_input = _make_revise_input()

    def call_json(prompt: str, system_prompt: str = "") -> dict:
        raise LLMJsonParseError("bad json")

    def call_text(prompt: str, system_prompt: str = "") -> str:
        raise RuntimeError("programming bug in fallback")

    with pytest.raises(RuntimeError, match="programming bug in fallback"):
        rev.generate_revision_plan(
            "# Draft", [], revise_input, call_json=call_json, call_text=call_text
        )


def test_generate_revision_plan_non_string_summary_falls_back_to_plain_text() -> None:
    """A non-string 'summary' triggers the plain-text fallback path."""
    revise_input = _make_revise_input()

    def call_json(prompt: str, system_prompt: str = "") -> dict:
        return {"summary": 123, "changes": [], "risks": []}

    def call_text(prompt: str, system_prompt: str = "") -> str:
        return "Plain text plan."

    plan = rev.generate_revision_plan(
        "# Draft", [], revise_input, call_json=call_json, call_text=call_text
    )
    assert plan.summary == "Plain text plan."
    assert plan.changes == []
    assert plan.risks == []


def test_generate_revision_plan_call_json_none_returns_no_output_plan() -> None:
    """A None/non-dict response from call_json yields the 'no output' plan."""
    revise_input = _make_revise_input()

    plan = rev.generate_revision_plan(
        "# Draft",
        [],
        revise_input,
        call_json=lambda p, s="": None,
        call_text=lambda p, s="": (_ for _ in ()).throw(AssertionError("unused")),
    )
    assert plan.summary == "Planning produced no output."


def test_generate_revision_plan_rate_limit_error_propagates_unwrapped() -> None:
    """LLMRateLimitError propagates unwrapped so the retry funnel can catch it."""
    from llm_service import LLMRateLimitError

    revise_input = _make_revise_input()

    def call_json(prompt: str, system_prompt: str = "") -> dict:
        raise LLMRateLimitError("rate limited")

    with pytest.raises(LLMRateLimitError, match="rate limited"):
        rev.generate_revision_plan(
            "# Draft", [], revise_input, call_json=call_json, call_text=lambda p, s="": ""
        )


def test_generate_revision_plan_temporary_error_propagates_unwrapped() -> None:
    """LLMTemporaryError propagates unwrapped so the retry funnel can catch it."""
    from llm_service import LLMTemporaryError

    revise_input = _make_revise_input()

    def call_json(prompt: str, system_prompt: str = "") -> dict:
        raise LLMTemporaryError("temporary")

    with pytest.raises(LLMTemporaryError, match="temporary"):
        rev.generate_revision_plan(
            "# Draft", [], revise_input, call_json=call_json, call_text=lambda p, s="": ""
        )


def test_generate_revision_plan_rate_limit_error_unwrapped_from_event_loop_exception() -> None:
    """A rate-limit error wrapped in EventLoopException is unwrapped before re-raising."""
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMRateLimitError

    revise_input = _make_revise_input()
    wrapped = LLMRateLimitError("rate limited")

    def call_json(prompt: str, system_prompt: str = "") -> dict:
        raise EventLoopException(wrapped)

    with pytest.raises(LLMRateLimitError) as excinfo:
        rev.generate_revision_plan(
            "# Draft", [], revise_input, call_json=call_json, call_text=lambda p, s="": ""
        )
    assert not isinstance(excinfo.value, EventLoopException)


def test_generate_revision_plan_unexpected_error_propagates() -> None:
    """An unexpected programming error (not an LLMError) propagates unhandled."""
    revise_input = _make_revise_input()

    def call_json(prompt: str, system_prompt: str = "") -> dict:
        raise RuntimeError("programming bug")

    with pytest.raises(RuntimeError, match="programming bug"):
        rev.generate_revision_plan(
            "# Draft", [], revise_input, call_json=call_json, call_text=lambda p, s="": ""
        )


def test_generate_revision_plan_json_failure_falls_back_to_plain_text() -> None:
    """A structured-response LLMError falls back to the plain-text plan via call_text."""
    from llm_service import LLMJsonParseError

    revise_input = _make_revise_input()

    def call_json(prompt: str, system_prompt: str = "") -> dict:
        raise LLMJsonParseError("bad json")

    def call_text(prompt: str, system_prompt: str = "") -> str:
        return "  Fallback plan text.  "

    plan = rev.generate_revision_plan(
        "# Draft", [], revise_input, call_json=call_json, call_text=call_text
    )
    assert plan.summary == "Fallback plan text."
    assert plan.changes == []
    assert plan.risks == []


def test_generate_revision_plan_both_json_and_text_fail_returns_final_fallback() -> None:
    """When both call_json and call_text fail with LLMError, the final fallback plan is returned."""
    from llm_service import LLMJsonParseError

    revise_input = _make_revise_input()

    def call_json(prompt: str, system_prompt: str = "") -> dict:
        raise LLMJsonParseError("bad json")

    def call_text(prompt: str, system_prompt: str = "") -> str:
        raise LLMJsonParseError("still bad")

    plan = rev.generate_revision_plan(
        "# Draft", [], revise_input, call_json=call_json, call_text=call_text
    )
    assert plan.summary == "Revision planning failed."


def test_generate_revision_plan_text_fallback_rate_limit_propagates() -> None:
    """A rate-limit error raised from the plain-text fallback call still propagates unwrapped."""
    from llm_service import LLMJsonParseError, LLMRateLimitError

    revise_input = _make_revise_input()

    def call_json(prompt: str, system_prompt: str = "") -> dict:
        raise LLMJsonParseError("bad json")

    def call_text(prompt: str, system_prompt: str = "") -> str:
        raise LLMRateLimitError("rate limited on fallback")

    with pytest.raises(LLMRateLimitError, match="rate limited on fallback"):
        rev.generate_revision_plan(
            "# Draft", [], revise_input, call_json=call_json, call_text=call_text
        )
