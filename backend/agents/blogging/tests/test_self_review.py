"""Tests for ``blog_writer_agent.self_review``'s free functions in isolation.

These exercise the module directly with a fake ``call_text`` callback rather
than going through ``BlogWriterAgent`` — the agent's own copies of this logic
(still in ``agent.py`` until the sibling wiring step) are covered separately
by ``test_writer_self_review_and_revise.py`` and ``test_blog_writer_agent.py``.
"""

from __future__ import annotations

import json

import pytest
from agents.blogging.blog_writer_agent import self_review as sr

from llm_service import LLMError, LLMRateLimitError, LLMTemporaryError

# ---------------------------------------------------------------------------
# _deterministic_self_check
# ---------------------------------------------------------------------------


def test_deterministic_self_check_clean_draft_no_violations() -> None:
    draft = (
        "# A Clean Draft\n\n"
        "You will find this section walks you through the idea carefully, "
        "connecting each point to something you already understand about "
        "your own workflow, and that is exactly the point of this guide.\n\n"
        "You can apply this to your own project whenever you are ready, "
        "and yourself will likely notice the improvement across your team's "
        "workflow within a week or two of consistent practice."
    )
    assert sr._deterministic_self_check(draft) == []


def test_deterministic_self_check_rejects_non_string() -> None:
    with pytest.raises(TypeError, match="draft must be a string"):
        sr._deterministic_self_check(123)  # type: ignore[arg-type]


def test_deterministic_self_check_em_dash() -> None:
    draft = "This is a sentence—with an em dash in it for testing purposes here."
    violations = sr._deterministic_self_check(draft)
    assert any("Em/en dash" in v for v in violations)


def test_deterministic_self_check_banned_phrase() -> None:
    draft = "In today's fast-paced world, everyone is looking for a better way to work."
    violations = sr._deterministic_self_check(draft)
    assert any("Banned phrase" in v and "today's fast-paced world" in v for v in violations)


def test_deterministic_self_check_vague_citation_without_source() -> None:
    draft = "Studies show that people prefer clear writing over jargon-filled prose entirely."
    violations = sr._deterministic_self_check(draft)
    assert any("Vague citation" in v for v in violations)


def test_deterministic_self_check_vague_citation_with_nearby_source_clears() -> None:
    draft = (
        "Studies show that people prefer clear writing "
        "[CLAIM: see https://example.com/research] over jargon-filled prose."
    )
    violations = sr._deterministic_self_check(draft)
    assert not any("Vague citation" in v for v in violations)


def test_deterministic_self_check_low_reader_address_count() -> None:
    draft = "This describes a process. It has several steps. Nobody is addressed directly here."
    violations = sr._deterministic_self_check(draft)
    assert any("Reader address" in v for v in violations)


def test_deterministic_self_check_staccato_streak() -> None:
    draft = "This is short. So is this. Same here. Short again."
    violations = sr._deterministic_self_check(draft)
    assert any("Staccato prose" in v for v in violations)


def test_deterministic_self_check_heading_paragraph_skipped_for_staccato() -> None:
    draft = "# Ok. Go. Now. Yes."
    violations = sr._deterministic_self_check(draft)
    assert not any("Staccato prose" in v for v in violations)


# ---------------------------------------------------------------------------
# _fix_deterministic_violations
# ---------------------------------------------------------------------------


def test_fix_deterministic_violations_success() -> None:
    call_text = lambda prompt, system_prompt="": (  # noqa: E731
        '{"draft": 0}\n---DRAFT---\n# Fixed draft\nClean text.'
    )
    out = sr._fix_deterministic_violations("original draft", ["Em dash found"], call_text)
    assert "Fixed draft" in out


def test_fix_deterministic_violations_empty_response_keeps_original() -> None:
    call_text = lambda prompt, system_prompt="": "no marker text"  # noqa: E731
    assert sr._fix_deterministic_violations("orig", ["v"], call_text) == "orig"


def test_fix_deterministic_violations_unexpected_error_propagates() -> None:
    def boom(prompt, system_prompt=""):
        raise RuntimeError("programming bug")

    with pytest.raises(RuntimeError, match="programming bug"):
        sr._fix_deterministic_violations("orig", ["x"], boom)


def test_fix_deterministic_violations_rate_limit_propagates_unwrapped() -> None:
    def rate_limited(prompt, system_prompt=""):
        raise LLMRateLimitError("rate limited")

    with pytest.raises(LLMRateLimitError):
        sr._fix_deterministic_violations("orig", ["x"], rate_limited)


def test_fix_deterministic_violations_soft_fail_llm_error_keeps_original() -> None:
    def failing(prompt, system_prompt=""):
        raise LLMError("some soft LLM failure")

    assert sr._fix_deterministic_violations("orig", ["x"], failing) == "orig"


# ---------------------------------------------------------------------------
# _llm_self_review
# ---------------------------------------------------------------------------


def test_llm_self_review_no_issues() -> None:
    call_text = lambda prompt, system_prompt="": "[]"  # noqa: E731
    assert sr._llm_self_review("draft text", call_text) == "draft text"


def test_llm_self_review_with_issues_applies_fix() -> None:
    state = {"i": 0}

    def fake(prompt, system_prompt=""):
        state["i"] += 1
        if state["i"] == 1:
            return json.dumps([{"location": "intro", "issue": "vague", "fix": "be specific"}])
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    out = sr._llm_self_review("draft text", fake)
    assert "Better draft" in out


