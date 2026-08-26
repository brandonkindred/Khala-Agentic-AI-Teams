"""Tests for BlogWriterAgent.run() — the main draft generation method."""

from __future__ import annotations

import pytest


def _agent():
    from .conftest import make_writer_agent

    return make_writer_agent()


def _writer_input(**overrides):
    from agents.blogging.blog_writer_agent.models import WriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    plan = make_content_plan(
        overarching_topic="Topic",
        narrative_flow="flow",
        sections=[ContentPlanSection(title="Intro", coverage_description="hook", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    kwargs = {
        "content_plan": plan,
        "audience": "devs",
        "tone_or_purpose": "inform",
    }
    kwargs.update(overrides)
    return WriterInput(**kwargs)


def test_writer_run_happy_with_all_options(monkeypatch, tmp_path) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": '{"draft": 0}\n---DRAFT---\n# A Title\nBody.\n',
    )
    # Disable the self-review path so tests stay deterministic
    monkeypatch.setattr(BlogWriterAgent, "_self_review", lambda self, d, allowed_claims_section="": d)

    output_path = tmp_path / "draft.md"
    out = a.run(
        _writer_input(
            selected_title="The Selected One",
            elicited_stories="A real story",
            length_guidance="aim for 1000 words",
        ),
        on_llm_request=lambda msg: None,
        draft_output_path=output_path,
    )
    assert "A Title" in out.draft
    assert output_path.exists()


def test_writer_run_empty_outline_returns_placeholder(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.models import WriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _agent()
    plan = make_content_plan(
        overarching_topic="Topic",
        narrative_flow="flow",
        sections=[ContentPlanSection(title="A", coverage_description="x", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    # Mock outline_for_prompt to return empty string
    monkeypatch.setattr(WriterInput, "outline_for_prompt", lambda self: "")
    out = a.run(WriterInput(content_plan=plan))
    assert "Add a content plan" in out.draft


def test_writer_run_no_marker_returns_placeholder(monkeypatch) -> None:
    """LLM returns text without ---DRAFT--- marker — placeholder returned."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": "no marker text",
    )
    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", lambda self, p, **kw: {})
    out = a.run(_writer_input())
    assert "No draft was generated" in out.draft


def test_writer_run_placeholder_skips_self_review(monkeypatch) -> None:
    """Empty draft uses ``_PLACEHOLDER_DRAFT`` and does not invoke self-review."""
    from agents.blogging.blog_writer_agent.agent import (
        _PLACEHOLDER_DRAFT,
        BlogWriterAgent,
    )

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": "no marker text",
    )
    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", lambda self, p, **kw: {})
    calls: list[str] = []
    monkeypatch.setattr(
        BlogWriterAgent,
        "_self_review",
        lambda self, d, allowed_claims_section="": calls.append(d) or d,
    )
    out = a.run(_writer_input())
    assert out.draft == _PLACEHOLDER_DRAFT
    assert calls == []


def test_writer_run_old_short_prefix_still_self_reviews(monkeypatch) -> None:
    """A draft matching only the old short prefix still runs self-review."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    body = "# Draft\n\nNo draft yet — waiting on research."
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": f'{{"draft": 0}}\n---DRAFT---\n{body}',
    )
    calls: list[str] = []
    monkeypatch.setattr(
        BlogWriterAgent,
        "_self_review",
        lambda self, d, allowed_claims_section="": calls.append(d) or d,
    )
    out = a.run(_writer_input())
    assert out.draft == body
    assert calls == [body]


def test_writer_run_json_parse_error_then_json_fallback(monkeypatch) -> None:
    """LLMJsonParseError on the text path soft-fails into the JSON draft fallback."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMJsonParseError

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(
            LLMJsonParseError("bad draft text")
        ),
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, **kw: {"draft": "# Fallback\nBody."},
    )
    monkeypatch.setattr(BlogWriterAgent, "_self_review", lambda self, d, allowed_claims_section="": d)
    out = a.run(_writer_input())
    assert "Fallback" in out.draft


def test_writer_run_wrapped_json_parse_error_then_json_fallback(monkeypatch) -> None:
    """EventLoopException-wrapped LLMJsonParseError still soft-fails to JSON fallback."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMJsonParseError

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(
            EventLoopException(LLMJsonParseError("bad draft text"))
        ),
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, **kw: {"draft": "# Unwrapped Fallback\nBody."},
    )
    monkeypatch.setattr(BlogWriterAgent, "_self_review", lambda self, d, allowed_claims_section="": d)
    out = a.run(_writer_input())
    assert "Unwrapped Fallback" in out.draft


def test_writer_run_json_fallback_non_dict_returns_placeholder(monkeypatch) -> None:
    """A non-dict/None return from _call_agent_json in the fallback must not raise
    AttributeError — it should fall through to the placeholder draft."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMJsonParseError

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(
            LLMJsonParseError("bad draft text")
        ),
    )
    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", lambda self, p, **kw: None)
    out = a.run(_writer_input())
    assert "No draft was generated" in out.draft


def test_writer_run_json_parse_error_and_fallback_also_fails(monkeypatch) -> None:
    """LLMJsonParseError on both text and JSON paths yields the placeholder draft."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import LLMJsonParseError

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(
            LLMJsonParseError("bad draft text")
        ),
    )

    def boom(self, p, **kw):
        raise LLMJsonParseError("bad json fallback")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", boom)
    out = a.run(_writer_input())
    assert "No draft was generated" in out.draft


def test_writer_run_wrapped_json_parse_error_and_fallback_also_fails(monkeypatch) -> None:
    """Wrapped parse errors on both paths still yield the placeholder draft."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMJsonParseError

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(
            EventLoopException(LLMJsonParseError("bad draft text"))
        ),
    )

    def boom(self, p, **kw):
        raise EventLoopException(LLMJsonParseError("bad json fallback"))

    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", boom)
    out = a.run(_writer_input())
    assert "No draft was generated" in out.draft


@pytest.mark.parametrize(
    "exc",
    [TypeError("programmer bug"), ValueError("programmer bug")],
    ids=["TypeError", "ValueError"],
)
def test_writer_run_programming_error_propagates(monkeypatch, exc: Exception) -> None:
    """TypeError/ValueError on draft generation must propagate, not soft-fail."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(exc),
    )
    with pytest.raises(type(exc), match="programmer bug"):
        a.run(_writer_input())


def test_writer_run_wrapped_programming_error_propagates(monkeypatch) -> None:
    """EventLoopException wrapping a programming error must still propagate."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from strands.types.exceptions import EventLoopException

    a = _agent()
    wrapped = EventLoopException(TypeError("programmer bug"))
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(wrapped),
    )
    with pytest.raises(EventLoopException) as excinfo:
        a.run(_writer_input())
    assert isinstance(excinfo.value.original_exception, TypeError)
    assert "programmer bug" in str(excinfo.value.original_exception)


def test_writer_run_default_length_guidance(monkeypatch) -> None:
    """When length_guidance is empty, the default 'TARGET LENGTH' block is appended."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    captured = {"prompt": ""}

    def fake_call(self, prompt, system_prompt=""):
        captured["prompt"] = prompt
        return '{"draft": 0}\n---DRAFT---\n# Out\nBody.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake_call)
    monkeypatch.setattr(BlogWriterAgent, "_self_review", lambda self, d, allowed_claims_section="": d)
    a.run(_writer_input(length_guidance=""))
    assert "TARGET LENGTH" in captured["prompt"]


def test_writer_revise_single_item_happy(monkeypatch) -> None:
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": '{"draft": 0}\n---DRAFT---\n# Single Item Revised\nBody.',
    )
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    ri = ReviseWriterInput(
        draft="# Orig", feedback_items=[item], feedback_summary="s", content_plan=plan
    )
    out = a._revise_single_item(
        draft="# Orig",
        item=item,
        item_index=1,
        total_items=1,
        style_guide_text="style",
        revise_input=ri,
    )
    assert "Single Item Revised" in out


def test_writer_revise_single_item_fallback_path(monkeypatch) -> None:
    """Text path yields no draft marker; JSON fallback helper succeeds."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)

    monkeypatch.setattr(
        BlogWriterAgent, "_call_text", lambda self, p, system_prompt="": "no marker"
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_fallback_draft_via_json",
        lambda self, p, system_prompt="": "# Recovered",
    )
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    ri = ReviseWriterInput(
        draft="# Orig", feedback_items=[item], feedback_summary="s", content_plan=plan
    )
    out = a._revise_single_item(
        draft="# Orig",
        item=item,
        item_index=1,
        total_items=1,
        style_guide_text="style",
        revise_input=ri,
    )
    assert "Recovered" in out


def test_writer_revise_single_item_programming_error_propagates(monkeypatch) -> None:
    """Non-transient errors from the text path must not be retried as transient."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)

    def boom(self, p, system_prompt=""):
        raise RuntimeError("programmer bug")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    ri = ReviseWriterInput(
        draft="# Orig", feedback_items=[item], feedback_summary="s", content_plan=plan
    )
    with pytest.raises(RuntimeError, match="programmer bug"):
        a._revise_single_item(
            draft="# Orig\nBody.",
            item=item,
            item_index=1,
            total_items=1,
            style_guide_text="style",
            revise_input=ri,
        )


def test_writer_revise_single_item_transient_retries_then_fallback(monkeypatch) -> None:
    """LLMTemporaryError on the text path is retried, then JSON fallback may recover."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from llm_service import LLMTemporaryError

    from ._content_plan_test_utils import make_content_plan

    a = _agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)

    def boom(self, p, system_prompt=""):
        raise LLMTemporaryError("503")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    monkeypatch.setattr(
        BlogWriterAgent,
        "_fallback_draft_via_json",
        lambda self, p, system_prompt="": "# Recovered transient",
    )
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    ri = ReviseWriterInput(
        draft="# Orig", feedback_items=[item], feedback_summary="s", content_plan=plan
    )
    out = a._revise_single_item(
        draft="# Orig",
        item=item,
        item_index=1,
        total_items=1,
        style_guide_text="style",
        revise_input=ri,
    )
    assert "Recovered transient" in out


def test_writer_revise_single_item_wrapped_json_parse_error_retries_then_fallback(
    monkeypatch,
) -> None:
    """EventLoopException-wrapped LLMJsonParseError must retry (not re-raise), then fall back.

    Regression test: the ``except Exception`` branch used to unwrap the cause
    only to check for LLMRateLimitError/LLMTemporaryError, re-raising a wrapped
    LLMJsonParseError instead of retrying it the same way as an unwrapped one.
    """
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMJsonParseError

    from ._content_plan_test_utils import make_content_plan

    a = _agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)

    def boom(self, p, system_prompt=""):
        raise EventLoopException(LLMJsonParseError("bad json", response_preview="x"))

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    monkeypatch.setattr(
        BlogWriterAgent,
        "_fallback_draft_via_json",
        lambda self, p, system_prompt="": "# Recovered from wrapped parse error",
    )
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    ri = ReviseWriterInput(
        draft="# Orig", feedback_items=[item], feedback_summary="s", content_plan=plan
    )
    out = a._revise_single_item(
        draft="# Orig",
        item=item,
        item_index=1,
        total_items=1,
        style_guide_text="style",
        revise_input=ri,
    )
    assert "Recovered from wrapped parse error" in out


def test_writer_revise_single_item_total_failure_returns_original(monkeypatch) -> None:
    """Empty text responses + failed fallback → original draft returned."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)

    monkeypatch.setattr(
        BlogWriterAgent, "_call_text", lambda self, p, system_prompt="": "no marker"
    )
    monkeypatch.setattr(
        BlogWriterAgent, "_fallback_draft_via_json", lambda self, p, system_prompt="": None
    )
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    ri = ReviseWriterInput(
        draft="# Orig", feedback_items=[item], feedback_summary="s", content_plan=plan
    )
    out = a._revise_single_item(
        draft="# Orig\nBody.",
        item=item,
        item_index=1,
        total_items=1,
        style_guide_text="style",
        revise_input=ri,
    )
    assert "Orig" in out


def test_writer_revise_single_item_fallback_unexpected_keeps_original(monkeypatch) -> None:
    """Unexpected JSON-fallback errors keep the original draft (not crash)."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)

    monkeypatch.setattr(
        BlogWriterAgent, "_call_text", lambda self, p, system_prompt="": "no marker"
    )

    def boom_fallback(self, p, system_prompt=""):
        raise RuntimeError("fallback boom")

    monkeypatch.setattr(BlogWriterAgent, "_fallback_draft_via_json", boom_fallback)
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    ri = ReviseWriterInput(
        draft="# Orig", feedback_items=[item], feedback_summary="s", content_plan=plan
    )
    out = a._revise_single_item(
        draft="# Orig\nBody.",
        item=item,
        item_index=1,
        total_items=1,
        style_guide_text="style",
        revise_input=ri,
    )
    assert "Orig" in out


def test_writer_revise_single_item_fallback_transient_reraises(monkeypatch) -> None:
    """Transient LLM errors from JSON fallback still propagate for stage retry."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from llm_service import LLMRateLimitError

    from ._content_plan_test_utils import make_content_plan

    a = _agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)

    monkeypatch.setattr(
        BlogWriterAgent, "_call_text", lambda self, p, system_prompt="": "no marker"
    )

    def boom_fallback(self, p, system_prompt=""):
        raise LLMRateLimitError("429")

    monkeypatch.setattr(BlogWriterAgent, "_fallback_draft_via_json", boom_fallback)
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    ri = ReviseWriterInput(
        draft="# Orig", feedback_items=[item], feedback_summary="s", content_plan=plan
    )
    with pytest.raises(LLMRateLimitError):
        a._revise_single_item(
            draft="# Orig\nBody.",
            item=item,
            item_index=1,
            total_items=1,
            style_guide_text="style",
            revise_input=ri,
        )


