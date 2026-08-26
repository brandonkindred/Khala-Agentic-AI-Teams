"""Tests for the blog copy editor agent."""

import json
from pathlib import Path

import pytest
from agents.blogging.blog_copy_editor_agent import (
    BlogCopyEditorAgent,
    CopyEditorInput,
    CopyEditorOutput,
    FeedbackItem,
)

from llm_service import DummyLLMClient

# Inline style guide passed at agent init so tests do not load the default file.
_TEST_STYLE_GUIDE = "Clear, conversational prose at ~8th grade. No em dashes."


def _draft_n_words(n: int) -> str:
    """Whitespace-separated tokens so len(draft.split()) == n."""
    return " ".join(["word"] * n)


def _structure_length_items(feedback_items: list) -> list:
    return [
        item
        for item in feedback_items
        if item.category == "structure"
        and item.location == "entire draft"
        and item.issue
        and "words" in item.issue.lower()
    ]


def test_length_injection_skips_when_within_soft_ceiling() -> None:
    """1134 words vs ~1000 target must not inject should_fix when soft_max allows it (e.g. 1300)."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
        brand_spec_content="",
    )
    result = agent.run(
        CopyEditorInput(
            draft=_draft_n_words(1134),
            target_word_count=1000,
            soft_min_words=750,
            soft_max_words=1300,
        )
    )
    assert _structure_length_items(result.feedback_items) == []


def test_length_injection_must_fix_past_soft_ceiling() -> None:
    """Above soft_max, ratio vs target still triggers programmatic length feedback."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
        brand_spec_content="",
    )
    result = agent.run(
        CopyEditorInput(
            draft=_draft_n_words(1400),
            target_word_count=1000,
            soft_min_words=750,
            soft_max_words=1300,
        )
    )
    length_items = _structure_length_items(result.feedback_items)
    assert len(length_items) >= 1
    assert length_items[0].severity == "must_fix"


def test_length_injection_soft_max_none_uses_ratio_only() -> None:
    """Without soft_max, modest overrun vs target still gets should_fix (legacy behavior)."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
        brand_spec_content="",
    )
    result = agent.run(
        CopyEditorInput(
            draft=_draft_n_words(1111),
            target_word_count=1000,
            soft_min_words=None,
            soft_max_words=None,
        )
    )
    length_items = _structure_length_items(result.feedback_items)
    assert len(length_items) == 1
    assert length_items[0].severity == "should_fix"


def test_blog_copy_editor_agent_run() -> None:
    """BlogCopyEditorAgent returns summary and feedback_items."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
        brand_spec_content="",
    )

    copy_editor_input = CopyEditorInput(
        draft="# Test Post\n\nThis is a draft with an em dash—here.",
        audience="CTOs",
        tone_or_purpose="technical",
    )

    result = agent.run(copy_editor_input)

    assert isinstance(result, CopyEditorOutput)
    assert result.summary
    assert isinstance(result.feedback_items, list)
    if result.feedback_items:
        item = result.feedback_items[0]
        assert item.category
        assert item.severity in ("must_fix", "should_fix", "consider")
        assert item.issue


def test_blog_copy_editor_agent_empty_draft() -> None:
    """BlogCopyEditorAgent returns minimal feedback for empty draft."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm, writing_style_guide_content="", brand_spec_content=""
    )

    result = agent.run(CopyEditorInput(draft=""))

    assert result.summary
    assert len(result.feedback_items) == 0


def test_blog_copy_editor_agent_writes_feedback_file(tmp_path: Path) -> None:
    """When feedback_output_path is set, run() writes the output to that file."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
        brand_spec_content="",
    )
    feedback_file = tmp_path / "editor_feedback.json"

    result = agent.run(
        CopyEditorInput(draft="# Test\n\nShort draft."),
        feedback_output_path=str(feedback_file),
    )

    assert feedback_file.exists()
    content = json.loads(feedback_file.read_text(encoding="utf-8"))
    assert "summary" in content
    assert "feedback_items" in content
    assert isinstance(content["feedback_items"], list)
    assert content["summary"] == result.summary
    assert len(content["feedback_items"]) == len(result.feedback_items)
    assert result.feedback_file_written is True
    # Set after serialization, so it never appears in the file the agent just wrote.
    assert "feedback_file_written" not in content


