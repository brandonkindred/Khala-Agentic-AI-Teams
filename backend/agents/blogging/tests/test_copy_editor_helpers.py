"""Unit tests for the extracted BlogCopyEditorAgent helper methods.

These exercise the private helpers directly — `_build_editor_prompt`,
`_invoke_editor_llm`, `_parse_feedback_items`, and `_inject_length_feedback` —
so the orchestration/`run` refactor keeps its branch behavior locked in.
"""

from __future__ import annotations

import json

import pytest
from agents.blogging.blog_copy_editor_agent import BlogCopyEditorAgent
from agents.blogging.blog_copy_editor_agent.models import CopyEditorInput, FeedbackItem
from agents.blogging.blog_copy_editor_agent.prompts import COPY_EDITOR_PROMPT
from strands.types.exceptions import EventLoopException

from llm_service import DummyLLMClient, LLMRateLimitError, LLMTemporaryError


def _make_agent(style: str = "Style", brand: str = "Brand") -> BlogCopyEditorAgent:
    return BlogCopyEditorAgent(
        llm_client=DummyLLMClient(),
        writing_style_guide_content=style,
        brand_spec_content=brand,
    )


# --------------------------------------------------------------------------- #
# _build_editor_prompt
# --------------------------------------------------------------------------- #


def test_build_prompt_includes_band_and_context_signals() -> None:
    """Soft band, audience, tone, human feedback, and prior feedback all land in the prompt."""
    agent = _make_agent()
    prev = [
        FeedbackItem(
            category="clarity",
            severity="should_fix",
            location="intro",
            issue="Opening is vague",
            suggestion="Lead with the payoff",
        )
    ]
    inp = CopyEditorInput(
        draft="ignored",
        audience="CTOs",
        tone_or_purpose="informative",
        human_feedback="Make it punchier",
        previous_feedback_items=prev,
        target_word_count=1000,
        soft_min_words=800,
        soft_max_words=1200,
        length_guidance="aim for ~1000 words",
    )

    prompt = agent._build_editor_prompt(inp, draft="Real draft body.", style_guide_text="Style")

    assert "soft band ~800–1200 words" in prompt
    assert "CONTENT PROFILE / LENGTH GUIDANCE" in prompt
    assert "Audience: CTOs" in prompt
    assert "Tone/Purpose: informative" in prompt
    assert "**AUTHOR'S REQUESTED CHANGES (must address these):**" in prompt
    assert "Make it punchier" in prompt
    assert "PREVIOUS PASS FEEDBACK" in prompt
    assert "1. [should_fix] clarity [intro]: Opening is vague" in prompt
    # Style-guide branch present, draft appended at the end.
    assert "STYLE GUIDE (evaluate the draft against these rules):" in prompt
    assert prompt.rstrip().endswith("Real draft body.")
    # Base instructions live only in the Agent system prompt — not duplicated here.
    assert COPY_EDITOR_PROMPT not in prompt


def test_build_prompt_no_band_uses_plain_target_line() -> None:
    """Without a soft band, the plain target-word-count line is used."""
    agent = _make_agent()
    inp = CopyEditorInput(draft="ignored", target_word_count=750)

    prompt = agent._build_editor_prompt(inp, draft="Body.", style_guide_text="Style")

    assert "Target word count: 750 words (draft is currently 1 words)." in prompt
    assert "soft band" not in prompt


def test_build_prompt_no_style_guide_branch() -> None:
    """Empty style guide yields the 'No style guidelines were provided' instruction."""
    agent = _make_agent(style="", brand="")
    inp = CopyEditorInput(draft="ignored")

    prompt = agent._build_editor_prompt(inp, draft="Body.", style_guide_text="")

    assert "No style guidelines were provided." in prompt
    assert "STYLE GUIDE (evaluate the draft against these rules):" not in prompt


def test_build_prompt_content_plan_is_included_in_full() -> None:
    """A content plan appears in the prompt without truncation."""
    agent = _make_agent()
    plan = "P" * 13000
    inp = CopyEditorInput(draft="ignored", content_plan_context=plan)

    prompt = agent._build_editor_prompt(inp, draft="Body.", style_guide_text="Style")

    assert "CONTENT PLAN (align feedback with this structure and section intent):" in prompt
    assert plan in prompt


# --------------------------------------------------------------------------- #
# _parse_feedback_items
# --------------------------------------------------------------------------- #


def test_parse_feedback_items_applies_defaults_and_filters() -> None:
    """Non-dicts and empty-issue entries are dropped; missing fields get defaults."""
    agent = _make_agent()
    raw = [
        {"issue": "Tighten this"},  # defaults: category=style, severity=consider
        {
            "category": "voice",
            "severity": "must_fix",
            "location": "p2",
            "issue": "Off tone",
            "suggestion": "Rephrase",
        },
        {"category": "x", "severity": "x", "issue": ""},  # dropped (empty issue)
        "not-a-dict",  # dropped
    ]

    items = agent._parse_feedback_items(raw)

    assert len(items) == 2
    assert items[0].category == "style"
    assert items[0].severity == "consider"
    assert items[0].location is None
    assert items[0].suggestion is None
    assert items[1].category == "voice"
    assert items[1].location == "p2"
    assert items[1].suggestion == "Rephrase"


