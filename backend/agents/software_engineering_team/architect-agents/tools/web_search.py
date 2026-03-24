"""Web search tool for current AWS pricing, service limits, and docs using Ollama web_search."""

from __future__ import annotations

import os

import httpx
from strands import tool


def _ollama_web_search_impl(query: str, max_results: int = 10) -> str:
    """Use Ollama web_search API (https://ollama.com/api/web_search)."""
    api_key = os.environ.get("OLLAMA_API_KEY")
    if not api_key:
        return "Ollama web search not available: OLLAMA_API_KEY is not set (e.g. from https://ollama.com/settings/keys)."

    base_url = (os.environ.get("OLLAMA_WEB_SEARCH_BASE_URL") or "https://ollama.com/api").rstrip(
        "/"
    )
    url = f"{base_url}/web_search"
    payload = {"query": query, "max_results": min(max_results, 10)}
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        return f"Web search request failed: {e}"

    if resp.status_code != 200:
        return f"Ollama web search failed with status {resp.status_code}: {resp.text}"

    data = resp.json()
    raw_results = data.get("results", []) or []
    if not raw_results:
        return "No results found."

    lines = []
    for idx, item in enumerate(raw_results, start=1):
        title = item.get("title") or "Untitled"
        url_str = item.get("url") or ""
        content = (item.get("content") or "")[:500]
        lines.append(f"{idx}. {title}\n   URL: {url_str}\n   {content}")
    return "\n\n".join(lines)


@tool
def web_search_tool(query: str, search_depth: str = "basic") -> str:
    """Search the web for current information on AWS services, pricing, and limits.

    Use this to check current AWS pricing, service availability, new service
    releases, and documentation. Helpful for validating architecture
    recommendations against up-to-date information.

    Args:
        query: Search query (e.g. "AWS Lambda pricing 2025",
            "Amazon RDS Multi-AZ cost", "Bedrock Claude model availability").
        search_depth: Kept for compatibility; Ollama web_search does not use this.

    Returns:
        Search results as formatted text. Returns an error message if
        OLLAMA_API_KEY is not set.
    """
    return _ollama_web_search_impl(query, max_results=10)
