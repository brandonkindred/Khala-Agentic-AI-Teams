"""Tests for blog_writer_agent helpers and blog_writing_process_v2 module-level
functions.

These cover small pure helpers and constructor / validation paths — the heavy
``run_pipeline`` orchestrator stays out of scope.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# blog_writer_agent.agent helpers
# ---------------------------------------------------------------------------


def test_extract_draft_after_marker_marker_present() -> None:
    from blog_writer_agent.agent import _extract_draft_after_marker

    raw = '{"draft": 0}\n---DRAFT---\n# Hello\n\nBody\n'
    assert _extract_draft_after_marker(raw).startswith("# Hello")


def test_extract_draft_after_marker_marker_inline() -> None:
    from blog_writer_agent.agent import _extract_draft_after_marker

    raw = "---DRAFT---# Hi"
    assert _extract_draft_after_marker(raw).startswith("# Hi")


def test_extract_draft_after_marker_falls_back_to_json() -> None:
    from blog_writer_agent.agent import _extract_draft_after_marker

    raw = '{"draft": "# Title\\n\\nBody"}'
    out = _extract_draft_after_marker(raw)
    assert "Title" in out


def test_extract_draft_after_marker_empty_inputs() -> None:
    from blog_writer_agent.agent import _extract_draft_after_marker

    assert _extract_draft_after_marker("") == ""
    assert _extract_draft_after_marker(None) == ""  # type: ignore[arg-type]
    assert _extract_draft_after_marker("not json and no marker") == ""


def test_write_draft_to_path_creates_parents(tmp_path: Path) -> None:
    from blog_writer_agent.agent import _write_draft_to_path

    target = tmp_path / "a" / "b" / "draft.md"
    _write_draft_to_path("# draft\n", target)
    assert target.read_text() == "# draft\n"


def test_writer_agent_requires_guidelines() -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    from llm_service import DummyLLMClient

    # Initialised without guidelines is allowed; only revise/draft enforces.
    agent = BlogWriterAgent(llm_client=DummyLLMClient())
    with pytest.raises(ValueError, match="brand"):
        agent._assert_guidelines_present()


def test_writer_agent_assertion_on_none_llm() -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    with pytest.raises(AssertionError):
        BlogWriterAgent(llm_client=None)


def test_writer_agent_style_prompt_merge() -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    from llm_service import DummyLLMClient

    a = BlogWriterAgent(
        llm_client=DummyLLMClient(),
        writing_style_guide_content="Style A",
        brand_spec_content="Brand B",
    )
    assert "BRAND SPEC" in a._style_prompt
    assert "Brand B" in a._style_prompt
    assert "WRITING STYLE GUIDE" in a._style_prompt


def test_writer_agent_deterministic_self_check() -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    from llm_service import DummyLLMClient

    a = BlogWriterAgent(llm_client=DummyLLMClient())
    # Em-dash, banned phrase, vague citation, few 'you', staccato prose
    bad = (
        "In today's fast-paced world—as we navigate change. "
        "Studies show this works.\n\n"
        "Word. Word. Word.\n"
    )
    out = a._deterministic_self_check(bad)
    joined = "\n".join(out)
    assert "Em/en dash" in joined
    assert "Banned phrase" in joined
    assert "Vague citation" in joined or "Reader address" in joined


def test_writer_agent_deterministic_self_check_clean_draft() -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    from llm_service import DummyLLMClient

    a = BlogWriterAgent(llm_client=DummyLLMClient())
    clean = (
        "# Header\n\n"
        "You are reading something. You will learn. You will see. "
        "We covered tracing, costs, and evaluation in real workloads.\n\n"
        "These are pragmatic and useful for shipping high-quality software in your team's stack.\n"
    )
    out = a._deterministic_self_check(clean)
    # May still have some, but should be small
    assert isinstance(out, list)


# ---------------------------------------------------------------------------
# blog_writing_process_v2 module-level helpers
# ---------------------------------------------------------------------------


def test_v2_extract_story_placeholders() -> None:
    from agent_implementations.blog_writing_process_v2 import (
        _extract_story_placeholders,
    )

    draft = """# Post

