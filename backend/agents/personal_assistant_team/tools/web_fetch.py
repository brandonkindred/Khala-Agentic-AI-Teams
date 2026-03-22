"""Web page fetcher for content extraction."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, HttpUrl

logger = logging.getLogger(__name__)


class WebFetchError(Exception):
    """Raised when web fetching fails."""


class FetchedPage(BaseModel):
    """A fetched web page."""

    url: HttpUrl
    title: str
    content: str
    domain: str


class WebFetchTool:
    """
    Tool for fetching and extracting content from web pages.
    """

    def __init__(
        self,
        timeout: float = 30.0,
        max_content_length: int = 100000,
    ) -> None:
        """
        Initialize the web fetch tool.
        
        Args:
            timeout: Request timeout in seconds
            max_content_length: Maximum content length to return
        """
        self.timeout = timeout
        self.max_content_length = max_content_length
        
        self.headers = {
            "User-Agent": "Mozilla/5.0 (compatible; PersonalAssistant/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

    def fetch(self, url: str | HttpUrl) -> FetchedPage:
        """
        Fetch and extract content from a URL.
        
        Args:
            url: URL to fetch
            
        Returns:
            FetchedPage with extracted content
        """
        url_str = str(url)
        parsed = urlparse(url_str)
        
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                response = client.get(url_str, headers=self.headers)
                response.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("Failed to fetch %s: %s", url_str, e)
            raise WebFetchError(f"Failed to fetch URL: {e}") from e
        
        content_type = response.headers.get("content-type", "")
        
        if "text/html" in content_type:
            title, content = self._extract_html(response.text)
        elif "text/plain" in content_type:
            title = ""
            content = response.text
        else:
            title = ""
            content = response.text[:self.max_content_length]
        
        return FetchedPage(
            url=HttpUrl(url_str),
            title=title or parsed.netloc,
            content=content[:self.max_content_length],
            domain=parsed.netloc,
        )

    def _extract_html(self, html: str) -> tuple[str, str]:
        """Extract title and text content from HTML."""
        title_match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
        title = title_match.group(1).strip() if title_match else ""
        
        text = html
        
        text = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"<!--[\s\S]*?-->", " ", text)
        text = re.sub(r"<nav[^>]*>[\s\S]*?</nav>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"<footer[^>]*>[\s\S]*?</footer>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"<header[^>]*>[\s\S]*?</header>", " ", text, flags=re.IGNORECASE)
        
        text = re.sub(r"<[^>]+>", " ", text)
        
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        text = re.sub(r"&quot;", '"', text)
        text = re.sub(r"&#\d+;", " ", text)
        
        text = re.sub(r"\s+", " ", text)
        text = text.strip()
        
        return title, text

    def fetch_multiple(
        self,
        urls: list[str | HttpUrl],
        continue_on_error: bool = True,
    ) -> list[FetchedPage]:
        """
        Fetch multiple URLs.
        
        Args:
            urls: List of URLs to fetch
            continue_on_error: Continue if a URL fails
            
        Returns:
            List of successfully fetched pages
        """
        pages = []
        
        for url in urls:
            try:
                page = self.fetch(url)
                pages.append(page)
            except WebFetchError as e:
                logger.warning("Failed to fetch %s: %s", url, e)
                if not continue_on_error:
                    raise
        
        return pages
