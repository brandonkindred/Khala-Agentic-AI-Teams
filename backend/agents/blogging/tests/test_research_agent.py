"""Tests for ResearchAgent — uses _call_json + web_search + web_fetcher mocks."""

from __future__ import annotations

from typing import List
from unittest.mock import MagicMock

import pytest
from pydantic import HttpUrl


def _make_agent(monkeypatch, json_responses: List | None = None):
    """Build a ResearchAgent with _call_json stubbed to return a queue of responses."""
    from agents.blogging.blog_research_agent.agent import ResearchAgent

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
    from agents.blogging.blog_research_agent.agent import ResearchAgent

    from llm_service import DummyLLMClient

    with pytest.raises(AssertionError):
        ResearchAgent(llm_client=None)
    with pytest.raises(AssertionError):
        ResearchAgent(llm_client=DummyLLMClient(), max_fetch_documents=0)


def test_research_agent_call_json_strips_fences(monkeypatch) -> None:
    """_call_json delegates to a Strands Agent and strips markdown fences."""
    from agents.blogging.blog_research_agent import agent as ra_mod
    from agents.blogging.blog_research_agent.agent import ResearchAgent

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
    from agents.blogging.blog_research_agent.models import (
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
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

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
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.blog_research_agent.tools.arxiv_search import ArxivSearchError

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
    from agents.blogging.blog_research_agent import agent as ra_mod

    def boom(*args, **kwargs):
        raise ArxivSearchError("nope")

    monkeypatch.setattr(ra_mod, "search_arxiv", boom)
    out = a.run(ResearchBriefInput(brief="AI", max_results=5))
    assert out.references == []


def test_research_agent_run_with_cache_resume(monkeypatch, tmp_path) -> None:
    """ResearchAgent resumes from cache when a checkpoint exists."""
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

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


def test_research_agent_run_checkpoints_candidates_after_fresh_search(
    monkeypatch, tmp_path
) -> None:
    """A fresh (non-cached) search run must checkpoint its candidates, same as every
    other step, so a resumed run after this point doesn't repeat the web searches.

    Regression test: previously only steps 1, 2, 4, 5, 6, 7 saved a checkpoint —
    step 3 (search) computed ``candidates`` but never persisted them, so an
    AgentCache-backed retry after this point still re-ran every web search.
    """
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="fresh search brief", max_results=3)

    a = _make_agent(
        monkeypatch,
        [
            {"topic": "AI"},
            {"queries": [{"query_text": "q", "intent": "overview"}]},
        ],
    )
    a.cache = cache
    mock_search = MagicMock()
    mock_search.search.return_value = []
    a.web_search = mock_search
    # Stub arXiv rather than letting the real query run: a live search can return
    # non-empty results depending on network access/query text, which would make
    # the academic_papers assertion below environment-dependent.
    monkeypatch.setattr(ResearchAgent, "_fetch_academic_papers", lambda self, b: [])

    a.run(brief)

    checkpoint = cache.load_checkpoint(brief)
    assert checkpoint is not None
    # None (unset) vs [] (checkpointed-but-empty) is the whole point of the regression:
    # before the fix this step's checkpoint was never written at all.
    assert checkpoint.candidates == []
    # academic_papers/similar_topics are also checkpointed now (steps 8-9), saved
    # sequentially after notes once the parallel section's .result()s come back.
    assert checkpoint.academic_papers == []
    assert checkpoint.similar_topics == []
    assert checkpoint.last_completed_step == "similar_topics"


def test_research_agent_run_resumes_from_empty_candidates_checkpoint(monkeypatch, tmp_path) -> None:
    """A checkpoint with candidates=[] (a completed search that found nothing) must
    be treated as resumable, not re-run: `[]` and "no checkpoint" are different states.

    Regression test: the resume check originally used truthiness (`cached_state and
    cached_state.candidates`), so an empty-but-completed candidates checkpoint looked
    identical to a missing one and the web searches ran again anyway.
    """
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="empty candidates brief", max_results=3)
    cache.save_checkpoint(brief, "normalized", normalized={"topic": "cached"})
    cache.save_checkpoint(brief, "queries", queries=[{"query_text": "q", "intent": "overview"}])
    cache.save_checkpoint(brief, "candidates", candidates=[])

    a = _make_agent(monkeypatch, [])
    a.cache = cache
    mock_search = MagicMock()
    mock_search.search.return_value = []
    a.web_search = mock_search
    # Stub arXiv so this test doesn't depend on live network access.
    monkeypatch.setattr(ResearchAgent, "_fetch_academic_papers", lambda self, b: [])

    a.run(brief)

    mock_search.search.assert_not_called()


