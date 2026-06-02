"""Web search and fetch tools for the job matching team."""

from .web_fetch import FetchedPage, WebFetcher, WebFetchError
from .web_search import OllamaWebSearch, SearchResult, WebSearchError

__all__ = [
    "FetchedPage",
    "WebFetchError",
    "WebFetcher",
    "OllamaWebSearch",
    "SearchResult",
    "WebSearchError",
]
