"""Tests for BlogWriterAgent.run() — the main draft generation method."""

from __future__ import annotations


def _agent():
    from blog_writer_agent.agent import BlogWriterAgent

    from llm_service import DummyLLMClient

    return BlogWriterAgent(
        llm_client=DummyLLMClient(),
        writing_style_guide_content="Style",
        brand_spec_content="Brand",
    )


def _writer_input(**overrides):
    from blog_writer_agent.models import WriterInput
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    plan = ContentPlan(
        overarching_topic="Topic",
        narrative_flow="flow",
        sections=[ContentPlanSection(title="Intro", coverage_description="hook", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    kwargs = {
        "content_plan": plan,
        "audience": "devs",
        "tone_or_purpose": "inform",
    }
    kwargs.update(overrides)
    return WriterInput(**kwargs)


def test_writer_run_happy_with_all_options(monkeypatch, tmp_path) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent",
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
    from blog_writer_agent.models import WriterInput
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    a = _agent()
    plan = ContentPlan(
        overarching_topic="Topic",
        narrative_flow="flow",
        sections=[ContentPlanSection(title="A", coverage_description="x", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    # Mock outline_for_prompt to return empty string
    monkeypatch.setattr(WriterInput, "outline_for_prompt", lambda self: "")
    out = a.run(WriterInput(content_plan=plan))
    assert "Add a content plan" in out.draft


def test_writer_run_no_marker_returns_placeholder(monkeypatch) -> None:
    """LLM returns text without ---DRAFT--- marker — placeholder returned."""
    from blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent",
        lambda self, p, system_prompt="": "no marker text",
    )
    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", lambda self, p, **kw: {})
    out = a.run(_writer_input())
    assert "No draft was generated" in out.draft


def test_writer_run_call_agent_throws_then_json_fallback(monkeypatch) -> None:
    """_call_agent raises; _call_agent_json succeeds with a draft."""
    from blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent",
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
    from blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent",
        lambda self, p, system_prompt="": (_ for _ in ()).throw(ValueError("oops")),
    )

    def boom(self, p, **kw):
        raise TypeError("nope")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", boom)
    out = a.run(_writer_input())
    assert "No draft was generated" in out.draft


def test_writer_run_default_length_guidance(monkeypatch) -> None:
    """When length_guidance is empty, the default 'TARGET LENGTH' block is appended."""
    from blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    captured = {"prompt": ""}

    def fake_call(self, prompt, system_prompt=""):
        captured["prompt"] = prompt
        return '{"draft": 0}\n---DRAFT---\n# Out\nBody.'

    monkeypatch.setattr(BlogWriterAgent, "_call_agent", fake_call)
    monkeypatch.setattr(BlogWriterAgent, "_self_review", lambda self, d: d)
    a.run(_writer_input(length_guidance=""))
    assert "TARGET LENGTH" in captured["prompt"]


def test_writer_revise_single_item_happy(monkeypatch) -> None:
    from blog_copy_editor_agent.models import FeedbackItem
    from blog_writer_agent.agent import BlogWriterAgent
    from blog_writer_agent.models import ReviseWriterInput
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    a = _agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent",
        lambda self, p, system_prompt="": '{"draft": 0}\n---DRAFT---\n# Single Item Revised\nBody.',
    )
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = ContentPlan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
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
    """All 2 attempts at _call_agent fail; _call_agent_json succeeds."""
    from blog_copy_editor_agent.models import FeedbackItem
    from blog_writer_agent.agent import BlogWriterAgent
    from blog_writer_agent.models import ReviseWriterInput
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    a = _agent()
    import blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)

    def boom(self, p, system_prompt=""):
        raise RuntimeError("transient")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent", boom)
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, **kw: {"draft": "# Recovered"},
    )
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = ContentPlan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
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
    from blog_copy_editor_agent.models import FeedbackItem
    from blog_writer_agent.agent import BlogWriterAgent
    from blog_writer_agent.models import ReviseWriterInput
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    a = _agent()
    import blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)

    def boom(self, p, system_prompt=""):
        raise RuntimeError("nope")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent", boom)

    def boom_json(self, p, **kw):
        raise ValueError("nope")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", boom_json)
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = ContentPlan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
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
    from blog_copy_editor_agent.models import FeedbackItem
    from blog_writer_agent.models import ReviseWriterInput
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    a = _agent()
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = ContentPlan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
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
    from blog_copy_editor_agent.models import FeedbackItem
    from blog_writer_agent.models import ReviseWriterInput
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    a = _agent()
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = ContentPlan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
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
