"""Regression tests pinning ResearchAgent's per-run LLM and compaction call budget.

Unlike test_llm_request_count.py (which only exercises DummyLLMClient.request_count
in isolation), these tests drive ResearchAgent.run end-to-end so a regression that
reintroduces a second per-document LLM pass, or that breaks the compaction
memoization, fails loudly with a message naming which step's call count changed.
"""

from __future__ import annotations

from collections import Counter
from typing import List
from unittest.mock import MagicMock

from pydantic import HttpUrl

from .conftest import make_stub_fetcher_class, make_stub_llm_class, make_stub_search_class


def _doc(n: int, content_len: int):
    from agents.blogging.blog_research_agent.models import SourceDocument

    return SourceDocument(
        url=HttpUrl(f"https://example.com/budget-{n}"),
        title=f"Budget Doc {n}",
        content="x" * content_len,
        publish_date=None,
        domain="example.com",
        language="en",
        metadata={},
    )


def _make_counting_llm():
    """A DummyLLMClient (via make_stub_llm_class) that logs every complete_json call.

    stream() (what the Strands event loop actually calls for a bare
    Agent(model=...)) increments DummyLLMClient._request_count itself AND calls
    complete_json(...) internally, which increments it again -- one logical
    ResearchAgent._call_json call bumps request_count by 2, not 1. Logging
    complete_json invocations directly instead gives an exact 1:1 count against
    _call_json calls, since _call_json's Agent() never passes tools, so stream()
    always takes the plain complete_json(...) branch.
    """
    base = make_stub_llm_class()

    class _CountingLLM(base):
        def __init__(self) -> None:
            super().__init__()
            self.calls: List[str] = []

        def complete_json(self, prompt: str, **kwargs):
            # list.append is atomic under the GIL, so this stays race-free across
            # _evaluate_documents' parallel_map worker threads.
            self.calls.append(prompt)
            return super().complete_json(prompt, **kwargs)

    return _CountingLLM()


def _categorize(prompt: str) -> str:
    """Classify a logged prompt by the step that must have produced it.

    QUERY_GENERATION_PROMPT interpolates the parsed brief's core_topics/angle/
    constraints as labeled lines, so it also matches BRIEF_PARSING_PROMPT's own
    anchor tokens; the generate_queries check (on tokens unique to it: the
    literal quoted "queries" key and "query_text") must run first so the two
    steps aren't folded together.

    Every branch matches on tokens unique to one prompt (verified directly
    against blog_research_agent/prompts.py) and an unrecognized prompt raises
    rather than falling through to a default bucket, so a new LLM step added
    to ResearchAgent without a matching pattern here fails loudly and points
    at the actual unhandled prompt instead of silently inflating some other
    step's count.
    """
    lowered = prompt.lower()
    if '"queries"' in lowered and "query_text" in lowered:
        return "generate_queries"
    if "core_topics" in lowered and "angle" in lowered and "constraints" in lowered:
        return "parse_brief"
    if "relevance_score" in lowered and "summary:" in lowered and "key_points" in lowered:
        return "evaluate_document"
    if "similar_topics" in lowered and "similarity_score" in lowered:
        return "similar_topics"
    # FINAL_SYNTHESIS_PROMPT's own text never contains the word "overview" --
    # only QUERY_GENERATION_PROMPT's "intent" example does -- so this matches
    # on the two response-shape keys the prompt's "Response shape" section
    # actually asks for instead.
    if '"analysis"' in lowered and '"outline"' in lowered:
        return "synthesize_overview"
    raise AssertionError(
        f"Unrecognized LLM prompt step; add a pattern to _categorize:\n{prompt[:200]}"
    )


def _install_compact_spy(monkeypatch) -> List[bool]:
    """Replace agent.py's bound compact_text with a spy recording exceeded-budget calls.

    agent.py imports compact_text by name (`from llm_service import ... compact_text`)
    and calls the bare name, so the module attribute it already bound at import time
    must be patched here -- patching llm_service.compaction.compact_text directly
    would have no effect on agent.py's calls.
    """
    from agents.blogging.blog_research_agent import agent as ra_mod

    attempts: List[bool] = []

    def spy(text, max_chars, llm, content_description="content", **kwargs):
        text = text or ""
        exceeded = len(text) > max_chars
        attempts.append(exceeded)
        return text[:max_chars] if exceeded else text

    monkeypatch.setattr(ra_mod, "compact_text", spy)
    return attempts


