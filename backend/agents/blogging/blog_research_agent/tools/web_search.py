from __future__ import annotations

import os
from typing import List

import httpx
from pydantic import HttpUrl

from ..models import SearchQuery, CandidateResult


class WebSearchError(RuntimeError):
    """Raised when the web search tool fails."""


class TavilyWebSearch:
    """
    Simple wrapper around the Tavily search API.

    This is intentionally minimal and can be replaced or extended
    with other providers if needed.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = "https://api.tavily.com/search",
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key or os.getenv("TAVILY_API_KEY")
        if not self.api_key:
            raise WebSearchError("TAVILY_API_KEY is not set in the environment.")
        self.base_url = base_url
        self.timeout = timeout

    def search(
        self,
        query: SearchQuery,
        *,
        max_results: int,
        recency_preference: str | None = "latest",
    ) -> List[CandidateResult]:
        """
        Execute a Tavily search for a single query.

        Parameters
        ----------
        query:
            SearchQuery describing the query text and high-level intent.
        max_results:
            Maximum number of results to return.
        recency_preference:
            Passed through to Tavily `search_depth` / `topic` / `time_range`
            as a simple hint. This adapter keeps things basic.

        Preconditions:
            - max_results >= 1.
            - recency_preference is None or one of the supported values (e.g. "latest_12_months", "no_preference").
        Postconditions:
            - Returns a list of CandidateResult of length at most max_results.
            - Raises WebSearchError on API or network failure.
        """
        assert max_results >= 1, "max_results must be at least 1"
        payload = {
            "api_key": self.api_key,
            "query": query.query_text,
            "max_results": max_results,
            "search_depth": "advanced",
        }

        if recency_preference == "latest_12_months":
            payload["time_range"] = "year"

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(self.base_url, json=payload)
        except httpx.HTTPError as exc:
            raise WebSearchError(f"HTTP error during Tavily search: {exc}") from exc

        if resp.status_code != 200:
            raise WebSearchError(
                f"Tavily search failed with status {resp.status_code}: {resp.text}"
            )

        data = resp.json()
        raw_results = data.get("results", []) or []

        candidates: List[CandidateResult] = []
        for idx, item in enumerate(raw_results[:max_results], start=1):
            url = item.get("url")
            title = item.get("title") or url or "Untitled"
            snippet = item.get("content") or item.get("snippet")
            if not url:
                continue

            candidates.append(
                CandidateResult(
                    title=title,
                    url=HttpUrl(url),
                    snippet=snippet,
                    source="tavily",
                    rank=idx,
                )
            )

        return candidates

