"""Tests for TrendDiscoveryAgent."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from blog_research_agent.models import CandidateResult
from blog_research_agent.tools.web_search import WebSearchError

from llm_service import DummyLLMClient
from social_media_marketing_team.trend_discovery_agent import TrendDiscoveryAgent
from social_media_marketing_team.trend_models import TrendDigest, TrendingTopic


def _make_candidate(title: str, url: str, snippet: str = "") -> CandidateResult:
    return CandidateResult(title=title, url=url, snippet=snippet, source="test", rank=1)


def _default_topics_payload():
    return [
        {
            "title": "AI tools dominate professional conversations",
            "summary": "AI productivity tools are trending across LinkedIn and X with millions of posts.",
            "platforms": ["LinkedIn", "X/Twitter"],
            "sources": ["https://example.com/1"],
            "relevance_score": 0.92,
        },
        {
            "title": "New music video breaks TikTok records",
            "summary": "A viral music video has generated over 500M views in 24 hours on TikTok.",
            "platforms": ["TikTok", "Instagram"],
            "sources": ["https://example.com/2"],
            "relevance_score": 0.88,
        },
        {
            "title": "Climate summit sparks global debate",
            "summary": "World leaders' statements at the UN climate summit are driving heated discussion on Bluesky and Reddit.",
            "platforms": ["Bluesky", "Reddit"],
            "sources": ["https://example.com/3"],
            "relevance_score": 0.80,
        },
    ]


class _TrendLLM(DummyLLMClient):
    """DummyLLMClient subclass that returns trend topic JSON from complete_json.

    Since DummyLLMClient.stream() routes through complete_json(), overriding
    complete_json() controls what the Strands Agent returns.
    """

    def __init__(self, topics=None):
        super().__init__()
        self._topics = topics if topics is not None else _default_topics_payload()
        self._prompts: list[str] = []

    def complete_json(self, prompt, **kwargs):
        self._prompts.append(prompt)
        return {"topics": self._topics}


@pytest.fixture()
def mock_search():
    searcher = MagicMock()
    searcher.search.return_value = [
        _make_candidate(
            "Trending Now", "https://example.com/news", "Social media is buzzing today."
        ),
    ]
    return searcher


def test_run_returns_digest_with_three_topics(mock_search):
    llm = _TrendLLM()
    agent = TrendDiscoveryAgent(llm_client=llm, web_search=mock_search)
    digest = agent.run(date="2026-03-16")

    assert isinstance(digest, TrendDigest)
    assert len(digest.topics) == 3
    assert all(isinstance(t, TrendingTopic) for t in digest.topics)


def test_run_populates_platforms_searched(mock_search):
    llm = _TrendLLM()
    agent = TrendDiscoveryAgent(llm_client=llm, web_search=mock_search)
    digest = agent.run(date="2026-03-16")

    assert len(digest.platforms_searched) > 0
    assert "X/Twitter" in digest.platforms_searched
    assert "TikTok" in digest.platforms_searched


def test_run_records_search_query_count(mock_search):
    llm = _TrendLLM()
    agent = TrendDiscoveryAgent(llm_client=llm, web_search=mock_search)
    digest = agent.run(date="2026-03-16")

    assert digest.search_query_count == 8


def test_run_deduplicates_urls(mock_search):
    """Duplicate URLs from different search queries should only appear once."""
    dup = _make_candidate("Dup", "https://example.com/same", "snippet")
    mock_search.search.return_value = [dup]

    llm = _TrendLLM()
    agent = TrendDiscoveryAgent(llm_client=llm, web_search=mock_search)
    agent.run(date="2026-03-16")

    # LLM should receive one unique result, not 8 copies of the same URL
    assert len(llm._prompts) == 1
    assert llm._prompts[0].count("https://example.com/same") == 1


def test_run_graceful_when_all_searches_fail(mock_search):
    """If every search raises WebSearchError, return an empty digest without raising."""
    mock_search.search.side_effect = WebSearchError("API unavailable")

    llm = _TrendLLM()
    agent = TrendDiscoveryAgent(llm_client=llm, web_search=mock_search)
    digest = agent.run(date="2026-03-16")

    assert isinstance(digest, TrendDigest)
    assert digest.topics == []
    assert len(llm._prompts) == 0  # LLM should not be called when no search results


def test_run_graceful_when_llm_fails(mock_search):
    """If the LLM call produces invalid JSON, return a digest with empty topics."""

    class _FailingLLM(DummyLLMClient):
        def complete_json(self, prompt, **kwargs):
            return {"invalid": "not topics format"}

    llm = _FailingLLM()
    agent = TrendDiscoveryAgent(llm_client=llm, web_search=mock_search)
    digest = agent.run(date="2026-03-16")

    assert isinstance(digest, TrendDigest)
    assert digest.topics == []
    assert digest.search_query_count == 8


def test_run_caps_topics_at_three(mock_search):
    """Even if the LLM returns more than 3 topics, only 3 should be in the digest."""
    extra_topics = _default_topics_payload() + [
        {
            "title": "Fourth topic",
            "summary": "Should be discarded.",
            "platforms": ["Facebook"],
            "sources": [],
            "relevance_score": 0.5,
        }
    ]

    llm = _TrendLLM(topics=extra_topics)
    agent = TrendDiscoveryAgent(llm_client=llm, web_search=mock_search)
    digest = agent.run(date="2026-03-16")

    assert len(digest.topics) == 3


def test_run_relevance_score_clamped(mock_search):
    """Relevance scores outside 0-1 should be clamped."""
    topics = [
        {
            "title": "T1",
            "summary": "S1",
            "platforms": [],
            "sources": [],
            "relevance_score": 1.5,
        },
        {
            "title": "T2",
            "summary": "S2",
            "platforms": [],
            "sources": [],
            "relevance_score": -0.2,
        },
        {
            "title": "T3",
            "summary": "S3",
            "platforms": [],
            "sources": [],
            "relevance_score": 0.7,
        },
    ]

    llm = _TrendLLM(topics=topics)
    agent = TrendDiscoveryAgent(llm_client=llm, web_search=mock_search)
    digest = agent.run(date="2026-03-16")

    scores = [t.relevance_score for t in digest.topics]
    assert all(0.0 <= s <= 1.0 for s in scores)


def test_run_generated_at_is_set(mock_search):
    llm = _TrendLLM()
    agent = TrendDiscoveryAgent(llm_client=llm, web_search=mock_search)
    digest = agent.run(date="2026-03-16")

    assert digest.generated_at
    assert "T" in digest.generated_at or "+" in digest.generated_at
