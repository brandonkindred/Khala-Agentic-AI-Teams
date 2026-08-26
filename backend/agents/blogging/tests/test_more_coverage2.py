"""More targeted coverage tests:

* ``blog_research_agent.agent`` — _synthesize_overview branches, _get_similar_topics,
  _fetch_academic_papers swallow.
* ``blog_publication_agent.agent`` — submit_draft, approve, reject, revision loop.
* ``blog_planning_agent.agent`` — make_blog_planning_agent / generate_content_plan.
"""

from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# blog_research_agent — extra branches
# ---------------------------------------------------------------------------


def test_research_synthesize_overview_dict_with_outline(monkeypatch) -> None:
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.models import (
        ResearchBriefInput,
        ResearchReference,
    )
    from pydantic import HttpUrl

    from llm_service import DummyLLMClient

    a = ResearchAgent(llm_client=DummyLLMClient())
    monkeypatch.setattr(
        ResearchAgent,
        "_call_json",
        lambda self, p: {"analysis": "It's fine.", "outline": ["point 1", "point 2"]},
    )
    refs = [
        ResearchReference(
            title="t",
            url=HttpUrl("https://x.com"),
            domain="x.com",
            summary="s",
            key_points=["k1"],
            type="primary",
            recency=None,
            relevance_score=0.8,
            authority_score=0.6,
            accuracy_score=0.7,
        )
    ]
    out = a._synthesize_overview(ResearchBriefInput(brief="x", max_results=5), refs)
    assert "It's fine." in out
    assert "point 1" in out


def test_research_synthesize_overview_string_response(monkeypatch) -> None:
    """LLM may return a string directly."""
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.models import (
        ResearchBriefInput,
        ResearchReference,
    )
    from pydantic import HttpUrl

    from llm_service import DummyLLMClient

    a = ResearchAgent(llm_client=DummyLLMClient())
    monkeypatch.setattr(ResearchAgent, "_call_json", lambda self, p: "Just a string")
    refs = [
        ResearchReference(
            title="t",
            url=HttpUrl("https://x.com"),
            domain="x.com",
            summary="s",
            key_points=[],
            type=None,
            recency=None,
            relevance_score=0.5,
            authority_score=0.5,
            accuracy_score=0.5,
        )
    ]
    out = a._synthesize_overview(ResearchBriefInput(brief="x", max_results=5), refs)
    assert out == "Just a string"


def test_research_synthesize_overview_json_error_returns_none(monkeypatch) -> None:
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.models import (
        ResearchBriefInput,
        ResearchReference,
    )
    from pydantic import HttpUrl

    from llm_service import DummyLLMClient, LLMJsonParseError

    a = ResearchAgent(llm_client=DummyLLMClient())

    def boom(self, p):
        raise LLMJsonParseError("bad", response_preview="doc")

    monkeypatch.setattr(ResearchAgent, "_call_json", boom)
    refs = [
        ResearchReference(
            title="t",
            url=HttpUrl("https://x.com"),
            domain=None,
            summary="s",
            key_points=[],
            type=None,
            recency=None,
            relevance_score=0.5,
            authority_score=0.5,
            accuracy_score=0.5,
        )
    ]
    out = a._synthesize_overview(ResearchBriefInput(brief="x", max_results=5), refs)
    assert out is None


def test_research_get_similar_topics_no_refs() -> None:
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    from llm_service import DummyLLMClient

    a = ResearchAgent(llm_client=DummyLLMClient())
    assert a._get_similar_topics(ResearchBriefInput(brief="x", max_results=5), []) == []


def test_research_get_similar_topics_filters_by_score(monkeypatch) -> None:
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.models import (
        ResearchBriefInput,
        ResearchReference,
    )
    from pydantic import HttpUrl

    from llm_service import DummyLLMClient

    a = ResearchAgent(llm_client=DummyLLMClient())
    monkeypatch.setattr(
        ResearchAgent,
        "_call_json",
        lambda self, p: {
            "similar_topics": [
                {"topic": "Strong match", "similarity_score": 0.9},
                {"topic": "Weak match", "similarity_score": 0.3},
                {"topic": "No score"},
                "not-a-dict",
            ]
        },
    )
    refs = [
        ResearchReference(
            title="t",
            url=HttpUrl("https://x.com"),
            domain=None,
            summary="s",
            key_points=[],
            type=None,
            recency=None,
            relevance_score=0.5,
            authority_score=0.5,
            accuracy_score=0.5,
        )
    ]
    out = a._get_similar_topics(ResearchBriefInput(brief="x", max_results=5), refs)
    assert "Strong match" in out
    assert "Weak match" not in out


def test_research_get_similar_topics_llm_error(monkeypatch) -> None:
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.models import (
        ResearchBriefInput,
        ResearchReference,
    )
    from pydantic import HttpUrl

    from llm_service import DummyLLMClient

    a = ResearchAgent(llm_client=DummyLLMClient())

    def boom(self, p):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(ResearchAgent, "_call_json", boom)
    refs = [
        ResearchReference(
            title="t",
            url=HttpUrl("https://x.com"),
            domain=None,
            summary="s",
            key_points=[],
            type=None,
            recency=None,
            relevance_score=0.5,
            authority_score=0.5,
            accuracy_score=0.5,
        )
    ]
    assert a._get_similar_topics(ResearchBriefInput(brief="x", max_results=5), refs) == []