def test_blog_copy_editor_agent_feedback_file_roundtrip(tmp_path: Path) -> None:
    """Written JSON matches the returned CopyEditorOutput."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
        brand_spec_content="",
    )
    feedback_file = tmp_path / "editor_feedback.json"

    result = agent.run(
        CopyEditorInput(draft="# Test\n\nDraft with content."),
        feedback_output_path=str(feedback_file),
    )

    data = result.model_dump()
    written = json.loads(feedback_file.read_text(encoding="utf-8"))
    assert written["summary"] == data["summary"]
    assert len(written["feedback_items"]) == len(data["feedback_items"])


def test_blog_copy_editor_agent_no_path_no_file(monkeypatch) -> None:
    """When feedback_output_path is not passed, the agent never attempts to write a feedback file."""
    from agents.blogging.blog_copy_editor_agent import agent as ce_mod

    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
        brand_spec_content="",
    )

    write_calls = []
    monkeypatch.setattr(
        ce_mod.BlogCopyEditorAgent,
        "_write_feedback_to_path",
        lambda self, *a, **k: write_calls.append((a, k)),
    )

    result = agent.run(CopyEditorInput(draft="# Test\n\nDraft."))

    assert result.summary is not None
    assert write_calls == []
    assert result.feedback_file_written is None


def test_blog_copy_editor_agent_empty_draft_writes_file(tmp_path: Path) -> None:
    """Empty draft with feedback_output_path set still writes a file with summary and empty feedback_items."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm, writing_style_guide_content="", brand_spec_content=""
    )
    feedback_file = tmp_path / "empty_feedback.json"

    result = agent.run(CopyEditorInput(draft=""), feedback_output_path=str(feedback_file))

    assert feedback_file.exists()
    content = json.loads(feedback_file.read_text(encoding="utf-8"))
    assert content["summary"]
    assert content["feedback_items"] == []
    assert result.summary
    assert len(result.feedback_items) == 0
    assert result.feedback_file_written is True


