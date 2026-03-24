from __future__ import annotations

from typing import Optional

import httpx
from bs4 import BeautifulSoup
from pydantic import HttpUrl

from ..models import SourceDocument


class WebFetchError(RuntimeError):
    """Raised when the web fetch tool fails."""


class SimpleWebFetcher:
    """
    Lightweight HTTP fetcher with basic HTML-to-text extraction.

    This is intentionally simple but good enough for most research use-cases.
    """

    def __init__(self, *, timeout: float = 15.0, user_agent: Optional[str] = None) -> None:
        """
        Preconditions: timeout > 0.
        """
        assert timeout > 0, "timeout must be positive"
        self.timeout = timeout
        self.user_agent = user_agent or "StrandsResearchAgent/1.0"

    def fetch(self, url: HttpUrl) -> SourceDocument:
        """
        Preconditions: url is a valid HttpUrl.
        Postconditions: Returns SourceDocument with url equal to input; or raises WebFetchError on failure.
        """
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        try:
            with httpx.Client(
                timeout=self.timeout, headers=headers, follow_redirects=True
            ) as client:
                resp = client.get(str(url))
        except httpx.HTTPError as exc:
            raise WebFetchError(f"HTTP error while fetching {url}: {exc}") from exc

        if resp.status_code >= 400:
            raise WebFetchError(f"Failed to fetch {url}: HTTP {resp.status_code}")

        content_type = resp.headers.get("Content-Type", "")
        text = resp.text

        title: Optional[str] = None
        plain_text = text
        if "html" in content_type.lower() or "<html" in text.lower():
            soup = BeautifulSoup(text, "html.parser")

            # Title
            if soup.title and soup.title.string:
                title = soup.title.string.strip()

            # Remove script/style
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()

            plain_text = soup.get_text(separator="\n", strip=True)

        domain = httpx.URL(str(url)).host

        return SourceDocument(
            url=url,
            title=title,
            content=plain_text,
            publish_date=None,  # Can be inferred later from metadata if needed
            domain=domain,
            language=None,
            metadata={"content_type": content_type},
        )
