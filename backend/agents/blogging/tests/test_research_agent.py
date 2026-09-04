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
    # 3. _evaluate_one_document via _evaluate_documents (one merged
    #    score+summarize call per fetched doc)
    # 4. synthesize_overview
    a = _make_agent(
        monkeypatch,
        [
            # 1. parse_brief returns "normalized"
            {"topic": "AI", "angle": "intro", "constraints": "short"},
            # 2. generate_queries returns one query
            {"queries": [{"query_text": "what is AI", "intent": "overview"}]},
            # 3. _evaluate_one_document (score+summarize in one call)
            {
                "relevance_score": 0.9,
                "authority_score": 0.8,
                "accuracy_score": 0.7,
                "type": "primary",
                "summary": "About AI.",
                "key_points": ["fact 1"],
            },
            # 4. synthesize_overview
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


def test_research_agent_run_resumes_from_empty_documents_checkpoint(monkeypatch, tmp_path) -> None:
    """A checkpoint with documents=[] (every candidate fetch failed or was
    rejected) must be treated as resumable, not re-fetched: `[]` and "no
    checkpoint" are different states, same as the candidates fix above.
    """
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="empty documents brief", max_results=3)
    cache.save_checkpoint(brief, "normalized", normalized={"topic": "cached"})
    cache.save_checkpoint(brief, "queries", queries=[{"query_text": "q", "intent": "overview"}])
    cache.save_checkpoint(
        brief,
        "candidates",
        candidates=[{"title": "Page", "url": "https://example.com/1", "rank": 1}],
    )
    cache.save_checkpoint(brief, "documents", documents=[])

    a = _make_agent(monkeypatch, [])
    a.cache = cache
    mock_fetcher = MagicMock()
    a.web_fetcher = mock_fetcher
    monkeypatch.setattr(ResearchAgent, "_fetch_academic_papers", lambda self, b: [])

    a.run(brief)

    mock_fetcher.fetch.assert_not_called()


def test_research_agent_run_skips_evaluation_when_scored_docs_and_references_cached(
    monkeypatch, tmp_path
) -> None:
    """A checkpoint with both scored_docs and references populated (the shape any
    pre- or post-merge checkpoint uses) must skip _evaluate_documents entirely on
    resume.

    Regression test: the tail-steps resume tests cache scored_docs=[]/references=[]
    alongside documents=[], so _evaluate_documents([], ...) would return ([], [])
    immediately even if the skip-check were broken (e.g. reverted to truthiness, or
    requiring only one of the two fields) — those tests can't actually catch that
    regression. Using non-empty, distinguishable cached data can.
    """
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="both cached", max_results=3)
    doc_dict = {"url": "https://example.com/cached-doc", "title": "Cached", "content": "c"}
    cache.save_checkpoint(brief, "normalized", normalized={"topic": "cached"})
    cache.save_checkpoint(brief, "queries", queries=[{"query_text": "q", "intent": "overview"}])
    cache.save_checkpoint(brief, "candidates", candidates=[])
    cache.save_checkpoint(brief, "documents", documents=[doc_dict])
    cache.save_checkpoint(
        brief,
        "scored_docs",
        scored_docs=[(doc_dict, 0.9, 0.8, 0.7, "primary")],
    )
    cache.save_checkpoint(
        brief,
        "references",
        references=[
            {
                "title": "Cached Ref",
                "url": "https://example.com/cached-doc",
                "domain": "example.com",
                "summary": "cached summary",
                "key_points": ["cached point"],
            }
        ],
    )

    a = _make_agent(monkeypatch, [])
    a.cache = cache
    monkeypatch.setattr(ResearchAgent, "_fetch_academic_papers", lambda self, b: [])
    monkeypatch.setattr(ResearchAgent, "_get_similar_topics", lambda self, b, refs: [])
    monkeypatch.setattr(ResearchAgent, "_synthesize_overview", lambda self, b, refs: "notes")

    eval_spy = MagicMock(side_effect=AssertionError("should not re-evaluate cached documents"))
    monkeypatch.setattr(ResearchAgent, "_evaluate_documents", eval_spy)

    out = a.run(brief)

    eval_spy.assert_not_called()
    assert [r.title for r in out.references] == ["Cached Ref"]