def test_blog_copy_editor_agent_reports_write_failure(tmp_path: Path) -> None:
    """run() reports a failed feedback-file write via feedback_file_written=False, without raising."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
        brand_spec_content="",
    )
    # A regular file cannot double as a parent directory, so mkdir(parents=True) fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    feedback_file = blocker / "editor_feedback.json"

    result = agent.run(
        CopyEditorInput(draft="# Test\n\nShort draft."),
        feedback_output_path=str(feedback_file),
    )

    assert result.feedback_file_written is False
    assert not feedback_file.exists()


@pytest.mark.parametrize("kind", ["rate_limit", "temporary"])
def test_copy_editor_transient_error_reraises(monkeypatch, kind) -> None:
    """A transient LLM-transport error propagates unwrapped (delegated to Temporal), no fallback."""
    from agents.blogging.shared import json_retry as json_retry_mod

    from llm_service import LLMRateLimitError, LLMTemporaryError

    err_cls = LLMRateLimitError if kind == "rate_limit" else LLMTemporaryError

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise err_cls("transient outage")

    monkeypatch.setattr(json_retry_mod, "Agent", _Agent)
    agent = BlogCopyEditorAgent(
        llm_client=DummyLLMClient(), writing_style_guide_content="", brand_spec_content=""
    )
    with pytest.raises(err_cls):
        agent.run(CopyEditorInput(draft="# d\n\nsome body text here"))


@pytest.mark.parametrize("kind", ["rate_limit", "temporary"])
def test_copy_editor_event_loop_exception_unwraps_transient(monkeypatch, kind) -> None:
    """strands EventLoopException must re-raise the unwrapped transient cause."""
    from agents.blogging.shared import json_retry as json_retry_mod
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMRateLimitError, LLMTemporaryError

    err_cls = LLMRateLimitError if kind == "rate_limit" else LLMTemporaryError
    cause = err_cls("transient outage")

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise EventLoopException(cause)

    monkeypatch.setattr(json_retry_mod, "Agent", _Agent)
    agent = BlogCopyEditorAgent(
        llm_client=DummyLLMClient(), writing_style_guide_content="", brand_spec_content=""
    )
    with pytest.raises(err_cls) as exc_info:
        agent.run(CopyEditorInput(draft="# d\n\nsome body text here"))
    assert exc_info.value is cause


def test_copy_editor_unexpected_error_degrades_to_fallback(monkeypatch) -> None:
    """A non-transient, non-JSON LLM/programming error degrades to a manual-review
    fallback (approved, no feedback) instead of crashing the draft stage."""
    from agents.blogging.shared import json_retry as json_retry_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise RuntimeError("unexpected model failure")

    monkeypatch.setattr(json_retry_mod, "Agent", _Agent)
    agent = BlogCopyEditorAgent(
        llm_client=DummyLLMClient(), writing_style_guide_content="", brand_spec_content=""
    )

    result = agent.run(CopyEditorInput(draft="# d\n\nsome body text here"))

    assert isinstance(result, CopyEditorOutput)
    assert "manually" in result.summary.lower()
    # A tooling failure approves so it never drives a pointless no-op rewrite of a
    # within-length draft — the copy editor is advisory; hard gates run downstream.
    assert result.approved is True
    assert result.feedback_items == []


def test_copy_editor_run_empty_json_object_uses_advisory_fallback(monkeypatch) -> None:
    """A parseable but empty ({}) model response is normalized to the advisory fallback.

    That keeps approved=True with no feedback so callers do not loop on a false
    rejection with zero actionable items (unlike a real must_fix response).
    """
    from agents.blogging.shared import json_retry as json_retry_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return "{}"

    monkeypatch.setattr(json_retry_mod, "Agent", _Agent)
    agent = BlogCopyEditorAgent(
        llm_client=DummyLLMClient(), writing_style_guide_content="", brand_spec_content=""
    )

    result = agent.run(
        CopyEditorInput(
            draft="# d\n\nsome body text here",
            target_word_count=1000,
            soft_min_words=750,
            soft_max_words=1300,
        )
    )

    assert "manually" in result.summary.lower()
    assert result.approved is True
    assert result.feedback_items == []


def test_copy_editor_json_parse_failure_degrades_to_fallback(monkeypatch) -> None:
    """When the model never returns parseable JSON, the fallback approves (no no-op rewrite)."""
    from agents.blogging.shared import json_retry as json_retry_mod

    from llm_service import LLMJsonParseError

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise LLMJsonParseError("not json")

    monkeypatch.setattr(json_retry_mod, "Agent", _Agent)
    agent = BlogCopyEditorAgent(
        llm_client=DummyLLMClient(), writing_style_guide_content="", brand_spec_content=""
    )

    result = agent.run(CopyEditorInput(draft="# d\n\nsome body text here"))

    assert isinstance(result, CopyEditorOutput)
    assert "manually" in result.summary.lower()
    assert result.approved is True
    assert result.feedback_items == []


def test_write_feedback_to_path_returns_true_on_success(tmp_path: Path) -> None:
    """_write_feedback_to_path returns True and creates the file (incl. parents) on success."""
    agent = BlogCopyEditorAgent(llm_client=DummyLLMClient())
    output = CopyEditorOutput(summary="ok", feedback_items=[])
    target = tmp_path / "nested" / "dir" / "fb.json"

    assert agent._write_feedback_to_path(output, target) is True
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["summary"] == "ok"


def test_init_includes_brand_spec_in_style_prompt() -> None:
    """Brand spec content is prepended to the style prompt when provided at init."""
    agent = BlogCopyEditorAgent(
        llm_client=DummyLLMClient(),
        brand_spec_content="Acme voice: bold and direct.",
        writing_style_guide_content="Use short sentences.",
    )
    assert "--- BRAND SPEC ---" in agent._style_prompt
    assert "Acme voice" in agent._style_prompt
    assert "--- WRITING STYLE GUIDE ---" in agent._style_prompt


def test_build_editor_prompt_includes_optional_context() -> None:
    """Optional input fields appear in the assembled editor context."""
    agent = BlogCopyEditorAgent(
        llm_client=DummyLLMClient(),
        writing_style_guide_content=_TEST_STYLE_GUIDE,
    )
    draft = "# Title\n\nBody paragraph."
    prompt = agent._build_editor_prompt(
        CopyEditorInput(
            draft=draft,
            length_guidance="Keep sections tight; avoid filler.",
            audience="Developers",
            tone_or_purpose="Educational",
            human_feedback="Shorten the intro.",
            previous_feedback_items=[
                FeedbackItem(
                    category="clarity",
                    severity="should_fix",
                    location="paragraph 1",
                    issue="Opening is too long.",
                    suggestion="Cut to one sentence.",
                )
            ],
            content_plan_context="Section 1: hook\nSection 2: deep dive",
            soft_min_words=750,
            soft_max_words=1300,
            target_word_count=1000,
        ),
        draft,
        _TEST_STYLE_GUIDE,
    )
    assert "CONTENT PROFILE / LENGTH GUIDANCE" in prompt
    assert "Keep sections tight" in prompt
    assert "Audience: Developers" in prompt
    assert "Tone/Purpose: Educational" in prompt
    assert "AUTHOR'S REQUESTED CHANGES" in prompt
    assert "Shorten the intro." in prompt
    assert "PREVIOUS PASS FEEDBACK" in prompt
    assert "Opening is too long." in prompt
    assert "CONTENT PLAN" in prompt
    assert "Section 1: hook" in prompt
    assert "DRAFT TO REVIEW:" in prompt


def test_build_editor_prompt_without_style_guide() -> None:
    """When no style guide text is supplied, the prompt states there is nothing to evaluate."""
    agent = BlogCopyEditorAgent(llm_client=DummyLLMClient())
    draft = "Short draft body."
    prompt = agent._build_editor_prompt(
        CopyEditorInput(draft=draft, soft_min_words=None, soft_max_words=None),
        draft,
        "",
    )
    assert "No style guidelines were provided" in prompt


def test_parse_feedback_items_skips_invalid_and_applies_defaults() -> None:
    """Non-dict entries and empty issues are skipped; missing fields get defaults."""
    agent = BlogCopyEditorAgent(llm_client=DummyLLMClient())
    items = agent._parse_feedback_items(
        [
            "not a dict",
            {"issue": ""},
            {"category": "  ", "severity": "", "issue": "Needs work"},
            {
                "category": "voice",
                "severity": "must_fix",
                "location": " intro ",
                "issue": "Too formal",
                "suggestion": " Use contractions ",
            },
        ]
    )
    assert len(items) == 2
    assert items[0].category == ""
    assert items[0].severity == "consider"
    assert items[0].issue == "Needs work"
    assert items[1].category == "voice"
    assert items[1].location == "intro"
    assert items[1].suggestion == "Use contractions"


def test_thin_technical_deep_dive_injects_consider_feedback() -> None:
    """Technical deep dives well under soft_min get a thin-draft consider hint."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
    )
    # soft_min=750, ratio 0.88 → threshold 660; 500 words is thin.
    result = agent.run(
        CopyEditorInput(
            draft=_draft_n_words(500),
            target_word_count=1000,
            soft_min_words=750,
            soft_max_words=1300,
            content_profile="technical_deep_dive",
        )
    )
    thin_items = [
        item
        for item in result.feedback_items
        if item.severity == "consider" and "technical deep dive" in item.issue.lower()
    ]
    assert len(thin_items) == 1


