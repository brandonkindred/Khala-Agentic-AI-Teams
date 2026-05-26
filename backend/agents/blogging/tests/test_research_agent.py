"""Tests for ResearchAgent — uses _call_json + web_search + web_fetcher mocks."""

from __future__ import annotations

from typing import List
from unittest.mock import MagicMock

import pytest
from pydantic import HttpUrl


def _make_agent(monkeypatch, json_responses: List | None = None):
    """Build a ResearchAgent with _call_json stubbed to return a queue of responses."""
    from blog_research_agent.agent import ResearchAgent

    from llm_service import DummyLLMClient

    state = {"i": 0}
    responses = json_responses or []

    def fake_call_json(self, prompt: str):
        i = state["i"]
        state["i"] += 1
        if i >= len(responses):
            return responses[-1] if responses else {}
        r = responses[i]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(ResearchAgent, "_call_json", fake_call_json)
    return ResearchAgent(llm_client=DummyLLMClient())


def test_research_agent_init_validation() -> None:
    from blog_research_agent.agent import ResearchAgent

    from llm_service import DummyLLMClient

    with pytest.raises(AssertionError):
        ResearchAgent(llm_client=None)
    with pytest.raises(AssertionError):
        ResearchAgent(llm_client=DummyLLMClient(), max_fetch_documents=0)


def test_research_agent_call_json_strips_fences(monkeypatch) -> None:
    """_call_json delegates to a Strands Agent and strips markdown fences."""
    from blog_research_agent import agent as ra_mod
    from blog_research_agent.agent import ResearchAgent

    from llm_service import DummyLLMClient

    class _StubAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return '```json\n{"a": 1}\n```'

    monkeypatch.setattr(ra_mod, "Agent", _StubAgent)
    a = ResearchAgent(llm_client=DummyLLMClient())
    assert a._call_json("anything") == {"a": 1}


def test_research_agent_run_happy_path(monkeypatch) -> None:
    """End-to-end smoke test with all seams mocked."""
    from blog_research_agent.models import (
        CandidateResult,
        ResearchBriefInput,
        SourceDocument,
    )

    # JSON responses by step:
    # 1. parse_brief
    # 2. generate_queries
    # 3. score_one_document × N (per fetched doc)
    # 4. summarize_one_document × N
    # 5. synthesize_overview
    a = _make_agent(
        monkeypatch,
        [
            # 1. parse_brief returns "normalized"
            {"topic": "AI", "angle": "intro", "constraints": "short"},
            # 2. generate_queries returns one query
            {"queries": [{"query_text": "what is AI", "intent": "overview"}]},
            # 3. score_one_document
            {
                "relevance_score": 0.9,
                "authority_score": 0.8,
                "accuracy_score": 0.7,
                "type": "primary",
            },
            # 4. summarize_one_document
            {"summary": "About AI.", "key_points": ["fact 1"]},
            # 5. synthesize_overview
            {"analysis": "AI is interesting.", "outline": ["a", "b"]},
        ],
    )

    # Mock web_search.search
    mock_search = MagicMock()
    mock_search.search.return_value = [
        CandidateResult(
            title="AI page",
            url=HttpUrl("https://example.com/ai"),
            snippet="snip",
            source="ollama",
            rank=1,
        )
    ]
    a.web_search = mock_search

    # Mock web_fetcher.fetch
    mock_fetcher = MagicMock()
    mock_fetcher.fetch.return_value = SourceDocument(
        url=HttpUrl("https://example.com/ai"),
        title="AI",
        content="About AI" * 50,
        publish_date=None,
        domain="example.com",
        language="en",
        metadata={},
    )
    a.web_fetcher = mock_fetcher

    out = a.run(
        ResearchBriefInput(brief="Tell me about AI", max_results=5),
        progress_callback=lambda s, p: None,
    )
    assert out.references
    assert "AI" in out.notes or out.notes is not None


def test_research_agent_run_no_results_path(monkeypatch) -> None:
    """When web_search returns nothing, the pipeline still completes (empty references)."""
    from blog_research_agent.agent import ResearchAgent
    from blog_research_agent.models import ResearchBriefInput

    a = _make_agent(
        monkeypatch,
        [
            {"topic": "AI"},
            {"queries": [{"query_text": "q1", "intent": "overview"}]},
            # No more responses needed
        ],
    )
    mock_search = MagicMock()
    mock_search.search.return_value = []
    a.web_search = mock_search

    # _fetch_academic_papers may be called — stub it
    monkeypatch.setattr(ResearchAgent, "_fetch_academic_papers", lambda self, b: [])

    out = a.run(ResearchBriefInput(brief="AI", max_results=5))
    assert out.references == []


def test_research_agent_run_handles_arxiv_failure(monkeypatch) -> None:
    """ArXiv search failure is swallowed (best-effort)."""
    from blog_research_agent.models import ResearchBriefInput
    from blog_research_agent.tools.arxiv_search import ArxivSearchError

    a = _make_agent(
        monkeypatch,
        [
            {"topic": "AI"},
            {"queries": [{"query_text": "q1", "intent": "overview"}]},
        ],
    )
    mock_search = MagicMock()
    mock_search.search.return_value = []
    a.web_search = mock_search

    # Make arxiv search raise
    from blog_research_agent import agent as ra_mod

    def boom(*args, **kwargs):
        raise ArxivSearchError("nope")

    monkeypatch.setattr(ra_mod, "search_arxiv", boom)
    out = a.run(ResearchBriefInput(brief="AI", max_results=5))
    assert out.references == []


def test_research_agent_run_with_cache_resume(monkeypatch, tmp_path) -> None:
    """ResearchAgent resumes from cache when a checkpoint exists."""
    from blog_research_agent.agent_cache import AgentCache
    from blog_research_agent.models import ResearchBriefInput

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="cached brief", max_results=3)
    cache.save_checkpoint(brief, "normalized", normalized={"topic": "cached"})

    a = _make_agent(
        monkeypatch,
        [
            # parse_brief is skipped (cached normalized)
            # generate_queries
            {"queries": [{"query_text": "q", "intent": "overview"}]},
        ],
    )
    a.cache = cache
    mock_search = MagicMock()
    mock_search.search.return_value = []
    a.web_search = mock_search

    out = a.run(brief)
    assert out.references == []


def test_research_agent_synthesize_overview_no_references() -> None:
    from blog_research_agent.agent import ResearchAgent
    from blog_research_agent.models import ResearchBriefInput

    from llm_service import DummyLLMClient

    a = ResearchAgent(llm_client=DummyLLMClient())
    out = a._synthesize_overview(ResearchBriefInput(brief="x", max_results=5), references=[])
    assert out is None
