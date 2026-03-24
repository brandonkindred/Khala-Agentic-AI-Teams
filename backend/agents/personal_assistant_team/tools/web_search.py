"""Web search tool using Ollama web_search API."""

from __future__ import annotations

import logging
import os
from typing import List, Optional

import httpx
from pydantic import BaseModel, HttpUrl

logger = logging.getLogger(__name__)


class WebSearchError(Exception):
    """Raised when web search fails."""


class SearchResult(BaseModel):
    """A single search result."""

    title: str
    url: HttpUrl
    snippet: str
    source: str = "ollama"
    rank: int = 0


# Ollama web_search allows max 10 results per request
OLLAMA_WEB_SEARCH_MAX_RESULTS = 10


class WebSearchTool:
    """
    Web search tool using Ollama's web_search API (https://ollama.com/api/web_search).

    Used for finding deals, researching venues, and general information lookup.
    Uses OLLAMA_API_KEY for authentication.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        """
        Initialize the web search tool.

        Args:
            api_key: Ollama API key (defaults to OLLAMA_API_KEY env var)
            base_url: Ollama API base URL (defaults to https://ollama.com/api)
            timeout: Request timeout in seconds
        """
        self.api_key = api_key or os.environ.get("OLLAMA_API_KEY")
        self.base_url = (
            base_url or os.environ.get("OLLAMA_WEB_SEARCH_BASE_URL") or "https://ollama.com/api"
        ).rstrip("/")
        self.timeout = timeout
        if not self.api_key:
            logger.warning("OLLAMA_API_KEY not set. Web search will fail.")

    def search(
        self,
        query: str,
        max_results: int = 10,
        search_depth: str = "basic",
        include_domains: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
    ) -> List[SearchResult]:
        """
        Execute a web search via Ollama web_search API.

        Args:
            query: Search query
            max_results: Maximum results to return (capped at 10 per Ollama API)
            search_depth: Ignored; kept for interface compatibility
            include_domains: Ignored by Ollama; kept for interface compatibility
            exclude_domains: Ignored by Ollama; kept for interface compatibility

        Returns:
            List of SearchResult objects
        """
        if not self.api_key:
            raise WebSearchError(
                "OLLAMA_API_KEY is not set. Web search requires an Ollama API key "
                "(e.g. from https://ollama.com/settings/keys)."
            )

        limit = min(max_results, OLLAMA_WEB_SEARCH_MAX_RESULTS)
        url = f"{self.base_url}/web_search"
        payload = {"query": query, "max_results": limit}
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as e:
            logger.error("Web search failed: %s", e)
            raise WebSearchError(f"Search request failed: {e}") from e

        results = []
        raw_results = data.get("results", []) or []
        for idx, item in enumerate(raw_results[:limit], start=1):
            url_str = item.get("url")
            if not url_str:
                continue
            results.append(
                SearchResult(
                    title=item.get("title") or url_str or "Untitled",
                    url=HttpUrl(url_str),
                    snippet=item.get("content", ""),
                    source="ollama",
                    rank=idx,
                )
            )
        return results

    def search_deals(
        self,
        product: str,
        max_results: int = 10,
    ) -> List[SearchResult]:
        """
        Search for deals on a specific product.

        Args:
            product: Product to search for
            max_results: Maximum results

        Returns:
            List of deal results
        """
        query = f"{product} deals discounts sale"
        return self.search(query=query, max_results=max_results)

    def search_restaurants(
        self,
        cuisine: str,
        location: str,
        max_results: int = 10,
    ) -> List[SearchResult]:
        """
        Search for restaurants.

        Args:
            cuisine: Type of cuisine
            location: Location/area
            max_results: Maximum results

        Returns:
            List of restaurant results
        """
        query = f"best {cuisine} restaurants in {location}"
        return self.search(query=query, max_results=max_results)

    def search_services(
        self,
        service_type: str,
        location: str,
        max_results: int = 10,
    ) -> List[SearchResult]:
        """
        Search for local services.

        Args:
            service_type: Type of service (e.g., "plumber", "dentist")
            location: Location/area
            max_results: Maximum results

        Returns:
            List of service provider results
        """
        query = f"{service_type} near {location} reviews ratings"
        return self.search(query=query, max_results=max_results)