def test_research_agent_run_call_budget(monkeypatch) -> None:
    """Pin the full per-run LLM and compaction call budget for a fresh (uncached) run.

    5 documents are fetched with max_results=2, so the pre-merge double-pass bug
    (score N docs, then re-summarize the top min(N, max_results)) would have cost
    5 + min(5, 2) = 7 document-level calls; the merged _evaluate_one_document must
    cost exactly N = 5.
    """
    from agents.blogging.blog_research_agent import agent as ra_mod
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    monkeypatch.setattr(ra_mod, "search_arxiv", lambda *a, **kw: [])
    attempts = _install_compact_spy(monkeypatch)

    docs = [
        _doc(0, 200),
        _doc(1, 200),
        _doc(2, 12000),
        _doc(3, 200),
        _doc(4, 12000),
    ]
    monkeypatch.setattr(
        ResearchAgent, "_fetch_documents", lambda self, candidates, brief_input: list(docs)
    )

    llm = _make_counting_llm()
    agent = ResearchAgent(
        llm_client=llm,
        web_search=make_stub_search_class()(),
        web_fetcher=make_stub_fetcher_class()(),
    )

    brief = ResearchBriefInput(brief="Budget test brief", max_results=2)
    out = agent.run(brief)

    counts = Counter(_categorize(p) for p in llm.calls)
    expected = {
        "parse_brief": 1,
        "generate_queries": 1,
        "evaluate_document": len(docs),
        "synthesize_overview": 1,
        "similar_topics": 1,
    }
    assert dict(counts) == expected, (
        f"LLM call budget regression: expected {expected}, got {dict(counts)}"
    )

    exceeded_count = sum(1 for exceeded in attempts if exceeded)
    fit_count = sum(1 for exceeded in attempts if not exceeded)
    assert exceeded_count == 2, f"expected 2 compaction attempts, got {exceeded_count}"
    assert fit_count == 3, f"expected 3 no-op compactions, got {fit_count}"

    assert len(out.references) == 2


def test_research_agent_run_resumes_full_checkpoint_zero_llm_calls(monkeypatch, tmp_path) -> None:
    """A checkpoint with every step populated must resume with zero LLM calls at all,
    proving the merged evaluation step's cache short-circuit still holds and that no
    tail step (overview/similar-topics) re-runs either."""
    from agents.blogging.blog_research_agent.agent import ResearchAgent
    from agents.blogging.blog_research_agent.agent_cache import AgentCache
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    monkeypatch.setenv("LLM_PROVIDER", "dummy")

    cache = AgentCache(tmp_path / "cache")
    brief = ResearchBriefInput(brief="fully cached budget test", max_results=2)
    doc_dict = {"url": "https://example.com/cached", "title": "Cached", "content": "c"}
    cache.save_checkpoint(brief, "normalized", normalized={"topic": "cached"})
    cache.save_checkpoint(brief, "queries", queries=[{"query_text": "q", "intent": "overview"}])
    cache.save_checkpoint(brief, "candidates", candidates=[])
    cache.save_checkpoint(brief, "documents", documents=[doc_dict])
    cache.save_checkpoint(brief, "scored_docs", scored_docs=[(doc_dict, 0.9, 0.8, 0.7, "primary")])
    cache.save_checkpoint(
        brief,
        "references",
        references=[
            {
                "title": "Cached Ref",
                "url": "https://example.com/cached",
                "domain": "example.com",
                "summary": "cached summary",
                "key_points": ["cached point"],
            }
        ],
    )
    cache.save_checkpoint(brief, "notes", notes="cached notes")
    cache.save_checkpoint(brief, "academic_papers", academic_papers=[])
    cache.save_checkpoint(brief, "similar_topics", similar_topics=["cached topic"])

    llm = _make_counting_llm()
    agent = ResearchAgent(llm_client=llm, cache=cache)

    eval_spy = MagicMock(
        side_effect=AssertionError("should not evaluate documents on full-checkpoint resume")
    )
    monkeypatch.setattr(ResearchAgent, "_evaluate_documents", eval_spy)
    fetch_spy = MagicMock(
        side_effect=AssertionError("should not fetch academic papers on full-checkpoint resume")
    )
    monkeypatch.setattr(ResearchAgent, "_fetch_academic_papers", fetch_spy)

    out = agent.run(brief)

    eval_spy.assert_not_called()
    fetch_spy.assert_not_called()
    assert llm.calls == [], f"expected zero LLM calls on full-checkpoint resume, got {llm.calls}"
    assert [r.title for r in out.references] == ["Cached Ref"]