def test_writer_build_revise_single_item_prompt(monkeypatch) -> None:
    """Smoke test the prompt building helper with title + stories + length_guidance."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _agent()
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    ri = ReviseWriterInput(
        draft="# d",
        feedback_items=[item],
        feedback_summary="s",
        content_plan=plan,
        selected_title="My Title",
        elicited_stories="A story",
        length_guidance="aim for 1000 words",
    )
    p = a._build_revise_single_item_prompt(
        draft="# d",
        item=item,
        item_index=1,
        total_items=3,
        style_guide_text="style",
        revise_input=ri,
    )
    assert "AUTHOR-CHOSEN TITLE" in p
    assert "AUTHOR'S PERSONAL STORIES" in p
    assert "aim for 1000 words" in p
    assert "1/3" in p


def test_writer_build_revise_single_item_prompt_default_length() -> None:
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _agent()
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    ri = ReviseWriterInput(
        draft="# d",
        feedback_items=[item],
        feedback_summary="s",
        content_plan=plan,
    )
    p = a._build_revise_single_item_prompt(
        draft="# d",
        item=item,
        item_index=1,
        total_items=2,
        style_guide_text="style",
        revise_input=ri,
    )
    assert "TARGET LENGTH" in p


def test_fallback_draft_via_json_success(monkeypatch) -> None:
    """_fallback_draft_via_json invokes run_json_gate correctly and returns a stripped draft."""

    a = _agent()
    captured: dict = {}

    def fake_gate(model, system_prompt, prompt, **kwargs):
        captured["max_attempts"] = kwargs.get("max_attempts")
        captured["prompt"] = prompt
        captured["strict"] = kwargs.get("strict_json_suffix", "")
        captured["fresh_agent_per_attempt"] = kwargs.get("fresh_agent_per_attempt")
        assert callable(kwargs.get("fallback_builder"))
        return {"draft": "  # From JSON  \n"}

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.run_json_gate",
        fake_gate,
    )
    out = a._fallback_draft_via_json("revise this draft")
    assert out == "# From JSON"
    assert captured["max_attempts"] == 2
    assert captured["fresh_agent_per_attempt"] is True
    assert "Respond with valid JSON only" in captured["prompt"]
    assert "draft" in captured["strict"].lower()


def test_fallback_draft_via_json_rejects_empty_prompt() -> None:
    """Empty/whitespace-only prompt raises ValueError, surviving `-O` optimization."""

    a = _agent()
    with pytest.raises(ValueError, match="prompt must be a non-empty string"):
        a._fallback_draft_via_json("   ")


def test_fallback_draft_via_json_rejects_non_string_prompt() -> None:
    """Non-string prompt raises ValueError, surviving `-O` optimization."""

    a = _agent()
    with pytest.raises(ValueError, match="prompt must be a non-empty string"):
        a._fallback_draft_via_json(None)


def test_fallback_draft_via_json_empty_draft_returns_none(monkeypatch) -> None:
    """Whitespace-only draft values are normalized to None so callers keep the original."""

    a = _agent()
    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.run_json_gate",
        lambda *a, **k: {"draft": "   "},
    )
    assert a._fallback_draft_via_json("prompt") is None


def test_fallback_draft_via_json_missing_draft_returns_none(monkeypatch) -> None:
    """A JSON response with no 'draft' key yields None."""

    a = _agent()
    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.run_json_gate",
        lambda *a, **k: {},
    )
    assert a._fallback_draft_via_json("prompt") is None


def test_fallback_draft_via_json_exhausted_hook_returns_none(monkeypatch) -> None:
    """fallback_builder returning {} on JSON-parse exhaustion yields None (keep original draft)."""
    from llm_service import LLMJsonParseError

    a = _agent()

    def fake_gate(model, system_prompt, prompt, **kwargs):
        return kwargs["fallback_builder"](LLMJsonParseError("bad json"))

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.run_json_gate",
        fake_gate,
    )
    assert a._fallback_draft_via_json("prompt") is None


def test_fallback_draft_via_json_unexpected_hook_returns_none(monkeypatch) -> None:
    """fallback_builder returning {} on an unexpected error yields None."""

    a = _agent()

    def fake_gate(model, system_prompt, prompt, **kwargs):
        assert kwargs.get("max_attempts") == 2
        return kwargs["fallback_builder"](RuntimeError("boom"))

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.run_json_gate",
        fake_gate,
    )
    assert a._fallback_draft_via_json("prompt") is None


def test_fallback_draft_via_json_transient_reraises(monkeypatch) -> None:
    """Transient LLM errors from run_json_gate are re-raised, not converted to None."""
    from llm_service import LLMRateLimitError

    a = _agent()

    def fake_gate(model, system_prompt, prompt, **kwargs):
        assert kwargs.get("max_attempts") == 2
        raise LLMRateLimitError("rate limited")

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.run_json_gate",
        fake_gate,
    )

    with pytest.raises(LLMRateLimitError):
        a._fallback_draft_via_json("prompt")


def test_fallback_draft_via_json_unwraps_event_loop_transient(monkeypatch) -> None:
    """Strands EventLoopException wrappers must re-raise the unwrapped transient cause.

    The draft-stage Temporal funnel retries only on LLMRateLimitError /
    LLMTemporaryError; re-raising the wrapper would be swallowed by the fallback
    and silently keep the unrevised draft.
    """
    from agents.blogging.shared import json_retry as json_retry_mod
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMRateLimitError

    a = _agent()
    wrapped = LLMRateLimitError("429 after client retries")

    class _BoomAgent:
        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, prompt):
            raise EventLoopException(wrapped)

    monkeypatch.setattr(json_retry_mod, "Agent", _BoomAgent)
    with pytest.raises(LLMRateLimitError) as excinfo:
        a._fallback_draft_via_json("prompt")
    assert excinfo.value is wrapped
    assert not isinstance(excinfo.value, EventLoopException)


def test_fallback_draft_via_json_agent_construction_error_returns_none(monkeypatch) -> None:
    """Agent construction TypeError is caught by the helper policy and yields None."""
    from agents.blogging.shared import json_retry as json_retry_mod

    a = _agent()

    class _BadAgent:
        def __init__(self, *args, **kwargs):
            raise TypeError("unsupported model config")

        def __call__(self, prompt):
            raise AssertionError("should not be called")

    monkeypatch.setattr(json_retry_mod, "Agent", _BadAgent)
    assert a._fallback_draft_via_json("prompt") is None
