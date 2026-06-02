"""Lightweight HTML fetch + text extraction for job listing pages.

Modeled on ``personal_assistant_team/tools/web_fetch.py`` (regex-based, no
BeautifulSoup dependency). Returns plain text suitable for LLM extraction of
structured posting fields.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_CONTENT = 60_000
_USER_AGENT = "Mozilla/5.0 (compatible; KhalaJobMatcher/1.0)"

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|nav|footer|header)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


class WebFetchError(RuntimeError):
    """Raised when a page cannot be fetched."""


@dataclass
class FetchedPage:
    url: str
    title: str
    text: str


class WebFetcher:
    """Fetch a URL and return cleaned plain text.

    Invariants:
        * Extracted ``text`` is never longer than ``max_content`` characters.
    """

    def __init__(
        self, *, timeout: float = DEFAULT_TIMEOUT, max_content: int = DEFAULT_MAX_CONTENT
    ) -> None:
        assert max_content > 0, "max_content must be positive"
        self.timeout = timeout
        self.max_content = max_content

    def fetch(self, url: str) -> FetchedPage:
        """Fetch ``url`` and return its title + cleaned text.

        Preconditions:
            * ``url`` is a non-empty http(s) URL.
        Postconditions:
            * Returns a :class:`FetchedPage` with text truncated to
              ``max_content`` characters.
        Raises:
            WebFetchError: on transport failure or non-2xx status.
        """
        assert url and url.startswith("http"), "url must be an absolute http(s) URL"
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                resp = client.get(url, headers={"User-Agent": _USER_AGENT})
        except httpx.HTTPError as exc:
            raise WebFetchError(f"Failed to fetch {url}: {exc}") from exc

        if resp.status_code >= 400:
            raise WebFetchError(f"Fetch of {url} returned status {resp.status_code}")

        title, text = self._extract(resp.text)
        return FetchedPage(url=url, title=title, text=text[: self.max_content])

    def _extract(self, html: str) -> tuple[str, str]:
        """Strip markup and return ``(title, text)``.

        Postconditions:
            * ``title`` is the ``<title>`` content (whitespace-collapsed) or "".
            * ``text`` has script/style/nav/footer/header blocks removed and
              whitespace collapsed.
        """
        title_match = _TITLE_RE.search(html)
        title = _WS_RE.sub(" ", title_match.group(1)).strip() if title_match else ""
        body = _SCRIPT_STYLE_RE.sub(" ", html)
        body = _TAG_RE.sub(" ", body)
        text = _WS_RE.sub(" ", body).strip()
        return title, text