def test_research_agent_run_resumes_legacy_three_element_scored_docs_checkpoint(
    monkeypatch, tmp_path
) -> None:
    """A scored_docs checkpoint predating per-document authority/accuracy scoring
    -- each item shaped [doc_dict, score, type_label] instead of the current
    5-element [doc_dict, relevance, authority, accuracy, type_label] -- must still
    resume without _evaluate_documents re-running, converting the legacy items to
    the 5-tuple shape (authority/accuracy defaulted to 0.5) instead of raising.
    """
    import json

    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="legacy scored_docs", max_results=3)
    doc_dict = {"url": "https://example.com/legacy-doc", "title": "Legacy", "content": "c"}
    cache.save_checkpoint(brief, "normalized", normalized={"topic": "cached"})
    cache.save_checkpoint(brief, "queries", queries=[{"query_text": "q", "intent": "overview"}])
    cache.save_checkpoint(brief, "candidates", candidates=[])
    cache.save_checkpoint(brief, "documents", documents=[doc_dict])

    # AgentCache.save_checkpoint always normalizes scored_docs to the current
    # 5-element shape on save, so a true legacy 3-element item can now only exist
    # as literal on-disk data predating this format -- write it directly rather
    # than through save_checkpoint to reproduce that.
    cache_file = cache._cache_file(cache._cache_key(brief))
    state_data = json.loads(cache_file.read_text())
    state_data["scored_docs"] = [[doc_dict, 0.9, "primary"]]
    state_data["last_completed_step"] = "scored_docs"
    cache_file.write_text(json.dumps(state_data))

    cache.save_checkpoint(
        brief,
        "references",
        references=[
            {
                "title": "Legacy Ref",
                "url": "https://example.com/legacy-doc",
                "domain": "example.com",
                "summary": "legacy summary",
                "key_points": [],
            }
        ],
    )

    a = _make_agent(monkeypatch, [])
    a.cache = cache
    monkeypatch.setattr(ResearchAgent, "_fetch_academic_papers", lambda self, b: [])
    monkeypatch.setattr(ResearchAgent, "_get_similar_topics", lambda self, b, refs: [])
    monkeypatch.setattr(ResearchAgent, "_synthesize_overview", lambda self, b, refs: "notes")

    eval_spy = MagicMock(side_effect=AssertionError("should not re-evaluate cached documents"))
    monkeypatch.setattr(ResearchAgent, "_evaluate_documents", eval_spy)

    out = a.run(brief)  # must not raise reconstructing the legacy scored_docs shape

    eval_spy.assert_not_called()
    assert [r.title for r in out.references] == ["Legacy Ref"]