def test_llm_self_review_markdown_fenced_array() -> None:
    state = {"i": 0}

    def fake(prompt, system_prompt=""):
        state["i"] += 1
        if state["i"] == 1:
            issues = json.dumps([{"location": "intro", "issue": "vague", "fix": "be specific"}])
            return f"Here is my review:\n```json\n{issues}\n```"
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    out = sr._llm_self_review("draft text", fake)
    assert "Better draft" in out


def test_llm_self_review_no_array_keeps_original() -> None:
    call_text = lambda prompt, system_prompt="": "just text"  # noqa: E731
    assert sr._llm_self_review("draft text", call_text) == "draft text"


def test_llm_self_review_top_level_object_keeps_original() -> None:
    call_text = lambda prompt, system_prompt="": '{"status": "ok"}'  # noqa: E731
    assert sr._llm_self_review("draft text", call_text) == "draft text"


def test_llm_self_review_rate_limit_propagates_unwrapped() -> None:
    def rate_limited(prompt, system_prompt=""):
        raise LLMTemporaryError("temporary failure")

    with pytest.raises(LLMTemporaryError):
        sr._llm_self_review("draft text", rate_limited)


def test_llm_self_review_soft_fail_llm_error_keeps_original() -> None:
    def failing(prompt, system_prompt=""):
        raise LLMError("some soft LLM failure")

    assert sr._llm_self_review("draft text", failing) == "draft text"


def test_llm_self_review_unexpected_error_propagates() -> None:
    def boom(prompt, system_prompt=""):
        raise RuntimeError("programming bug")

    with pytest.raises(RuntimeError, match="programming bug"):
        sr._llm_self_review("draft text", boom)


def test_llm_self_review_scalar_json_response_keeps_original() -> None:
    """A response that parses to a JSON scalar (not a list/object) is treated
    as "no issues found" via the final ``_extract_json_array_from_text`` fallback."""
    call_text = lambda prompt, system_prompt="": "42"  # noqa: E731
    assert sr._llm_self_review("draft text", call_text) == "draft text"


# ---------------------------------------------------------------------------
# Local helper copies, exercised directly for branches not reached via the
# four self-review functions above.
# ---------------------------------------------------------------------------


def test_unwrap_llm_cause_unwraps_event_loop_exception() -> None:
    from strands.types.exceptions import EventLoopException

    original = ValueError("underlying failure")
    wrapped = EventLoopException(original)
    assert sr._unwrap_llm_cause(wrapped) is original


def test_unwrap_llm_cause_passthrough_for_plain_exception() -> None:
    exc = RuntimeError("plain")
    assert sr._unwrap_llm_cause(exc) is exc


def test_extract_draft_after_marker_rejects_non_string() -> None:
    assert sr._extract_draft_after_marker(None) == ""
    assert sr._extract_draft_after_marker("") == ""


def test_extract_draft_after_marker_json_fallback_without_marker() -> None:
    assert sr._extract_draft_after_marker('{"draft": "Hello world"}') == "Hello world"


def test_extract_json_array_from_text_skips_invalid_bracket_then_matches() -> None:
    text = '[not valid json] then [{"issue": "a", "fix": "b"}]'
    result = sr._extract_json_array_from_text(text, required_keys=("issue",))
    assert result == [{"issue": "a", "fix": "b"}]


def test_extract_json_array_from_text_skips_non_matching_array_then_matches() -> None:
    text = '[1, 2, 3] then [{"issue": "a", "fix": "b"}]'
    result = sr._extract_json_array_from_text(text, required_keys=("issue",))
    assert result == [{"issue": "a", "fix": "b"}]


def test_extract_json_array_from_text_empty_fallback() -> None:
    assert sr._extract_json_array_from_text("[]", required_keys=("issue",)) == []


def test_extract_json_array_from_text_no_bracket_returns_none() -> None:
    assert sr._extract_json_array_from_text("no brackets here", required_keys=("issue",)) is None


def test_looks_like_top_level_json_object_false_when_not_brace() -> None:
    assert sr._looks_like_top_level_json_object("not a brace") is False


def test_looks_like_top_level_json_object_false_on_invalid_json() -> None:
    assert sr._looks_like_top_level_json_object("{not valid json") is False


def test_looks_like_top_level_json_object_true_for_clean_object() -> None:
    assert sr._looks_like_top_level_json_object('{"status": "ok"}') is True


# ---------------------------------------------------------------------------
# _self_review
# ---------------------------------------------------------------------------


def test_self_review_with_violations_calls_fix_then_llm_review() -> None:
    calls: list[str] = []

    def fake(prompt, system_prompt=""):
        calls.append(prompt)
        if len(calls) == 1:
            # deterministic fix call
            return '{"draft": 0}\n---DRAFT---\n# Fixed draft\nYou will enjoy your own results here.'
        # llm self-review call: no issues found
        return "[]"

    draft_with_em_dash = "This has an em dash—right here for the test to catch cleanly."
    out = sr._self_review(draft_with_em_dash, fake)
    assert "Fixed draft" in out
    assert len(calls) == 2


def test_self_review_without_violations_skips_fix_still_reviews() -> None:
    calls: list[str] = []
    clean_draft = (
        "You will find this section walks you through the idea carefully, "
        "connecting each point to something you already understand about "
        "your own workflow, and that is exactly the point of this guide. "
        "You can apply this to your own project whenever you are ready, "
        "and yourself will likely notice the improvement across your team's "
        "workflow within a week or two of consistent practice."
    )
    assert sr._deterministic_self_check(clean_draft) == []

    def fake(prompt, system_prompt=""):
        calls.append(prompt)
        return "[]"

    out = sr._self_review(clean_draft, fake)
    assert out == clean_draft
    assert len(calls) == 1
