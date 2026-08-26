"""Tests for ``blog_writer_agent.self_review``, exercised in isolation.

These tests call the module's free functions directly with a plain
``call_text`` stand-in callable — no ``BlogWriterAgent`` instantiation — to
confirm the module has no hidden dependency on the agent class. Coverage
mirrors (without duplicating every regression case from) the agent-level
self-review tests in ``test_writer_self_review_and_revise.py``.
"""

from __future__ import annotations

import json
import logging

import pytest
from agents.blogging.blog_writer_agent import self_review as sr

# ---------------------------------------------------------------------------
# deterministic_self_check
# ---------------------------------------------------------------------------


def test_deterministic_self_check_clean_draft_no_violations() -> None:
    """A draft satisfying every rule reports no violations."""
    draft = (
        "# Heading\n\n"
        "You will learn how your project benefits when you apply this pattern "
        "consistently across your codebase, because it keeps things predictable "
        "and easy to reason about over time.\n"
    )
    assert sr.deterministic_self_check(draft) == []


def test_deterministic_self_check_em_dash() -> None:
    """An em dash or en dash in a paragraph is flagged."""
    violations = sr.deterministic_self_check("This has an em dash—right there.")
    assert any("Em/en dash" in v for v in violations)


def test_deterministic_self_check_banned_phrase() -> None:
    """A banned phrase is flagged by name."""
    violations = sr.deterministic_self_check("In today's fast-paced world, things change.")
    assert any("In today's fast-paced world" in v for v in violations)


def test_deterministic_self_check_vague_citation_without_source() -> None:
    """A vague citation with no nearby link/source marker is flagged."""
    violations = sr.deterministic_self_check("Studies show that this approach works well.")
    assert any("Vague citation" in v for v in violations)


def test_deterministic_self_check_vague_citation_with_source_not_flagged() -> None:
    """A vague citation followed shortly by a link is not flagged."""
    violations = sr.deterministic_self_check(
        "Studies show this works. See https://example.com/research for details."
    )
    assert not any("Vague citation" in v for v in violations)


def test_deterministic_self_check_low_reader_address_count() -> None:
    """Fewer than MIN_READER_ADDRESS_COUNT reader-address words is flagged."""
    violations = sr.deterministic_self_check("This is about the topic in general terms.")
    assert any("Reader address" in v for v in violations)


def test_deterministic_self_check_staccato_prose() -> None:
    """Three or more consecutive short sentences in a paragraph are flagged."""
    draft = "This is short. So is this. Also short. Yet more."
    violations = sr.deterministic_self_check(draft)
    assert any("Staccato prose" in v for v in violations)