def test_research_agent_run_reevaluates_once_when_only_scored_docs_cached(
    monkeypatch, tmp_path
) -> None:
    """A checkpoint that completed scored_docs but not references (interrupted
    between the two sequential save_checkpoint calls in Step 5, or a pre-merge
    checkpoint from when scoring and summarizing were still separate steps) is not
    treated as resumable for Step 5: _evaluate_documents must run exactly once,
    recomputing both scored_docs and references together, rather than reusing the
    cached scored_docs or running evaluation more than once.
    """
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import ResearchBriefInput, ResearchReference

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="partial step 5 checkpoint", max_results=3)
    doc_dict = {"url": "https://example.com/partial-doc", "title": "Partial", "content": "c"}
    cache.save_checkpoint(brief, "normalized", normalized={"topic": "cached"})
    cache.save_checkpoint(brief, "queries", queries=[{"query_text": "q", "intent": "overview"}])
    cache.save_checkpoint(brief, "candidates", candidates=[])
    cache.save_checkpoint(brief, "documents", documents=[doc_dict])
    cache.save_checkpoint(
        brief,
        "scored_docs",
        scored_docs=[(doc_dict, 0.9, 0.8, 0.7, "primary")],
    )
    # references intentionally left unset.

    a = _make_agent(monkeypatch, [])
    a.cache = cache
    monkeypatch.setattr(ResearchAgent, "_fetch_academic_papers", lambda self, b: [])
    monkeypatch.setattr(ResearchAgent, "_get_similar_topics", lambda self, b, refs: [])
    monkeypatch.setattr(ResearchAgent, "_synthesize_overview", lambda self, b, refs: "notes")

    calls: list = []

    def fake_evaluate_one(self, doc, brief_input):
        calls.append(doc)
        ref = ResearchReference(
            title="Freshly evaluated", url=doc.url, domain=doc.domain, summary="s", key_points=[]
        )
        return (doc, 1.0, 1.0, 1.0, "primary", ref)

    monkeypatch.setattr(ResearchAgent, "_evaluate_one_document", fake_evaluate_one)

    out = a.run(brief)

    assert len(calls) == 1  # exactly one fresh evaluation pass, not zero and not two
    assert [r.title for r in out.references] == ["Freshly evaluated"]

    checkpoint = cache.load_checkpoint(brief)
    assert checkpoint.references is not None
    assert len(checkpoint.references) == 1


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


def test_research_agent_run_resumes_null_notes_checkpoint(monkeypatch, tmp_path) -> None:
    """A notes checkpoint saved as None (a legitimate _synthesize_overview outcome
    — no references, or an unusable LLM response) must still count as "already
    computed" on resume, not be mistaken for "never checkpointed" and recomputed.

    Regression test: cached_state.notes is None either way notes was never
    checkpointed or it legitimately completed with a null result, so the resume
    check must use a separate notes_computed marker rather than `notes is not
    None`, matching the "is not None" fix already applied to the list-typed
    steps (which don't have this ambiguity, since `[]` is distinguishable from
    unset)."""
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="resume null notes", max_results=3)
    cache.save_checkpoint(brief, "normalized", normalized={"topic": "cached"})
    cache.save_checkpoint(brief, "queries", queries=[{"query_text": "q", "intent": "overview"}])
    cache.save_checkpoint(brief, "candidates", candidates=[])
    cache.save_checkpoint(brief, "documents", documents=[])
    cache.save_checkpoint(brief, "scored_docs", scored_docs=[])
    cache.save_checkpoint(brief, "references", references=[])
    cache.save_checkpoint(brief, "notes", notes=None)
    cache.save_checkpoint(brief, "academic_papers", academic_papers=[])
    cache.save_checkpoint(brief, "similar_topics", similar_topics=[])

    a = _make_agent(monkeypatch, [])
    a.cache = cache

    overview_spy = MagicMock(side_effect=AssertionError("should not recompute overview"))
    monkeypatch.setattr(ResearchAgent, "_synthesize_overview", overview_spy)

    out = a.run(brief)

    overview_spy.assert_not_called()
    assert out.notes is None


