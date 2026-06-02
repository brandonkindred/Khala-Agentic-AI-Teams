"""Minimal Ollama web-search client for job discovery.

Modeled on the proven ``blogging/blog_research_agent/tools/web_search.py``
(same endpoint, ``OLLAMA_API_KEY`` auth, retry/backoff), but self-contained
and returning a plain team-local :class:`SearchResult` so this team has no
cross-team model coupling and stays trivially mockable in tests.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import List

import httpx

logger = logging.getLogger(__name__)


class WebSearchError(RuntimeError):
    """Raised when the web search tool fails."""


# Ollama web search allows at most 10 results per request.
OLLAMA_WEB_SEARCH_MAX_RESULTS = 10
WEB_SEARCH_MAX_RETRIES = 3
WEB_SEARCH_BACKOFF_BASE = 2.0


@dataclass
class SearchResult:
    """A single web search hit."""

    title: str
    url: str
    snippet: str = ""
    source: str = "ollama"
    rank: int = 0


class OllamaWebSearch:
    """Web search via Ollama's ``web_search`` API.

    Invariants:
        * Network calls are only attempted when an API key is available;
          otherwise :meth:`search` raises ``WebSearchError`` before any I/O.
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

    def search(self, query_text: str, *, max_results: int = 10) -> List[SearchResult]:
        """Run a single web search query.

        Preconditions:
            * ``query_text`` is a non-empty string.
            * ``max_results >= 1``.
        Postconditions:
            * Returns at most ``min(max_results, 10)`` results, each with a
              non-empty ``url``, ranked from 1.
        Raises:
            WebSearchError: on missing API key, non-200 response, or repeated
                transport failure.
        """
        assert query_text and query_text.strip(), "query_text must be non-empty"
        assert max_results >= 1, "max_results must be at least 1"
        limit = min(max_results, OLLAMA_WEB_SEARCH_MAX_RESULTS)

        if not self.api_key:
            raise WebSearchError(
                "OLLAMA_API_KEY is not set. Job web search requires an Ollama API key "
                "(see https://ollama.com/settings/keys)."
            )

        url = f"{self.base_url}/web_search"
        payload = {"query": query_text, "max_results": limit}
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
                        f"HTTP error during Ollama web search after "
                        f"{WEB_SEARCH_MAX_RETRIES + 1} attempts: {exc}"
                    ) from exc
            except httpx.HTTPError as exc:
                raise WebSearchError(f"HTTP error during Ollama web search: {exc}") from exc

        if resp is None:  # pragma: no cover - defensive; loop always sets or raises
            raise WebSearchError("Ollama web search failed: no response after retries")
        if resp.status_code != 200:
            raise WebSearchError(
                f"Ollama web search failed with status {resp.status_code}: {resp.text}"
            )

        raw_results = (self._parse_json(resp) or {}).get("results", []) or []
        results: List[SearchResult] = []
        for idx, item in enumerate(raw_results[:limit], start=1):
            url_str = item.get("url")
            if not url_str:
                continue
            results.append(
                SearchResult(
                    title=item.get("title") or url_str,
                    url=url_str,
                    snippet=item.get("content") or "",
                    source="ollama",
                    rank=idx,
                )
            )
        return results

    @staticmethod
    def _parse_json(resp: httpx.Response) -> dict:
        """Decode a 200 response body as a JSON object, surfacing a clean error.

        Preconditions:
            * ``resp`` is a 2xx response whose body should be a JSON object.
        Postconditions:
            * Returns a ``dict`` (``{}`` for an empty body). A non-object body
              (list, scalar, ``null``) raises ``WebSearchError`` rather than
              letting a downstream ``.get`` raise ``AttributeError``.
        Raises:
            WebSearchError: when the body is not valid JSON, or decodes to
                something other than an object, so callers see the tool's
                documented error type rather than a raw decode/attribute error.
        """
        try:
            body = resp.json()
        except ValueError as exc:
            raise WebSearchError(
                f"Ollama web search returned status 200 but a non-JSON body: {exc}"
            ) from exc
        if body is None:
            return {}
        if not isinstance(body, dict):
            raise WebSearchError(
                f"Ollama web search returned a non-object JSON body: {type(body).__name__}"
            )
        return body
