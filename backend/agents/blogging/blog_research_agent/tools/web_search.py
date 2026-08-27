from __future__ import annotations

import logging
import os
import time
from typing import List

import httpx
from pydantic import HttpUrl

from llm_service.interface import LLMRateLimitError, LLMTemporaryError

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
        429/5xx responses and connection errors are retried locally (same backoff
        budget, WEB_SEARCH_MAX_RETRIES attempts) before raising, so a short-lived
        outage usually resolves within this call. Raises LLMRateLimitError once a
        429 outlasts that budget, LLMTemporaryError once a 5xx or connection error
        does (both transient — for callers that additionally funnel this through a
        Temporal retry policy), or WebSearchError on any other API/network failure
        (missing API key, other non-200 status, a non-retryable httpx error).
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
                    continue
                # A connection outage that outlasts the local retry budget is just
                # as transient as a 5xx response (the two are indistinguishable to
                # the caller), so it gets the same LLMTemporaryError classification
                # rather than a terminal WebSearchError/ResearchError.
                raise LLMTemporaryError(
                    f"Ollama web search connection error after {WEB_SEARCH_MAX_RETRIES + 1} attempts: {exc}"
                ) from exc
            except httpx.HTTPError as exc:
                raise WebSearchError(f"HTTP error during Ollama web search: {exc}") from exc

            if resp.status_code == 200:
                break
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                # Retry transient upstream failures locally first — same backoff loop
                # as the connection-error path above — so a short-lived rate limit or
                # server blip resolves within this single search() call instead of
                # relying on a Temporal activity retry (which thread-mode callers
                # don't get at all: neither _run_pipeline_with_tracking nor the
                # synchronous pipeline route retries a raised LLMRateLimitError/
                # LLMTemporaryError, so exhausting this budget is genuinely terminal
                # for them, not just deferred to an outer retry layer).
                if attempt < WEB_SEARCH_MAX_RETRIES:
                    wait = WEB_SEARCH_BACKOFF_BASE**attempt
                    logger.warning(
                        "Web search transient status %d (attempt %d/%d). Retrying in %.1fs",
                        resp.status_code,
                        attempt + 1,
                        WEB_SEARCH_MAX_RETRIES + 1,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                # Exhausted local retries: classified the same way the Ollama LLM
                # client classifies its own 429/5xx responses, so Temporal-mode
                # callers that funnel research through the activity retry policy
                # still get a further outer retry.
                if resp.status_code == 429:
                    raise LLMRateLimitError(
                        f"Ollama web search rate limited (429) after "
                        f"{WEB_SEARCH_MAX_RETRIES + 1} attempts: {resp.text}",
                        status_code=429,
                    )
                raise LLMTemporaryError(
                    f"Ollama web search server error {resp.status_code} after "
                    f"{WEB_SEARCH_MAX_RETRIES + 1} attempts: {resp.text}",
                    status_code=resp.status_code,
                )
            raise WebSearchError(
                f"Ollama web search failed with status {resp.status_code}: {resp.text}"
            )

        assert resp is not None and resp.status_code == 200

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
