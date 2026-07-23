"""Tests for BlogWriterAgent.run() — the main draft generation method."""

from __future__ import annotations


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
    monkeypatch.setattr(BlogWriterAgent, "_self_review", lambda self, d: d)

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


def test_writer_run_call_agent_throws_then_json_fallback(monkeypatch) -> None:
    """_call_agent raises; _call_agent_json succeeds with a draft."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(ValueError("oops")),
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, **kw: {"draft": "# Fallback\nBody."},
    )
    monkeypatch.setattr(BlogWriterAgent, "_self_review", lambda self, d: d)
    out = a.run(_writer_input())
    assert "Fallback" in out.draft


def test_writer_run_call_agent_throws_and_fallback_also_fails(monkeypatch) -> None:
    """Both _call_agent and _call_agent_json fail — placeholder returned."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(ValueError("oops")),
    )

    def boom(self, p, **kw):
        raise TypeError("nope")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", boom)
    out = a.run(_writer_input())
    assert "No draft was generated" in out.draft


def test_writer_run_default_length_guidance(monkeypatch) -> None:
    """When length_guidance is empty, the default 'TARGET LENGTH' block is appended."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    captured = {"prompt": ""}

    def fake_call(self, prompt, system_prompt=""):
        captured["prompt"] = prompt
        return '{"draft": 0}\n---DRAFT---\n# Out\nBody.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake_call)
    monkeypatch.setattr(BlogWriterAgent, "_self_review", lambda self, d: d)
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
    """All text attempts fail; JSON fallback helper succeeds."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)

    def boom(self, p, system_prompt=""):
        raise RuntimeError("transient")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    monkeypatch.setattr(
        BlogWriterAgent,
        "_fallback_draft_via_json",
        lambda self, p: "# Recovered",
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


def test_writer_revise_single_item_total_failure_returns_original(monkeypatch) -> None:
    """All retries + fallback fail → original draft returned."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)

    def boom(self, p, system_prompt=""):
        raise RuntimeError("nope")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    monkeypatch.setattr(BlogWriterAgent, "_fallback_draft_via_json", lambda self, p: None)
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
    """_fallback_draft_via_json invokes call_json_with_retry correctly and returns a stripped draft."""

    a = _agent()
    captured: dict = {}

    def fake_retry(factory, prompt, **kwargs):
        captured["max_attempts"] = kwargs.get("max_attempts")
        captured["prompt"] = prompt
        captured["strict"] = kwargs.get("strict_json_suffix", "")
        assert callable(factory)
        assert callable(kwargs.get("on_exhausted"))
        assert callable(kwargs.get("on_unexpected_error"))
        assert callable(kwargs.get("unwrap_exception"))
        return {"draft": "  # From JSON  \n"}

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.call_json_with_retry",
        fake_retry,
    )
    out = a._fallback_draft_via_json("revise this draft")
    assert out == "# From JSON"
    assert captured["max_attempts"] == 2
    assert "Respond with valid JSON only" in captured["prompt"]
    assert "draft" in captured["strict"].lower()


def test_fallback_draft_via_json_empty_draft_returns_none(monkeypatch) -> None:
    """Whitespace-only draft values are normalized to None so callers keep the original."""

    a = _agent()
    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.call_json_with_retry",
        lambda *a, **k: {"draft": "   "},
    )
    assert a._fallback_draft_via_json("prompt") is None


def test_fallback_draft_via_json_missing_draft_returns_none(monkeypatch) -> None:
    """A JSON response with no 'draft' key yields None."""

    a = _agent()
    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.call_json_with_retry",
        lambda *a, **k: {},
    )
    assert a._fallback_draft_via_json("prompt") is None


def test_fallback_draft_via_json_exhausted_hook_returns_none(monkeypatch) -> None:
    """on_exhausted returning {} must yield None (keep original draft at call sites)."""
    from llm_service import LLMJsonParseError

    a = _agent()

    def fake_retry(factory, prompt, **kwargs):
        return kwargs["on_exhausted"](LLMJsonParseError("bad json"))

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.call_json_with_retry",
        fake_retry,
    )
    assert a._fallback_draft_via_json("prompt") is None


def test_fallback_draft_via_json_unexpected_hook_returns_none(monkeypatch) -> None:
    """on_unexpected_error returning {} causes _fallback_draft_via_json to return None."""

    a = _agent()

    def fake_retry(factory, prompt, **kwargs):
        return kwargs["on_unexpected_error"](RuntimeError("boom"))

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.call_json_with_retry",
        fake_retry,
    )
    assert a._fallback_draft_via_json("prompt") is None


def test_fallback_draft_via_json_transient_reraises(monkeypatch) -> None:
    """Transient LLM errors from call_json_with_retry are re-raised, not converted to None."""
    import pytest

    from llm_service import LLMRateLimitError

    a = _agent()

    def fake_retry(factory, prompt, **kwargs):
        raise LLMRateLimitError("rate limited")

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.call_json_with_retry",
        fake_retry,
    )

    with pytest.raises(LLMRateLimitError):
        a._fallback_draft_via_json("prompt")


def test_fallback_draft_via_json_unwraps_event_loop_transient(monkeypatch) -> None:
    """Strands EventLoopException wrappers must re-raise the unwrapped transient cause.

    The draft-stage Temporal funnel retries only on LLMRateLimitError /
    LLMTemporaryError; re-raising the wrapper would be swallowed by on_unexpected_error
    and silently keep the unrevised draft.
    """
    import pytest
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMRateLimitError

    a = _agent()
    wrapped = LLMRateLimitError("429 after client retries")

    class _BoomAgent:
        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, prompt):
            raise EventLoopException(wrapped)

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.Agent",
        _BoomAgent,
    )
    with pytest.raises(LLMRateLimitError) as excinfo:
        a._fallback_draft_via_json("prompt")
    assert excinfo.value is wrapped
    assert not isinstance(excinfo.value, EventLoopException)