def test_deterministic_self_check_rejects_non_string() -> None:
    """Non-string input raises TypeError."""
    with pytest.raises(TypeError, match="draft must be a string"):
        sr.deterministic_self_check(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# fix_deterministic_violations
# ---------------------------------------------------------------------------


def test_fix_deterministic_violations_applies_fix() -> None:
    """A clean LLM response with a draft marker applies the fixed draft."""

    def call_text(prompt: str, system_prompt: str = "") -> str:
        return '{"draft": 0}\n---DRAFT---\n# Fixed draft\nClean text.'

    out = sr.fix_deterministic_violations("original draft", ["Em dash found"], call_text)
    assert "Fixed draft" in out


def test_fix_deterministic_violations_no_marker_keeps_original() -> None:
    """If the LLM returns nothing extractable, the original draft is kept."""

    def call_text(prompt: str, system_prompt: str = "") -> str:
        return "no marker text"

    assert sr.fix_deterministic_violations("orig", ["v"], call_text) == "orig"


def test_fix_deterministic_violations_soft_fails_permanent_error(caplog) -> None:
    """Non-transient LLM errors are soft-failed: original draft returned, error logged."""
    from llm_service import LLMPermanentError

    def call_text(prompt: str, system_prompt: str = "") -> str:
        raise LLMPermanentError("permanent")

    with caplog.at_level(logging.ERROR):
        out = sr.fix_deterministic_violations("orig", ["x"], call_text)
    assert out == "orig"
    assert any("Deterministic fix LLM call failed" in r.message for r in caplog.records)
    assert any(r.exc_info is not None for r in caplog.records)


def test_fix_deterministic_violations_rate_limit_reraises() -> None:
    """LLMRateLimitError propagates unwrapped so the retry funnel can catch it."""
    from llm_service import LLMRateLimitError

    def call_text(prompt: str, system_prompt: str = "") -> str:
        raise LLMRateLimitError("rate limited")

    with pytest.raises(LLMRateLimitError, match="rate limited"):
        sr.fix_deterministic_violations("orig", ["x"], call_text)


def test_fix_deterministic_violations_temporary_reraises() -> None:
    """LLMTemporaryError propagates unwrapped so the retry funnel can catch it."""
    from llm_service import LLMTemporaryError

    def call_text(prompt: str, system_prompt: str = "") -> str:
        raise LLMTemporaryError("temporary")

    with pytest.raises(LLMTemporaryError, match="temporary"):
        sr.fix_deterministic_violations("orig", ["x"], call_text)


def test_fix_deterministic_violations_unwraps_wrapped_rate_limit() -> None:
    """A rate-limit error wrapped in EventLoopException is unwrapped before re-raising."""
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMRateLimitError

    wrapped = LLMRateLimitError("rate limited")

    def call_text(prompt: str, system_prompt: str = "") -> str:
        raise EventLoopException(wrapped)

    with pytest.raises(LLMRateLimitError) as excinfo:
        sr.fix_deterministic_violations("orig", ["x"], call_text)
    assert excinfo.value is wrapped
    assert not isinstance(excinfo.value, EventLoopException)


def test_fix_deterministic_violations_unexpected_error_propagates() -> None:
    """An unexpected programming error (not an LLM error) propagates unhandled."""

    def call_text(prompt: str, system_prompt: str = "") -> str:
        raise RuntimeError("programming bug")

    with pytest.raises(RuntimeError, match="programming bug"):
        sr.fix_deterministic_violations("orig", ["x"], call_text)


# ---------------------------------------------------------------------------
# llm_self_review
# ---------------------------------------------------------------------------


def test_llm_self_review_no_issues() -> None:
    """An empty JSON array review response returns the draft unchanged."""

    def call_text(prompt: str, system_prompt: str = "") -> str:
        return "[]"

    assert sr.llm_self_review("draft text", call_text) == "draft text"


def test_llm_self_review_with_issues_applies_fix() -> None:
    """When review returns issues, a second call applies the fix."""
    state = {"i": 0}

    def call_text(prompt: str, system_prompt: str = "") -> str:
        state["i"] += 1
        if state["i"] == 1:
            return json.dumps([{"location": "intro", "issue": "vague", "fix": "be specific"}])
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    out = sr.llm_self_review("draft text", call_text)
    assert "Better draft" in out
    assert state["i"] == 2


def test_llm_self_review_fenced_array_applies_fix() -> None:
    """Issues array wrapped in markdown fences is still extracted correctly."""
    state = {"i": 0}

    def call_text(prompt: str, system_prompt: str = "") -> str:
        state["i"] += 1
        if state["i"] == 1:
            issues = json.dumps([{"location": "intro", "issue": "vague", "fix": "be specific"}])
            return f"Here is my review:\n```json\n{issues}\n```"
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    out = sr.llm_self_review("draft text", call_text)
    assert "Better draft" in out


def test_llm_self_review_prose_prefixed_array_applies_fix() -> None:
    """Unfenced prose before a valid issues array still applies fixes."""
    state = {"i": 0}
    review_payload = (
        'Here are the issues: [{"location": "intro", "issue": "vague", "fix": "be specific"}]'
    )

    def call_text(prompt: str, system_prompt: str = "") -> str:
        state["i"] += 1
        if state["i"] == 1:
            return review_payload
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    out = sr.llm_self_review("draft text", call_text)
    assert "Better draft" in out


def test_llm_self_review_prose_fallback_all_empty_issues_returns_draft_no_second_call() -> None:
    """A prose-rescanned array whose only element has a falsy ``issue`` is dropped.

    Regression test: the prose-rescan fallback (``_extract_json_array_from_text``)
    only requires the ``issue`` key to be *present*, not truthy — so it can return
    a dict like ``{"issue": ""}``. Without post-filtering, that would produce a
    blank instruction line and an unnecessary second LLM call that could rewrite
    an otherwise-unchanged draft.
    """
    calls = {"n": 0}
    review_payload = 'Here are the issues: [{"location": "intro", "issue": ""}]'

    def call_text(prompt: str, system_prompt: str = "") -> str:
        calls["n"] += 1
        return review_payload

    out = sr.llm_self_review("draft text", call_text)
    assert out == "draft text"
    assert calls["n"] == 1


def test_llm_self_review_prose_fallback_drops_empty_issue_keeps_valid_one() -> None:
    """A prose-rescanned array mixing a falsy-``issue`` entry and a real one keeps only the real one."""
    state = {"i": 0}
    review_payload = (
        'Here are the issues: [{"location": "intro", "issue": ""}, '
        '{"location": "body", "issue": "vague", "fix": "be specific"}]'
    )

    fix_prompts = []

    def call_text(prompt: str, system_prompt: str = "") -> str:
        state["i"] += 1
        if state["i"] == 1:
            return review_payload
        fix_prompts.append(prompt)
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    out = sr.llm_self_review("draft text", call_text)
    assert "Better draft" in out
    assert state["i"] == 2
    # Only the real issue made it into the fix prompt, numbered starting at 1
    # (the blank-issue entry was filtered out rather than occupying slot 1).
    assert "1. [body] vague" in fix_prompts[0]
    assert "2." not in fix_prompts[0].split("---\nCURRENT DRAFT")[0]


def test_llm_self_review_non_list_json_returns_draft() -> None:
    """A JSON object (not an array) is treated as no issues."""
    calls = {"n": 0}

    def call_text(prompt: str, system_prompt: str = "") -> str:
        calls["n"] += 1
        return '{"status": "ok"}'

    out = sr.llm_self_review("draft text", call_text)
    assert out == "draft text"
    assert calls["n"] == 1


def test_llm_self_review_malformed_json_returns_draft(caplog) -> None:
    """Malformed JSON that cannot be parsed as a list is treated as no issues."""

    def call_text(prompt: str, system_prompt: str = "") -> str:
        return "[not-valid-json"

    with caplog.at_level(logging.INFO):
        out = sr.llm_self_review("orig", call_text)
    assert out == "orig"
    assert any("response was not a JSON array" in r.message for r in caplog.records)


def test_llm_self_review_direct_list_drops_entries_without_issue_key() -> None:
    """A direct-parsed list keeps only elements with an ``issue`` key."""
    state = {"i": 0}
    review_payload = (
        '[{"title": "unrelated"}, {"location": "intro", "issue": "vague", "fix": "be specific"}]'
    )

    def call_text(prompt: str, system_prompt: str = "") -> str:
        state["i"] += 1
        if state["i"] == 1:
            return review_payload
        return '{"draft": 0}\n---DRAFT---\n# Better draft\nSpecific text.'

    out = sr.llm_self_review("draft text", call_text)
    assert "Better draft" in out


def test_llm_self_review_soft_fails_permanent_error(caplog) -> None:
    """Non-transient LLM errors during self-review are soft-failed and logged."""
    from llm_service import LLMPermanentError

    def call_text(prompt: str, system_prompt: str = "") -> str:
        raise LLMPermanentError("permanent")

    with caplog.at_level(logging.ERROR):
        out = sr.llm_self_review("orig", call_text)
    assert out == "orig"
    assert any("LLM self-review failed" in r.message for r in caplog.records)
    assert any(r.exc_info is not None for r in caplog.records)


def test_llm_self_review_rate_limit_reraises() -> None:
    """LLMRateLimitError during self-review propagates unwrapped."""
    from llm_service import LLMRateLimitError

    def call_text(prompt: str, system_prompt: str = "") -> str:
        raise LLMRateLimitError("rate limited")

    with pytest.raises(LLMRateLimitError, match="rate limited"):
        sr.llm_self_review("orig", call_text)


def test_llm_self_review_temporary_reraises() -> None:
    """LLMTemporaryError during self-review propagates unwrapped."""
    from llm_service import LLMTemporaryError

    def call_text(prompt: str, system_prompt: str = "") -> str:
        raise LLMTemporaryError("temporary")

    with pytest.raises(LLMTemporaryError, match="temporary"):
        sr.llm_self_review("orig", call_text)


def test_llm_self_review_unwraps_wrapped_permanent_error(caplog) -> None:
    """A permanent error wrapped in EventLoopException is unwrapped before soft-failing."""
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMPermanentError

    def call_text(prompt: str, system_prompt: str = "") -> str:
        raise EventLoopException(LLMPermanentError("permanent"))

    with caplog.at_level(logging.ERROR):
        out = sr.llm_self_review("orig", call_text)
    assert out == "orig"
    assert any("LLM self-review failed" in r.message for r in caplog.records)


def test_llm_self_review_unexpected_error_propagates() -> None:
    """An unexpected programming error (not an LLM error) propagates unhandled."""

    def call_text(prompt: str, system_prompt: str = "") -> str:
        raise RuntimeError("programming bug")

    with pytest.raises(RuntimeError, match="programming bug"):
        sr.llm_self_review("orig", call_text)


# ---------------------------------------------------------------------------
# self_review
# ---------------------------------------------------------------------------


def test_self_review_combines_both_steps() -> None:
    """self_review runs both the deterministic pass and the LLM pass."""
    calls = []

    def call_text(prompt: str, system_prompt: str = "") -> str:
        calls.append(prompt)
        return '{"draft": 0}\n---DRAFT---\n# Result\nGood text.'

    # Force at least one deterministic violation (em dash + banned phrase).
    draft = "In today's fast-paced world—Studies show."
    out = sr.self_review(draft, call_text)
    assert "—" not in out
    assert "Good text" in out
    # Two calls: one to fix deterministic violations, one for the LLM review pass.
    assert len(calls) == 2


def test_self_review_skips_fix_when_no_violations() -> None:
    """When deterministic_self_check finds nothing, only the LLM review call runs."""
    calls = {"n": 0}

    def call_text(prompt: str, system_prompt: str = "") -> str:
        calls["n"] += 1
        return "[]"

    draft = (
        "You will find that your reading of this passage benefits from your patience, "
        "since you can revisit any part of it whenever you like."
    )
    assert sr.deterministic_self_check(draft) == []
    out = sr.self_review(draft, call_text)
    assert out == draft
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# _extract_json_array_from_text (internal helper, direct coverage)
# ---------------------------------------------------------------------------


def test_extract_json_array_from_text_does_not_salvage_from_inside_decoded_value() -> None:
    """Regression: after a successful decode of a non-matching array, the scanner
    must not re-enter that already-decoded span looking for a nested match. The
    outer array's own top-level elements are a list and a string (neither a
    dict), so it correctly does not match required_keys — but its first element
    is itself an array containing a dict with a truthy "issue" key. A real match
    must not be salvaged from inside an already-rejected value.
    """
    text = '[[{"issue": "wrongly-salvaged", "fix": "z"}], "sibling"]'
    assert sr._extract_json_array_from_text(text, required_keys=("issue",)) is None


def test_extract_json_array_from_text_resumes_after_decoded_value_not_inside_it() -> None:
    """The scanner resumes past a decoded (non-matching) value's end, not from
    just after its opening bracket — so it finds the real match that follows
    a non-matching value, rather than a nested array salvaged from inside it.
    """
    text = (
        '[[{"issue": "wrongly-salvaged", "fix": "z"}], "sibling"] '
        'then [{"issue": "real", "fix": "y"}]'
    )
    result = sr._extract_json_array_from_text(text, required_keys=("issue",))
    assert result == [{"issue": "real", "fix": "y"}]


def test_banned_phrase_pattern_loop_variables_do_not_leak_into_module_namespace() -> None:
    """The ``for _phrase in BANNED_PHRASES`` pattern-compilation loop must not
    leave its loop variables as module attributes after import."""
    assert not hasattr(sr, "_phrase")
    assert not hasattr(sr, "_escaped")
    assert len(sr._BANNED_PHRASE_PATTERNS) == len(sr.BANNED_PHRASES)
