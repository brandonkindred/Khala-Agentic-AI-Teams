"""Web search tool for current AWS pricing, service limits, and docs."""

from __future__ import annotations

from strands import tool


def _tavily_search_impl(query: str, search_depth: str = "basic") -> str:
    """Use Tavily via strands-agents-tools if available."""
    try:
        from strands_tools import tavily_search

        result = tavily_search(query=query, search_depth=search_depth)
        if hasattr(result, "message"):
            return str(result.message)
        return str(result)
    except ImportError:
        return "Tavily not available (install strands-agents-tools with tavily extras)"


def _http_fetch_impl(url: str) -> str:
    """Fetch URL content via strands-agents-tools http_request if available."""
    try:
        from strands_tools import http_request

        result = http_request(method="GET", url=url)
        if hasattr(result, "message"):
            return str(result.message)
        return str(result)
    except ImportError:
        return "http_request not available (install strands-agents-tools)"


@tool
def web_search_tool(query: str, search_depth: str = "basic") -> str:
    """Search the web for current information on AWS services, pricing, and limits.

    Use this to check current AWS pricing, service availability, new service
    releases, and documentation. Helpful for validating architecture
    recommendations against up-to-date information.

    Args:
        query: Search query (e.g. "AWS Lambda pricing 2025",
            "Amazon RDS Multi-AZ cost", "Bedrock Claude model availability").
        search_depth: "basic" for quick results, "advanced" for deeper search.

    Returns:
        Search results or fetched content. Returns an error message if
        the search backend (Tavily) is not configured.
    """
    return _tavily_search_impl(query, search_depth)