def test_research_fetch_academic_papers_swallows_error(monkeypatch) -> None:
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    from llm_service import DummyLLMClient

    a = ResearchAgent(llm_client=DummyLLMClient())

    import agents.blogging.blog_research_agent.agent as ra_mod

    def boom(*a, **kw):
        raise RuntimeError("arxiv down")

    monkeypatch.setattr(ra_mod, "search_arxiv", boom)
    out = a._fetch_academic_papers(ResearchBriefInput(brief="x", max_results=5))
    assert out == []


# ---------------------------------------------------------------------------
# blog_publication_agent
# ---------------------------------------------------------------------------


def test_publication_submit_draft_happy(tmp_path) -> None:
    from agents.blogging.blog_publication_agent import BlogPublicationAgent, SubmitDraftInput

    from llm_service import DummyLLMClient

    agent = BlogPublicationAgent(llm_client=DummyLLMClient(), blog_posts_root=tmp_path / "posts")
    inp = SubmitDraftInput(
        draft="# My Title\n\nBody.",
        audience="devs",
        tone_or_purpose="inform",
        tags=["ai"],
    )
    out = agent.submit_draft(inp)
    assert out.submission_id
    assert out.file_path.exists()
    assert out.state == "awaiting_approval"


def test_publication_submit_draft_empty_raises(tmp_path) -> None:
    from agents.blogging.blog_publication_agent import BlogPublicationAgent, SubmitDraftInput

    from llm_service import DummyLLMClient

    agent = BlogPublicationAgent(llm_client=DummyLLMClient(), blog_posts_root=tmp_path / "posts")
    with pytest.raises(ValueError):
        agent.submit_draft(SubmitDraftInput(draft="   "))


def test_publication_approve_happy(tmp_path) -> None:
    from agents.blogging.blog_publication_agent import BlogPublicationAgent, SubmitDraftInput

    from llm_service import DummyLLMClient

    agent = BlogPublicationAgent(llm_client=DummyLLMClient(), blog_posts_root=tmp_path / "posts")
    sub = agent.submit_draft(SubmitDraftInput(draft="# Title\n\nBody."))
    result = agent.approve(sub.submission_id)
    assert result.folder_path.exists()
    assert (result.folder_path / "medium.md").exists()
    assert (result.folder_path / "devto.md").exists()
    assert (result.folder_path / "substack.md").exists()


def test_publication_approve_missing_raises(tmp_path) -> None:
    from agents.blogging.blog_publication_agent import BlogPublicationAgent

    from llm_service import DummyLLMClient

    agent = BlogPublicationAgent(llm_client=DummyLLMClient(), blog_posts_root=tmp_path / "posts")
    with pytest.raises(FileNotFoundError):
        agent.approve("nonexistent-submission-id")


def test_publication_reject_with_force_ready(tmp_path) -> None:
    from agents.blogging.blog_publication_agent import BlogPublicationAgent, SubmitDraftInput

    from llm_service import DummyLLMClient

    agent = BlogPublicationAgent(llm_client=DummyLLMClient(), blog_posts_root=tmp_path / "posts")
    sub = agent.submit_draft(SubmitDraftInput(draft="# t\n\nBody"))
    out = agent.reject(sub.submission_id, "too vague", force_ready_to_revise=True)
    assert out.ready_to_revise is True
    assert "too vague" in out.collected_feedback_summary


def test_publication_reject_with_llm_followup(tmp_path, monkeypatch) -> None:
    from agents.blogging.blog_publication_agent import BlogPublicationAgent, SubmitDraftInput
    from agents.blogging.shared import json_retry as jr_mod

    from llm_service import DummyLLMClient

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return json.dumps(
                {
                    "ready_to_revise": False,
                    "questions": ["What outcome did you want?"],
                    "feedback_summary": "Needs more specifics",
                }
            )

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    agent = BlogPublicationAgent(llm_client=DummyLLMClient(), blog_posts_root=tmp_path / "posts")
    sub = agent.submit_draft(SubmitDraftInput(draft="# t\n\nBody"))
    out = agent.reject(sub.submission_id, "weak hook")
    assert out.ready_to_revise is False
    assert "What outcome" in out.questions[0]


def test_publication_reject_missing_submission(tmp_path) -> None:
    from agents.blogging.blog_publication_agent import BlogPublicationAgent

    from llm_service import DummyLLMClient

    agent = BlogPublicationAgent(llm_client=DummyLLMClient(), blog_posts_root=tmp_path / "posts")
    with pytest.raises(FileNotFoundError):
        agent.reject("nope", "bad")


# ---------------------------------------------------------------------------
# blog_planning_agent.agent — make_blog_planning_agent factory + run flow
# ---------------------------------------------------------------------------


def test_planning_agent_make_default_init() -> None:
    """make_blog_planning_agent() is a zero-arg factory used by the sandbox shim."""
    from agents.blogging.blog_planning_agent.agent import make_blog_planning_agent

    a = make_blog_planning_agent()
    assert a is not None


def test_planning_agent_runner_delegates(monkeypatch) -> None:
    """The _BlogPlanningAgentRunner.run() delegates to BlogPlanningAgent.run."""
    from agents.blogging.blog_planning_agent.agent import make_blog_planning_agent
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan, make_planning_phase_result

    runner = make_blog_planning_agent()

    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    ppr = make_planning_phase_result(plan, planning_wall_ms_total=1.0)

    # Patch the wrapped agent's run method
    monkeypatch.setattr(runner._agent.__class__, "run", lambda self, *a, **kw: ppr)
    out = runner.run(
        {"planning_input": {"brief": "hi", "length_policy_context": "ctx", "research_digest": "rd"}}
    )
    assert out["content_plan"]["overarching_topic"] == "x"