def test_parse_feedback_items_empty_input() -> None:
    """An empty iterable yields an empty list."""
    agent = _make_agent()
    assert agent._parse_feedback_items([]) == []


# --------------------------------------------------------------------------- #
# _inject_length_feedback
# --------------------------------------------------------------------------- #


def test_inject_length_must_fix_prepended() -> None:
    """Past the soft ceiling and over the must ratio → must_fix inserted at the front."""
    agent = _make_agent()
    inp = CopyEditorInput(
        draft="ignored",
        target_word_count=1000,
        soft_max_words=1300,
        editor_must_fix_over_ratio=1.4,
    )
    existing = [FeedbackItem(category="voice", severity="consider", issue="minor")]

    out = agent._inject_length_feedback(existing, inp, actual_word_count=1500)

    assert out is existing  # mutated in place and returned
    assert out[0].category == "structure"
    assert out[0].severity == "must_fix"
    assert out[0].location == "entire draft"


def test_inject_length_should_fix_appended() -> None:
    """Over the should ratio but under the must ratio → should_fix appended at the end."""
    agent = _make_agent()
    inp = CopyEditorInput(
        draft="ignored",
        target_word_count=1000,
        soft_max_words=1100,
        editor_must_fix_over_ratio=1.5,
        editor_should_fix_over_ratio=1.15,
    )
    existing = [FeedbackItem(category="voice", severity="consider", issue="minor")]

    out = agent._inject_length_feedback(existing, inp, actual_word_count=1200)

    assert out[-1].category == "structure"
    assert out[-1].severity == "should_fix"


def test_inject_length_within_band_no_injection() -> None:
    """At or below the soft ceiling → no structure/length item added."""
    agent = _make_agent()
    inp = CopyEditorInput(draft="ignored", target_word_count=1000, soft_max_words=1300)

    out = agent._inject_length_feedback([], inp, actual_word_count=1100)

    assert out == []


def test_inject_length_technical_deep_dive_thin_draft() -> None:
    """A thin technical deep dive gets a 'consider' under-length hint."""
    agent = _make_agent()
    inp = CopyEditorInput(
        draft="ignored",
        target_word_count=2000,
        soft_min_words=1500,
        soft_max_words=2500,
        content_profile="technical_deep_dive",
    )

    out = agent._inject_length_feedback([], inp, actual_word_count=100)

    assert any(f.severity == "consider" and f.category == "structure" for f in out)


def test_inject_length_skips_when_target_word_count_non_positive() -> None:
    """Non-positive target is a no-op — do not invent over-length feedback.

    The old fallback set over_ratio=1.0 when target was 0. With a must_fix
    ratio below 1.0 (validation bypassed), that falsely injects must_fix.
    """
    agent = _make_agent()
    # Bypass Field bounds so we can exercise the defensive path.
    inp = CopyEditorInput.model_construct(
        draft="ignored",
        target_word_count=0,
        soft_max_words=None,
        editor_must_fix_over_ratio=0.5,
        editor_should_fix_over_ratio=0.5,
        soft_min_words=None,
        content_profile=None,
    )
    existing = [FeedbackItem(category="voice", severity="consider", issue="minor")]

    out = agent._inject_length_feedback(existing, inp, actual_word_count=500)

    assert out is existing
    assert len(out) == 1
    assert out[0].category == "voice"


# --------------------------------------------------------------------------- #
# _invoke_editor_llm
# --------------------------------------------------------------------------- #


def _patch_agent(monkeypatch, side_effect) -> dict:
    """Replace the module-level Agent with a stub whose __call__ runs `side_effect`.

    Returns a dict that captures the Agent constructor kwargs so tests can assert
    what was passed (e.g. the system prompt).
    """
    from agents.blogging.shared import json_retry as json_retry_mod

    captured: dict = {}

    class _Agent:
        def __init__(self, *a, **kw):
            captured.update(kw)

        def __call__(self, prompt):
            return side_effect(prompt)

    monkeypatch.setattr(json_retry_mod, "Agent", _Agent)
    return captured


def test_invoke_llm_success_and_calls_progress_hook(monkeypatch) -> None:
    """A valid JSON response is parsed and the on_llm_request hook fires once."""
    agent = _make_agent()
    _patch_agent(
        monkeypatch, lambda p: json.dumps({"approved": True, "summary": "ok", "feedback_items": []})
    )
    calls: list[str] = []

    data = agent._invoke_editor_llm("prompt", on_llm_request=calls.append)

    assert data["summary"] == "ok"
    assert calls == ["Reviewing draft for style and clarity..."]


