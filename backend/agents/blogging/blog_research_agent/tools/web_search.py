from __future__ import annotations

import logging
import os
import time
from typing import List

import httpx
from pydantic import HttpUrl

from ..models import CandidateResult, SearchQuery

logger = logging.getLogger(__name__)


class WebSearchError(RuntimeError):
    """Raised when the web search tool fails."""


# Ollama web search allows max 10 results per request
OLLAMA_WEB_SEARCH_MAX_RESULTS = 10

# Retries for transient connection/SSL errors
WEB_SEARCH_MAX_RETRIES = 3
WEB_SEARCH_BACKOFF_BASE = 2.0


class OllamaWebSearch:
    """
    Web search using Ollama's web_search API (https://ollama.com/api/web_search).

    Uses OLLAMA_API_KEY for authentication. Optional base_url for self-hosted
    or alternate endpoints (default: https://ollama.com/api).
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("OLLAMA_API_KEY")
        self.base_url = (
            base_url or os.environ.get("OLLAMA_WEB_SEARCH_BASE_URL") or "https://ollama.com/api"
        ).rstrip("/")
        self.timeout = timeout

    def search(
        self,
        query: SearchQuery,
        *,
        max_results: int,
        recency_preference: str | None = "latest",
    ) -> List[CandidateResult]:
        """
        Execute an Ollama web search for a single query.

        Parameters
        ----------
        query
            SearchQuery describing the query text and high-level intent.
        max_results
            Maximum number of results to return (capped at 10 per Ollama API).
        recency_preference
            Ignored by Ollama web search; kept for interface compatibility.

        Returns
        -------
        List of CandidateResult, length at most max_results.
        Raises WebSearchError on API or network failure.
        """
        assert max_results >= 1, "max_results must be at least 1"
        limit = min(max_results, OLLAMA_WEB_SEARCH_MAX_RESULTS)

        if not self.api_key:
            raise WebSearchError(
                "OLLAMA_API_KEY is not set. Web search requires an Ollama API key "
                "(e.g. from https://ollama.com/settings/keys)."
            )

        url = f"{self.base_url}/web_search"
        payload = {"query": query.query_text, "max_results": limit}
        headers = {"Authorization": f"Bearer {self.api_key}"}

        resp: httpx.Response | None = None
        for attempt in range(WEB_SEARCH_MAX_RETRIES + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(url, json=payload, headers=headers)
                break
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                if attempt < WEB_SEARCH_MAX_RETRIES:
                    wait = WEB_SEARCH_BACKOFF_BASE**attempt
                    logger.warning(
                        "Web search connection error (attempt %d/%d): %s. Retrying in %.1fs",
                        attempt + 1,
                        WEB_SEARCH_MAX_RETRIES + 1,
                        type(exc).__name__,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    raise WebSearchError(
                        f"HTTP error during Ollama web search after {WEB_SEARCH_MAX_RETRIES + 1} attempts: {exc}"
                    ) from exc
            except httpx.HTTPError as exc:
                raise WebSearchError(f"HTTP error during Ollama web search: {exc}") from exc

        if resp is None:
            raise WebSearchError("Ollama web search failed: no response after retries")
        if resp.status_code != 200:
            raise WebSearchError(
                f"Ollama web search failed with status {resp.status_code}: {resp.text}"
            )

        data = resp.json()
        raw_results = data.get("results", []) or []

        candidates: List[CandidateResult] = []
        for idx, item in enumerate(raw_results[:limit], start=1):
            url_str = item.get("url")
            title = item.get("title") or url_str or "Untitled"
            content = item.get("content") or ""
            if not url_str:
                continue
            candidates.append(
                CandidateResult(
                    title=title,
                    url=HttpUrl(url_str),
                    snippet=content or None,
                    source="ollama",
                    rank=idx,
                )
            )
        return candidates
