"""Unit tests for the extracted BlogCopyEditorAgent helper methods.

These exercise the private helpers directly — `_build_editor_prompt`,
`_invoke_editor_llm`, `_parse_feedback_items`, and `_inject_length_feedback` —
so the orchestration/`run` refactor keeps its branch behavior locked in.
"""

from __future__ import annotations

import json

import pytest
from blog_copy_editor_agent import BlogCopyEditorAgent
from blog_copy_editor_agent.models import CopyEditorInput, FeedbackItem

from llm_service import DummyLLMClient, LLMJsonParseError


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


def test_build_prompt_content_plan_is_included_and_truncated() -> None:
    """A content plan appears in the prompt and is truncated to 12000 chars."""
    agent = _make_agent()
    plan = "P" * 13000
    inp = CopyEditorInput(draft="ignored", content_plan_context=plan)

    prompt = agent._build_editor_prompt(inp, draft="Body.", style_guide_text="Style")

    assert "CONTENT PLAN (align feedback with this structure and section intent):" in prompt
    assert "P" * 12000 in prompt
    assert "P" * 12001 not in prompt


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


# --------------------------------------------------------------------------- #
# _invoke_editor_llm
# --------------------------------------------------------------------------- #


def _patch_agent(monkeypatch, side_effect) -> None:
    """Replace the module-level Agent with a stub whose __call__ runs `side_effect`."""
    from blog_copy_editor_agent import agent as ce_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return side_effect(prompt)

    monkeypatch.setattr(ce_mod, "Agent", _Agent)


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
    """Both JSON attempts fail → deterministic fallback dict."""
    agent = _make_agent()
    _patch_agent(monkeypatch, lambda p: "still not json")

    data = agent._invoke_editor_llm("base")

    assert "could not parse" in data["summary"].lower()
    assert data["feedback_items"] == []


def test_invoke_llm_transport_error_then_success(monkeypatch) -> None:
    """A transport error backs off (sleep patched) and the next round succeeds."""
    from blog_copy_editor_agent import agent as ce_mod

    monkeypatch.setattr(ce_mod.time, "sleep", lambda s: None)
    agent = _make_agent()
    calls = {"n": 0}

    def side_effect(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection reset")
        return json.dumps({"summary": "after retry", "feedback_items": []})

    _patch_agent(monkeypatch, side_effect)

    data = agent._invoke_editor_llm("base")

    assert data["summary"] == "after retry"
    assert calls["n"] == 2


def test_invoke_llm_transport_error_exhausts_and_raises(monkeypatch) -> None:
    """Persistent transport errors propagate once the retry budget is spent."""
    from blog_copy_editor_agent import agent as ce_mod

    monkeypatch.setattr(ce_mod.time, "sleep", lambda s: None)
    agent = _make_agent()

    def side_effect(prompt: str) -> str:
        raise RuntimeError("still down")

    _patch_agent(monkeypatch, side_effect)

    with pytest.raises(RuntimeError, match="still down"):
        agent._invoke_editor_llm("base")


def test_invoke_llm_empty_json_object_uses_final_fallback(monkeypatch) -> None:
    """A parseable but empty ({}) response falls through to the final fallback dict."""
    agent = _make_agent()
    _patch_agent(monkeypatch, lambda p: "{}")

    data = agent._invoke_editor_llm("base")

    assert "could not parse" in data["summary"].lower()
    assert data["feedback_items"] == []


def test_invoke_llm_first_attempt_json_error_is_importable() -> None:
    """Guard: LLMJsonParseError is the exception type the retry loop catches."""
    assert issubclass(LLMJsonParseError, Exception)


# --------------------------------------------------------------------------- #
# _write_feedback_to_path (failure path)
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
