"""
Trend discovery agent: identifies the top 3 trending social media topics
from the last 24 hours by running parallel web searches and synthesizing
results with an LLM.

No platform API keys required — uses the existing OllamaWebSearch tool.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import List, Optional

from blog_research_agent.models import CandidateResult, SearchQuery
from blog_research_agent.tools.web_search import OllamaWebSearch, WebSearchError

from llm_service import LLMClient, LLMJsonParseError

from .trend_models import TrendDigest, TrendingTopic

logger = logging.getLogger(__name__)

PLATFORMS_SEARCHED = [
    "X/Twitter",
    "LinkedIn",
    "TikTok",
    "Instagram",
    "Facebook",
    "Bluesky",
    "Reddit",
    "Threads",
]

_SYNTHESIS_PROMPT = """\
You are a social media analyst. Below are web search snippets collected from searches about trending topics \
on social media platforms in the last 24 hours. Based only on these results, identify the top 3 trending topics.

For each topic return:
- title: concise headline (5-15 words)
- summary: 2-3 sentences explaining the trend and why it is gaining traction
- platforms: list of platform names where this trend was observed (e.g. ["X/Twitter", "TikTok"])
- sources: up to 3 URLs from the snippets most relevant to this topic
- relevance_score: estimated trend strength from 0.0 to 1.0 (1.0 = extremely viral)

Return exactly this JSON (no extra keys, no markdown):
{{"topics": [
  {{"title": "...", "summary": "...", "platforms": [...], "sources": [...], "relevance_score": 0.0}},
  {{"title": "...", "summary": "...", "platforms": [...], "sources": [...], "relevance_score": 0.0}},
  {{"title": "...", "summary": "...", "platforms": [...], "sources": [...], "relevance_score": 0.0}}
]}}

If fewer than 3 clear trends are identifiable, return only the ones you can confidently identify.

--- SEARCH RESULTS ---
{snippets}
"""


def _build_queries(date_str: str) -> List[SearchQuery]:
    """Build one targeted search query per major platform plus a general sweep."""
    return [
        SearchQuery(query_text=f"trending social media topics today {date_str}", intent="general"),
        SearchQuery(query_text=f"viral content trending X Twitter {date_str}", intent="x_twitter"),
        SearchQuery(query_text=f"LinkedIn trending discussions {date_str}", intent="linkedin"),
        SearchQuery(query_text=f"TikTok trending hashtags videos {date_str}", intent="tiktok"),
        SearchQuery(query_text=f"Instagram trending reels {date_str}", intent="instagram"),
        SearchQuery(query_text=f"Facebook trending topics {date_str}", intent="facebook"),
        SearchQuery(query_text=f"Bluesky trending posts {date_str}", intent="bluesky"),
        SearchQuery(query_text=f"Reddit viral posts social media {date_str}", intent="reddit"),
    ]


def _format_snippets(results: List[CandidateResult]) -> str:
    """Format search results into a compact text block for the LLM prompt."""
    lines = []
    for r in results:
        url = str(r.url)
        snippet = (r.snippet or "").strip()[:400]
        lines.append(f"[{r.title}] ({url})\n{snippet}")
    return "\n\n".join(lines)


class TrendDiscoveryAgent:
    """
    Discovers the top 3 trending social media topics from the last 24 hours.

    Uses parallel web searches across major platforms and an LLM to synthesize
    results into a structured TrendDigest. Requires OLLAMA_API_KEY (already used
    by the rest of the platform).
    """

    def __init__(self, llm_client: LLMClient, web_search: OllamaWebSearch) -> None:
        if llm_client is None:
            raise ValueError("llm_client is required")
        if web_search is None:
            raise ValueError("web_search is required")
        self.llm = llm_client
        self.web_search = web_search

    def _search_one(self, query: SearchQuery) -> List[CandidateResult]:
        """Execute a single search query; returns empty list on failure."""
        try:
            results = self.web_search.search(query, max_results=10)
            logger.debug("Search '%s' returned %d results", query.query_text, len(results))
            return results
        except WebSearchError as exc:
            logger.warning("Web search failed for query '%s': %s", query.query_text, exc)
            return []

    def run(self, date: Optional[str] = None) -> TrendDigest:
        """
        Run trend discovery for the given date (defaults to today UTC).

        Returns a TrendDigest with up to 3 trending topics. Never raises —
        if all searches or the LLM call fail, returns an empty digest with a log warning.
        """
        generated_at = datetime.now(tz=timezone.utc).isoformat()
        date_str = date or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        queries = _build_queries(date_str)

        logger.info("TrendDiscoveryAgent: running %d searches for date=%s", len(queries), date_str)

        # Run all searches in parallel
        all_results: List[CandidateResult] = []
        seen_urls: set[str] = set()
        with ThreadPoolExecutor(max_workers=len(queries)) as executor:
            futures = {executor.submit(self._search_one, q): q for q in queries}
            for future in as_completed(futures):
                for result in future.result():
                    url_str = str(result.url)
                    if url_str not in seen_urls:
                        seen_urls.add(url_str)
                        all_results.append(result)

        logger.info(
            "TrendDiscoveryAgent: collected %d unique results across %d queries",
            len(all_results),
            len(queries),
        )

        if not all_results:
            logger.warning(
                "TrendDiscoveryAgent: all searches returned no results; returning empty digest"
            )
            return TrendDigest(
                generated_at=generated_at,
                topics=[],
                platforms_searched=PLATFORMS_SEARCHED,
                search_query_count=len(queries),
            )

        snippets_text = _format_snippets(all_results)
        prompt = _SYNTHESIS_PROMPT.format(snippets=snippets_text[:60_000])  # stay within context

        topics: List[TrendingTopic] = []
        try:
            data = self.llm.complete_json(prompt, temperature=0.2)
            raw_topics = data.get("topics", []) if isinstance(data, dict) else []
            for item in raw_topics[:3]:
                if not isinstance(item, dict):
                    continue
                title = (item.get("title") or "").strip()
                summary = (item.get("summary") or "").strip()
                if not title or not summary:
                    continue
                platforms = [str(p) for p in (item.get("platforms") or []) if p]
                sources = [str(s) for s in (item.get("sources") or []) if s][:3]
                score = float(item.get("relevance_score") or 0.0)
                score = max(0.0, min(1.0, score))
                topics.append(
                    TrendingTopic(
                        title=title,
                        summary=summary,
                        platforms=platforms,
                        sources=sources,
                        relevance_score=score,
                    )
                )
        except (LLMJsonParseError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "TrendDiscoveryAgent: LLM synthesis failed: %s; returning empty topic list", exc
            )

        logger.info("TrendDiscoveryAgent: identified %d trending topics", len(topics))
        return TrendDigest(
            generated_at=generated_at,
            topics=topics,
            platforms_searched=PLATFORMS_SEARCHED,
            search_query_count=len(queries),
        )