def test_invoke_llm_delivers_base_instructions_as_system_prompt(monkeypatch) -> None:
    """The base instructions reach the model once, via the Agent's system prompt."""
    agent = _make_agent()
    captured = _patch_agent(
        monkeypatch, lambda p: json.dumps({"summary": "ok", "feedback_items": []})
    )

    agent._invoke_editor_llm("just the context")

    assert captured["system_prompt"] == COPY_EDITOR_PROMPT


def test_invoke_llm_strict_retry_then_success(monkeypatch) -> None:
    """First response is unparseable; the strict-instruction retry then parses."""
    agent = _make_agent()
    seen: list[str] = []

    def side_effect(prompt: str) -> str:
        seen.append(prompt)
        if len(seen) == 1:
            return "not json"
        return json.dumps({"summary": "recovered", "feedback_items": []})

    _patch_agent(monkeypatch, side_effect)

    data = agent._invoke_editor_llm("base")

    assert data["summary"] == "recovered"
    # The retry prompt carries the strict-JSON suffix.
    assert "single JSON object only" in seen[1]


def test_invoke_llm_json_parse_exhausted_returns_fallback(monkeypatch) -> None:
    """Both JSON attempts fail → advisory fallback dict (approved, manual review)."""
    agent = _make_agent()
    _patch_agent(monkeypatch, lambda p: "still not json")

    data = agent._invoke_editor_llm("base")

    assert "could not parse" in data["summary"].lower()
    assert data["approved"] is True
    assert data["feedback_items"] == []


def test_invoke_llm_raw_transient_error_reraises(monkeypatch) -> None:
    """A transient LLM error propagates (delegated to Temporal), not degraded to fallback."""
    agent = _make_agent()
    calls = {"n": 0}

    def side_effect(prompt: str) -> str:
        calls["n"] += 1
        raise LLMTemporaryError("transient outage")

    _patch_agent(monkeypatch, side_effect)

    with pytest.raises(LLMTemporaryError, match="transient outage"):
        agent._invoke_editor_llm("base")
    assert calls["n"] == 1  # no blocking retry here


def test_invoke_llm_wrapped_transient_error_reraises_unwrapped(monkeypatch) -> None:
    """A transient error wrapped in EventLoopException re-raises as the UNWRAPPED cause.

    The Temporal stage funnel catches only LLMRateLimitError/LLMTemporaryError to trigger
    a retry, so re-raising the EventLoopException wrapper would land in its terminal-failure
    handler instead. The unwrapped cause must propagate.
    """
    agent = _make_agent()
    wrapped = LLMRateLimitError("429 after client retries")

    def side_effect(prompt: str) -> str:
        raise EventLoopException(wrapped)

    _patch_agent(monkeypatch, side_effect)

    with pytest.raises(LLMRateLimitError) as excinfo:
        agent._invoke_editor_llm("base")
    # The exact unwrapped instance propagates, not the EventLoopException wrapper.
    assert excinfo.value is wrapped
    assert not isinstance(excinfo.value, EventLoopException)


def test_invoke_llm_non_transient_error_degrades_to_fallback(monkeypatch) -> None:
    """A programming bug is not transient — it fails closed to the manual-review fallback."""
    agent = _make_agent()
    calls = {"n": 0}

    def side_effect(prompt: str) -> str:
        calls["n"] += 1
        raise AttributeError("bug: 'NoneType' has no attribute 'x'")

    _patch_agent(monkeypatch, side_effect)

    data = agent._invoke_editor_llm("base")

    assert "manually" in data["summary"].lower()
    assert data["approved"] is True
    assert data["feedback_items"] == []
    assert calls["n"] == 1  # no retry


def test_invoke_llm_wrapped_non_transient_error_degrades_to_fallback(monkeypatch) -> None:
    """A bug wrapped in EventLoopException is unwrapped, judged non-transient, and degraded."""
    agent = _make_agent()

    def side_effect(prompt: str) -> str:
        raise EventLoopException(TypeError("bug: unsupported operand"))

    _patch_agent(monkeypatch, side_effect)

    data = agent._invoke_editor_llm("base")

    assert "manually" in data["summary"].lower()
    assert data["approved"] is True


def test_invoke_llm_empty_json_object_returns_fallback(monkeypatch) -> None:
    """A parseable but empty ({}) response is normalized to the advisory fallback."""
    agent = _make_agent()
    _patch_agent(monkeypatch, lambda p: "{}")

    data = agent._invoke_editor_llm("base")

    assert data["approved"] is True
    assert "manually" in data["summary"].lower()
    assert data["feedback_items"] == []


# --------------------------------------------------------------------------- #
# _write_feedback_to_path (failure path via run)
# --------------------------------------------------------------------------- #


def test_write_feedback_failure_is_swallowed(tmp_path) -> None:
    """A write failure (path is a directory) is logged, not raised, and run() still returns."""
    agent = _make_agent()
    # Pointing the output at an existing directory makes write_text fail; the
    # helper must swallow the error so the review result is still returned.
    result = agent.run(
        CopyEditorInput(draft="# Draft\n\nBody text."), feedback_output_path=str(tmp_path)
    )

    assert result.summary
    assert tmp_path.is_dir()