def test_on_llm_request_callback_invoked() -> None:
    """run() forwards on_llm_request to the LLM invocation path."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
    )
    messages: list[str] = []

    agent.run(
        CopyEditorInput(draft="# Test\n\nDraft body."),
        on_llm_request=messages.append,
    )

    assert messages == ["Reviewing draft for style and clarity..."]


@pytest.mark.parametrize("raw_approved", ["false", ["nonempty"], "true", 1])
def test_non_bool_approved_defaults_to_false(monkeypatch, raw_approved) -> None:
    """A non-bool `approved` value (truthy string/list/int) must not be treated as True.

    Regression test: `bool(data.get("approved", False))` would coerce the
    string "false", a non-empty list, or a nonzero int to True. Only a real
    bool True should approve the draft.
    """
    from agents.blogging.shared import json_retry as json_retry_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return json.dumps(
                {"approved": raw_approved, "summary": "reviewed", "feedback_items": []}
            )

    monkeypatch.setattr(json_retry_mod, "Agent", _Agent)
    agent = BlogCopyEditorAgent(
        llm_client=DummyLLMClient(), writing_style_guide_content="", brand_spec_content=""
    )

    result = agent.run(CopyEditorInput(draft="# d\n\nsome body text here"))

    assert result.approved is False


def test_bool_true_approved_is_approved(monkeypatch) -> None:
    """A real bool True `approved` with no blocking feedback approves the draft."""
    from agents.blogging.shared import json_retry as json_retry_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return json.dumps({"approved": True, "summary": "reviewed", "feedback_items": []})

    monkeypatch.setattr(json_retry_mod, "Agent", _Agent)
    agent = BlogCopyEditorAgent(
        llm_client=DummyLLMClient(), writing_style_guide_content="", brand_spec_content=""
    )

    result = agent.run(CopyEditorInput(draft="# d\n\nsome body text here"))

    assert result.approved is True


def test_missing_approved_falls_back_to_severity_counts(monkeypatch) -> None:
    """When the model omits `approved` entirely, fall back to severity counts.

    Regression test for the fallback the inline comment above the approval
    derivation describes: no blocking (must_fix/should_fix) feedback items
    means approved defaults to True; a blocking item means approved defaults
    to False. Neither case includes an `approved` key in the LLM response.
    """
    from agents.blogging.shared import json_retry as json_retry_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return json.dumps({"summary": "reviewed", "feedback_items": self._items})

    def _make_agent(items):
        cls = type("_Agent", (_Agent,), {"_items": items})
        monkeypatch.setattr(json_retry_mod, "Agent", cls)
        return BlogCopyEditorAgent(
            llm_client=DummyLLMClient(), writing_style_guide_content="", brand_spec_content=""
        )

    agent_no_blocking = _make_agent([])
    result_no_blocking = agent_no_blocking.run(CopyEditorInput(draft="# d\n\nsome body text here"))
    assert result_no_blocking.approved is True

    agent_blocking = _make_agent(
        [
            {
                "category": "style",
                "severity": "must_fix",
                "issue": "Uses an em dash.",
            }
        ]
    )
    result_blocking = agent_blocking.run(CopyEditorInput(draft="# d\n\nsome body text here"))
    assert result_blocking.approved is False


def test_non_list_feedback_items_falls_back_to_empty(monkeypatch) -> None:
    """A truthy but non-list `feedback_items` value from the model must not crash run().

    Regression test: `data.get("feedback_items") or []` only falls back to `[]`
    when the value is falsy. A dict (or string) is truthy and was previously
    passed straight through to `_parse_feedback_items`, silently dropping all
    feedback instead of being treated as the empty/invalid response it is.
    """
    from agents.blogging.shared import json_retry as json_retry_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return json.dumps({"summary": "reviewed", "feedback_items": {"issue": "not a list"}})

    monkeypatch.setattr(json_retry_mod, "Agent", _Agent)
    agent = BlogCopyEditorAgent(
        llm_client=DummyLLMClient(), writing_style_guide_content="", brand_spec_content=""
    )

    result = agent.run(CopyEditorInput(draft="# d\n\nsome body text here"))

    assert result.feedback_items == []
    assert result.approved is True


def test_non_string_summary_falls_back_to_default(monkeypatch) -> None:
    """A non-string `summary` value from the model must not crash run().

    Regression test: `(data.get("summary") or "").strip()` assumes the LLM-returned
    `summary` is a string or None. A malformed response where `summary` is a list
    (or dict, or other non-string) previously raised AttributeError from `.strip()`
    instead of degrading gracefully.
    """
    from agents.blogging.shared import json_retry as json_retry_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return json.dumps({"summary": ["not", "a", "string"], "feedback_items": []})

    monkeypatch.setattr(json_retry_mod, "Agent", _Agent)
    agent = BlogCopyEditorAgent(
        llm_client=DummyLLMClient(), writing_style_guide_content="", brand_spec_content=""
    )

    result = agent.run(CopyEditorInput(draft="# d\n\nsome body text here"))

    assert result.summary == "No summary generated."


def test_write_feedback_to_path_returns_false_on_failure(tmp_path: Path) -> None:
    """_write_feedback_to_path reports failure via return value (False) instead of raising."""
    agent = BlogCopyEditorAgent(llm_client=DummyLLMClient())
    output = CopyEditorOutput(summary="ok", feedback_items=[])
    # A regular file cannot double as a parent directory, so mkdir(parents=True) fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    target = blocker / "fb.json"

    assert agent._write_feedback_to_path(output, target) is False
    assert not target.exists()