def test_research_agent_run_checkpoints_siblings_when_notes_raises(monkeypatch, tmp_path) -> None:
    """When notes synthesis raises, the concurrently-computed academic_papers and
    similar_topics results must still be checkpointed rather than discarded.

    Regression test: collecting the three futures' results in sequence
    (`notes_future.result(); academic_future.result(); ...`) meant an exception
    from notes_future.result() skipped retrieving (and therefore saving) the other
    two futures' already-completed results, even though the ThreadPoolExecutor
    context manager waits for them to finish regardless. A Temporal retry would
    then repeat the arXiv/LLM calls for work that had, in fact, already succeeded.
    """
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import AcademicPaper, ResearchBriefInput

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="notes raises brief", max_results=3)
    cache.save_checkpoint(brief, "normalized", normalized={"topic": "cached"})
    cache.save_checkpoint(brief, "queries", queries=[{"query_text": "q", "intent": "overview"}])
    cache.save_checkpoint(brief, "candidates", candidates=[])
    cache.save_checkpoint(brief, "documents", documents=[])
    cache.save_checkpoint(brief, "scored_docs", scored_docs=[])
    cache.save_checkpoint(brief, "references", references=[])

    a = _make_agent(monkeypatch, [])
    a.cache = cache

    monkeypatch.setattr(
        ResearchAgent,
        "_synthesize_overview",
        lambda self, b, refs: (_ for _ in ()).throw(RuntimeError("LLM transport error")),
    )
    monkeypatch.setattr(
        ResearchAgent,
        "_fetch_academic_papers",
        lambda self, b: [
            AcademicPaper(
                title="Fresh Paper",
                url="https://arxiv.org/abs/9999",
                overview_or_summary="fresh abstract",
            )
        ],
    )
    monkeypatch.setattr(ResearchAgent, "_get_similar_topics", lambda self, b, refs: ["fresh topic"])

    with pytest.raises(RuntimeError, match="LLM transport error"):
        a.run(brief)

    checkpoint = cache.load_checkpoint(brief)
    assert checkpoint is not None
    assert checkpoint.notes is None
    assert checkpoint.academic_papers == [
        {
            "title": "Fresh Paper",
            "url": "https://arxiv.org/abs/9999",
            "overview_or_summary": "fresh abstract",
        }
    ]
    assert checkpoint.similar_topics == ["fresh topic"]


@pytest.mark.parametrize("wrap_in_event_loop_exception", [False, True])
def test_research_agent_similar_topics_cancellation_propagates(
    monkeypatch, tmp_path, wrap_in_event_loop_exception
) -> None:
    """A Temporal cancellation raised inside _get_similar_topics's Strands call must
    propagate out of run() rather than being swallowed by its broad
    "optional step, fail open" except block and turned into an empty result.

    Regression test for the Codex finding: run() returning normally on a
    cancellation would let run_planning() continue into planning instead of the
    job being recorded as cancelled. Covers both a bare CancelledError and one
    Strands wraps in EventLoopException (as a live Agent() call raises it)."""
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from strands.types.exceptions import EventLoopException
    from temporalio.exceptions import CancelledError

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="cancel during similar topics", max_results=3)
    cache.save_checkpoint(brief, "normalized", normalized={"topic": "cached"})
    cache.save_checkpoint(brief, "queries", queries=[{"query_text": "q", "intent": "overview"}])
    cache.save_checkpoint(brief, "candidates", candidates=[])
    cache.save_checkpoint(brief, "documents", documents=[])
    cache.save_checkpoint(brief, "scored_docs", scored_docs=[])
    cache.save_checkpoint(brief, "references", references=[])

    a = _make_agent(monkeypatch, [])
    a.cache = cache

    monkeypatch.setattr(ResearchAgent, "_synthesize_overview", lambda self, b, refs: "notes")
    monkeypatch.setattr(ResearchAgent, "_fetch_academic_papers", lambda self, b: [])

    cause = CancelledError("job cancelled")
    to_raise = EventLoopException(cause) if wrap_in_event_loop_exception else cause

    def _boom(self, b, refs):
        raise to_raise

    monkeypatch.setattr(ResearchAgent, "_get_similar_topics", _boom)

    expected_exc = EventLoopException if wrap_in_event_loop_exception else CancelledError
    with pytest.raises(expected_exc):
        a.run(brief)


def test_research_agent_academic_papers_cancellation_propagates(monkeypatch, tmp_path) -> None:
    """A Temporal cancellation during the arXiv search must propagate rather than
    being swallowed by _fetch_academic_papers's broad "best effort" except block."""
    from agents.blogging.blog_research_agent import agent as ra_mod
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from temporalio.exceptions import CancelledError

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="cancel during arxiv", max_results=3)
    cache.save_checkpoint(brief, "normalized", normalized={"topic": "cached"})
    cache.save_checkpoint(brief, "queries", queries=[{"query_text": "q", "intent": "overview"}])
    cache.save_checkpoint(brief, "candidates", candidates=[])
    cache.save_checkpoint(brief, "documents", documents=[])
    cache.save_checkpoint(brief, "scored_docs", scored_docs=[])
    cache.save_checkpoint(brief, "references", references=[])

    a = _make_agent(monkeypatch, [])
    a.cache = cache

    monkeypatch.setattr(ResearchAgent, "_synthesize_overview", lambda self, b, refs: "notes")
    monkeypatch.setattr(ResearchAgent, "_get_similar_topics", lambda self, b, refs: [])

    def boom(*args, **kwargs):
        raise CancelledError("job cancelled")

    monkeypatch.setattr(ra_mod, "search_arxiv", boom)

    with pytest.raises(CancelledError):
        a.run(brief)


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