def test_research_agent_run_resumes_academic_papers_and_similar_topics(
    monkeypatch, tmp_path
) -> None:
    """Cached academic_papers/similar_topics checkpoints must be reused, not
    recomputed, so a resumed run doesn't redo the arXiv HTTP call or the LLM
    similar-topics call."""
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="resume tail steps", max_results=3)
    cache.save_checkpoint(brief, "normalized", normalized={"topic": "cached"})
    cache.save_checkpoint(brief, "queries", queries=[{"query_text": "q", "intent": "overview"}])
    cache.save_checkpoint(brief, "candidates", candidates=[])
    cache.save_checkpoint(brief, "documents", documents=[])
    cache.save_checkpoint(brief, "scored_docs", scored_docs=[])
    cache.save_checkpoint(brief, "references", references=[])
    cache.save_checkpoint(brief, "notes", notes="cached notes")
    cache.save_checkpoint(
        brief,
        "academic_papers",
        academic_papers=[
            {
                "title": "Cached Paper",
                "url": "https://arxiv.org/abs/1234",
                "overview_or_summary": "cached abstract",
            }
        ],
    )
    cache.save_checkpoint(brief, "similar_topics", similar_topics=["cached topic"])

    a = _make_agent(monkeypatch, [])
    a.cache = cache

    fetch_spy = MagicMock(side_effect=AssertionError("should not re-fetch academic papers"))
    similar_spy = MagicMock(side_effect=AssertionError("should not recompute similar topics"))
    monkeypatch.setattr(ResearchAgent, "_fetch_academic_papers", fetch_spy)
    monkeypatch.setattr(ResearchAgent, "_get_similar_topics", similar_spy)

    out = a.run(brief)

    fetch_spy.assert_not_called()
    similar_spy.assert_not_called()
    assert out.notes == "cached notes"
    assert [p.title for p in out.academic_papers] == ["Cached Paper"]
    assert out.similar_topics == ["cached topic"]


def test_research_agent_synthesize_overview_no_references() -> None:
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    from llm_service import DummyLLMClient

    a = ResearchAgent(llm_client=DummyLLMClient())
    out = a._synthesize_overview(ResearchBriefInput(brief="x", max_results=5), references=[])
    assert out is None


def _doc(n: int):
    from agents.blogging.blog_research_agent.models import SourceDocument

    return SourceDocument(
        url=HttpUrl(f"https://example.com/{n}"),
        title=f"Doc {n}",
        content="content " * 20,
        publish_date=None,
        domain="example.com",
        language="en",
        metadata={},
    )


def test_score_documents_propagates_attribution_into_workers(monkeypatch) -> None:
    """Regression: the per-document scoring fan-out must run inside a copy of the
    caller's context so LLM attribution / request-id reach the worker threads
    (raw ThreadPoolExecutor submission used to drop them)."""
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    from llm_service import (
        DummyLLMClient,
        bind_request_id,
        current_attribution,
        current_request_id,
        llm_attribution,
    )

    a = ResearchAgent(llm_client=DummyLLMClient())
    seen: list[tuple[str, str]] = []

    def fake_score_one(self, doc, brief_input):
        seen.append((current_attribution().team, current_request_id()))
        return (doc, 1.0, 1.0, 1.0, "blog")

    monkeypatch.setattr(ResearchAgent, "_score_one_document", fake_score_one)

    docs = [_doc(0), _doc(1), _doc(2)]
    with llm_attribution(team="blogging"), bind_request_id("req-score-123"):
        a._score_documents(docs, ResearchBriefInput(brief="x", max_results=5))

    assert seen == [("blogging", "req-score-123")] * 3


def test_summarize_documents_propagates_attribution_into_workers(monkeypatch) -> None:
    """Same contract for the per-document summarization fan-out."""
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.models import ResearchBriefInput, ResearchReference

    from llm_service import (
        DummyLLMClient,
        bind_request_id,
        current_attribution,
        current_request_id,
        llm_attribution,
    )

    a = ResearchAgent(llm_client=DummyLLMClient())
    seen: list[tuple[str, str]] = []

    def fake_summarize_one(self, item, brief_input):
        seen.append((current_attribution().team, current_request_id()))
        doc = item[0]
        return ResearchReference(
            title=doc.title, url=doc.url, domain=doc.domain, summary="s", key_points=[]
        )

    monkeypatch.setattr(ResearchAgent, "_summarize_one_document", fake_summarize_one)

    scored = [(_doc(i), 1.0, 1.0, 1.0, "blog") for i in range(3)]
    with llm_attribution(team="blogging"), bind_request_id("req-sum-456"):
        a._summarize_documents(scored, ResearchBriefInput(brief="x", max_results=5))

    assert seen == [("blogging", "req-sum-456")] * 3