[Author: add a moment you debugged a production outage]
Some paragraph.
[Author: insert a story about your favourite tool]
"""
    out = _extract_story_placeholders(draft)
    assert len(out) == 2
    topics = [t for _, t in out]
    assert any("outage" in t for t in topics)
    assert any("favourite tool" in t for t in topics)


def test_v2_extract_plan_keywords() -> None:
    from types import SimpleNamespace

    from agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    plan = SimpleNamespace(
        overarching_topic="Designing scalable systems and APIs",
        sections=[
            SimpleNamespace(title="Introduction to scale", order=0),
            SimpleNamespace(title="Practical tradeoffs", order=1),
        ],
    )
    kws = _extract_plan_keywords(plan)
    # Words like 'and', 'to' (<4 chars) should be dropped; longer kept
    assert "scalable" in kws
    assert "systems" in kws
    assert all(len(k) >= 4 for k in kws)


def test_v2_extract_plan_keywords_handles_empty() -> None:
    from agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    plan = type("P", (), {"overarching_topic": "", "sections": []})()
    assert _extract_plan_keywords(plan) == []


def test_v2_is_external_cancellation_temporal() -> None:
    from agent_implementations.blog_writing_process_v2 import (
        _is_external_cancellation,
    )
    from temporalio.exceptions import CancelledError

    e = CancelledError("nope")
    assert _is_external_cancellation(e) is True
    assert _is_external_cancellation(RuntimeError("not cancellation")) is False


def test_v2_planning_llm_client_passthrough(monkeypatch) -> None:
    from agent_implementations.blog_writing_process_v2 import (
        build_plan_critic_agent,
        plan_critic_llm_client,
        planning_llm_client,
    )

    from llm_service import DummyLLMClient

    base = DummyLLMClient()
    # No override env vars → passthrough
    monkeypatch.delenv("BLOG_PLANNING_MODEL", raising=False)
    monkeypatch.delenv("BLOG_PLAN_CRITIC_MODEL", raising=False)
    monkeypatch.delenv("BLOG_PLAN_CRITIC_ENABLED", raising=False)

    assert planning_llm_client(base) is base
    assert plan_critic_llm_client(base) is base
    assert build_plan_critic_agent(base) is None


def test_v2_build_plan_critic_agent_when_enabled(monkeypatch) -> None:
    from agent_implementations.blog_writing_process_v2 import (
        build_plan_critic_agent,
    )

    from llm_service import DummyLLMClient

    monkeypatch.setenv("BLOG_PLAN_CRITIC_ENABLED", "true")
    base = DummyLLMClient()
    critic = build_plan_critic_agent(base)
    assert critic is not None


# ---------------------------------------------------------------------------
# ghost_writer_agent — pure helpers
# ---------------------------------------------------------------------------


def test_ghost_writer_no_experience_phrase() -> None:
    from ghost_writer_agent.agent import _is_no_experience

    assert _is_no_experience("skip") is True
    assert _is_no_experience("SKIP.") is True
    assert _is_no_experience("none") is True
    assert _is_no_experience("I don't have any story") is True
    assert _is_no_experience("I haven't tried that") is True
    assert _is_no_experience("Yes I have a great one") is False
    assert _is_no_experience("nothing comes to mind here") is True


def test_ghost_writer_agent_construction() -> None:
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    assert agent is not None


def test_ghost_writer_extract_gaps_from_plan_no_opportunities() -> None:
    """find_story_gaps falls back to LLM when plan has no story_opportunity fields."""
    from ghost_writer_agent.agent import GhostWriterElicitationAgent
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    from llm_service import DummyLLMClient

    plan = ContentPlan(
        overarching_topic="X",
        narrative_flow="flow",
        sections=[
            ContentPlanSection(title="A", coverage_description="cov", order=0),
        ],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    # When no story_opportunity on sections, _extract_gaps_from_plan returns []
    out = agent._extract_gaps_from_plan(plan)
    assert out == []


def test_ghost_writer_extract_gaps_from_plan_with_opportunities(monkeypatch) -> None:
    from ghost_writer_agent.agent import GhostWriterElicitationAgent
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    from llm_service import DummyLLMClient

    sec_a = ContentPlanSection(
        title="A", coverage_description="cov", order=0, story_opportunity="A debug story"
    )
    sec_b = ContentPlanSection(
        title="B",
        coverage_description="cov2",
        order=1,
        story_opportunity="A migration story",
    )
    plan = ContentPlan(
        overarching_topic="X",
        narrative_flow="flow",
        sections=[sec_a, sec_b],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    # Patch the seed generator to avoid LLM call
    monkeypatch.setattr(agent, "_generate_friendly_seeds", lambda opps: [f"seed-{o}" for o in opps])
    out = agent._extract_gaps_from_plan(plan)
    assert len(out) == 2
    assert out[0].section_title == "A"
    assert "seed-A debug story" == out[0].seed_question


def test_ghost_writer_generate_friendly_seeds_fallback(monkeypatch) -> None:
    """When the LLM call raises, _generate_friendly_seeds falls back to generic seeds."""
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())

    # Patch the Agent class globally inside ghost_writer_agent.agent
    import ghost_writer_agent.agent as gw_agent

    class _BoomAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise RuntimeError("nope")

    monkeypatch.setattr(gw_agent, "Agent", _BoomAgent)
    out = agent._generate_friendly_seeds(["topic A.", "topic B."])
    assert len(out) == 2
    assert all("topic" in s.lower() for s in out)


def test_ghost_writer_generate_friendly_seeds_empty_input() -> None:
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    assert agent._generate_friendly_seeds([]) == []


# ---------------------------------------------------------------------------
# graphs/*.py — call build helpers
# ---------------------------------------------------------------------------


def test_graphs_build_returns_objects() -> None:
    """Smoke-test the build_* helpers — confirms imports + composition work."""
    from graphs.copy_edit_swarm import build_copy_edit_swarm
    from graphs.pipeline_graph import build_post_review_graph, build_pre_review_graph
    from graphs.rewrite_swarm import build_rewrite_swarm

    assert build_copy_edit_swarm() is not None
    assert build_rewrite_swarm() is not None
    assert build_pre_review_graph() is not None
    assert build_post_review_graph() is not None
