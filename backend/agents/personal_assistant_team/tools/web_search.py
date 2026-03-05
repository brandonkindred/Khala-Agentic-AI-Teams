"""Web search tool using Tavily API."""

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
    source: str = "tavily"
    rank: int = 0


class WebSearchTool:
    """
    Web search tool using Tavily API.
    
    Used for finding deals, researching venues, and general information lookup.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://api.tavily.com/search",
        timeout: float = 15.0,
    ) -> None:
        """
        Initialize the web search tool.
        
        Args:
            api_key: Tavily API key (defaults to TAVILY_API_KEY env var)
            base_url: Tavily API base URL
            timeout: Request timeout in seconds
        """
        self.api_key = api_key or os.getenv("TAVILY_API_KEY")
        if not self.api_key:
            logger.warning("TAVILY_API_KEY not set. Web search will fail.")
        
        self.base_url = base_url
        self.timeout = timeout

    def search(
        self,
        query: str,
        max_results: int = 10,
        search_depth: str = "basic",
        include_domains: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
    ) -> List[SearchResult]:
        """
        Execute a web search.
        
        Args:
            query: Search query
            max_results: Maximum results to return
            search_depth: "basic" or "advanced"
            include_domains: Only include results from these domains
            exclude_domains: Exclude results from these domains
            
        Returns:
            List of SearchResult objects
        """
        if not self.api_key:
            raise WebSearchError("TAVILY_API_KEY is not set")
        
        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
        }
        
        if include_domains:
            payload["include_domains"] = include_domains
        if exclude_domains:
            payload["exclude_domains"] = exclude_domains
        
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(self.base_url, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as e:
            logger.error("Web search failed: %s", e)
            raise WebSearchError(f"Search request failed: {e}") from e
        
        results = []
        for idx, item in enumerate(data.get("results", [])[:max_results], start=1):
            url = item.get("url")
            if not url:
                continue
            
            results.append(SearchResult(
                title=item.get("title", "Untitled"),
                url=HttpUrl(url),
                snippet=item.get("content", item.get("snippet", "")),
                source="tavily",
                rank=idx,
            ))
        
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
        
        return self.search(
            query=query,
            max_results=max_results,
            search_depth="advanced",
            include_domains=[
                "slickdeals.net",
                "retailmenot.com",
                "dealnews.com",
                "amazon.com",
                "walmart.com",
                "target.com",
                "bestbuy.com",
            ],
        )

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
        
        return self.search(
            query=query,
            max_results=max_results,
            search_depth="advanced",
            include_domains=[
                "yelp.com",
                "tripadvisor.com",
                "opentable.com",
                "google.com/maps",
            ],
        )

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
        
        return self.search(
            query=query,
            max_results=max_results,
            search_depth="advanced",
        )