def test_evaluate_one_document_cancellation_propagates(monkeypatch) -> None:
    """A Temporal cancellation during the combined per-document score+summarize
    call must propagate rather than being swallowed by the default-score/
    excerpt-fallback except block."""
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from strands.types.exceptions import EventLoopException
    from temporalio.exceptions import CancelledError

    from llm_service import DummyLLMClient

    a = ResearchAgent(llm_client=DummyLLMClient())
    cause = CancelledError("job cancelled")
    monkeypatch.setattr(
        ResearchAgent,
        "_call_json",
        lambda self, prompt: (_ for _ in ()).throw(EventLoopException(cause)),
    )

    with pytest.raises(EventLoopException):
        a._evaluate_one_document(_doc(0), ResearchBriefInput(brief="x", max_results=5))


def test_evaluate_documents_propagates_attribution_into_workers(monkeypatch) -> None:
    """Regression: the merged per-document fan-out must run inside a copy of the
    caller's context so LLM attribution / request-id reach the worker threads
    (raw ThreadPoolExecutor submission used to drop them)."""
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

    def fake_evaluate_one(self, doc, brief_input):
        seen.append((current_attribution().team, current_request_id()))
        ref = ResearchReference(
            title=doc.title, url=doc.url, domain=doc.domain, summary="s", key_points=[]
        )
        return (doc, 1.0, 1.0, 1.0, "blog", ref)

    monkeypatch.setattr(ResearchAgent, "_evaluate_one_document", fake_evaluate_one)

    docs = [_doc(0), _doc(1), _doc(2)]
    with llm_attribution(team="blogging"), bind_request_id("req-eval-123"):
        a._evaluate_documents(docs, ResearchBriefInput(brief="x", max_results=5))

    assert seen == [("blogging", "req-eval-123")] * 3


def test_evaluate_documents_calls_evaluate_one_document_once_per_doc(monkeypatch) -> None:
    """Regression: top-ranked documents (the ones that end up in both scored_docs
    and the capped references) must not cost a second _evaluate_one_document call
    - the whole point of merging _score_documents/_summarize_documents into one
    fan-out is that each document is evaluated exactly once."""
    from collections import Counter

    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.models import ResearchBriefInput, ResearchReference

    from llm_service import DummyLLMClient

    a = ResearchAgent(llm_client=DummyLLMClient())
    calls: list = []

    def fake_evaluate_one(self, doc, brief_input):
        # list.append is atomic under the GIL, unlike a dict/int += counter,
        # so this stays race-free across the fan-out's worker threads.
        calls.append(doc)
        ref = ResearchReference(
            title=doc.title, url=doc.url, domain=doc.domain, summary="s", key_points=[]
        )
        return (doc, 1.0, 1.0, 1.0, "blog", ref)

    monkeypatch.setattr(ResearchAgent, "_evaluate_one_document", fake_evaluate_one)

    docs = [_doc(i) for i in range(5)]
    scored_docs, references = a._evaluate_documents(
        docs, ResearchBriefInput(brief="x", max_results=2)
    )

    # Total count alone would miss a defect that double-evaluates one document
    # while skipping another (still 5 calls); compare per-document identity too.
    assert Counter(id(d) for d in calls) == Counter(id(d) for d in docs)
    assert len(calls) == 5
    assert len(scored_docs) == 5
    assert len(references) == 2
