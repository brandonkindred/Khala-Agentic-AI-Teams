"""
Low-level HTTP transport, retry/backoff, and Link-header pagination for
`GitHubClient` (see `client.py`).

Split out of `client.py` to keep transport concerns (requests, retries,
rate-limit backoff, pagination) separate from the client's public API
surface (dataclasses, error types, and `get_*`/`list_*`/`create_*` methods).
No behavior change from the prior single-file layout.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterator, Optional

import httpx

from shared.http.retry import retry_delay

# Named to match the pre-split logger identity (`...github_source.client`), not this
# module's own `__name__`: these log records are byte-identical carry-overs from client.py,
# and keeping the same logger name preserves that history for anything filtering on it.
logger = logging.getLogger(__name__.replace("client_http", "client"))

DEFAULT_BASE_URL = "https://api.github.com"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_RETRIES = 3
RATE_LIMIT_CAP_S = 60
SECONDARY_RATE_LIMIT_MAX_RETRIES = 5
SECONDARY_RATE_LIMIT_CAP_S = 120
MAX_ISSUES_TRAVERSED = 1000
MAX_REVIEW_THREADS_TRAVERSED = 2000
MAX_REVIEW_COMMENTS_TRAVERSED = 5000


_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def _parse_next_link(header_value: Optional[str]) -> Optional[str]:
    if not header_value:
        return None
    m = _LINK_NEXT_RE.search(header_value)
    return m.group(1) if m else None


def _parse_retry_after(header_value: Optional[str]) -> Optional[float]:
    """Parse a ``Retry-After`` header's integer-seconds value.

    Postconditions:
        - Returns ``None`` when the header is absent or not a base-10 integer
          (GitHub's secondary-rate-limit ``Retry-After`` is always integer
          seconds, never an HTTP-date, but a malformed value must not raise).
    """
    if not header_value:
        return None
    try:
        return float(int(header_value))
    except ValueError:
        return None


class GitHubAPIError(RuntimeError):
    """Raised on any unrecoverable GitHub API failure."""

    def __init__(self, status: int, body: str = "") -> None:
        """Carry the failing HTTP status and response body alongside the exception message.

        Postconditions:
            - Sets ``self.status`` and ``self.body`` to the given values and calls
              ``RuntimeError.__init__`` with ``f"GitHub API {status}: {body}"``, so the
              exception's ``str()`` is diagnosable even where only the message is logged.
        """
        self.status = status
        self.body = body
        super().__init__(f"GitHub API {status}: {body}")


class _GitHubHttpMixin:
    """Low-level request/pagination methods for `GitHubClient`.

    Preconditions:
        - The inheriting class sets, before any method here is called (as
          ``GitHubClient.__init__`` does): ``self._token`` (str), ``self._base_url``
          (str, no trailing slash), ``self._max_retries`` (int, >= 1), ``self._sleep``
          (callable, ``time.sleep``-compatible), and ``self._client`` (an ``httpx.Client``).
          ``self._timeout`` is not read by any method here — it is stored by
          ``GitHubClient.__init__`` for introspection only, since the actual per-request
          timeout is already baked into ``self._client`` at construction time.
    """

    # ----- low-level request -------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "khala-coding-team",
        }

    def _absolute_url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        if not path_or_url.startswith("/"):
            path_or_url = "/" + path_or_url
        return f"{self._base_url}{path_or_url}"

    def _retry_secondary_rate_limit(
        self,
        method: str,
        url: str,
        params: Optional[dict[str, Any]],
        json: Optional[dict[str, Any]],
        response: httpx.Response,
    ) -> httpx.Response:
        """Retry a GitHub secondary-rate-limit (429) response, honoring ``Retry-After``.

        Preconditions:
            - ``response.status_code == 429``.
        Postconditions:
            - Returns the first non-429 response obtained from re-issuing the same
              request, or, if every retry is still 429, the final 429 response after
              exactly ``SECONDARY_RATE_LIMIT_MAX_RETRIES`` retries. Does not raise
              ``GitHubAPIError`` itself -- the caller's ``_check`` turns a persisting
              429 into ``GitHubAPIError`` only once this budget is exhausted. The
              underlying ``self._client.request(...)`` / ``self._sleep(...)`` calls
              may still raise their own exceptions (e.g. ``httpx.TransportError``).
        """
        for attempt in range(SECONDARY_RATE_LIMIT_MAX_RETRIES):
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            wait = retry_delay(
                attempt, 1.0, SECONDARY_RATE_LIMIT_CAP_S, retry_after_seconds=retry_after
            )
            logger.warning(
                "GitHub %s %s -> 429 (secondary rate limit); sleeping %.1fs (retry %d/%d)",
                method,
                url,
                wait,
                attempt + 1,
                SECONDARY_RATE_LIMIT_MAX_RETRIES,
            )
            self._sleep(wait)
            response = self._client.request(
                method,
                url,
                headers=self._headers(),
                params=params,
                json=json,
            )
            if response.status_code != 429:
                return response
        return response

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
    ) -> httpx.Response:
        url = self._absolute_url(path)
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            try:
                response = self._client.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json,
                )
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning(
                    "GitHub %s %s transport error: %s (attempt %d)", method, url, exc, attempt + 1
                )
                self._sleep(retry_delay(attempt, 1.0, RATE_LIMIT_CAP_S))
                continue

            if response.status_code in (502, 503, 504):
                logger.warning(
                    "GitHub %s %s -> %d (attempt %d)",
                    method,
                    url,
                    response.status_code,
                    attempt + 1,
                )
                self._sleep(retry_delay(attempt, 1.0, RATE_LIMIT_CAP_S))
                continue

            if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
                reset = response.headers.get("X-RateLimit-Reset")
                wait = 1.0
                if reset:
                    try:
                        wait = max(1.0, min(RATE_LIMIT_CAP_S, int(reset) - int(time.time())))
                    except ValueError:
                        wait = 1.0
                logger.warning("GitHub rate-limited; sleeping %.1fs", wait)
                self._sleep(wait)
                continue

            if response.status_code == 429:
                return self._retry_secondary_rate_limit(method, url, params, json, response)

            return response

        if last_exc is not None:
            raise GitHubAPIError(0, f"transport error: {last_exc}") from last_exc
        raise GitHubAPIError(0, "exceeded retries")

    def _check(self, response: httpx.Response) -> httpx.Response:
        if 200 <= response.status_code < 300:
            return response
        raise GitHubAPIError(response.status_code, response.text)

    # ----- pagination ----------------------------------------------------------

    def _paginate(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        *,
        cap: Optional[int] = None,
        cap_label: Optional[str] = None,
        not_found_ok: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Yield raw item payloads across every page of a ``Link``-header-paginated
        GitHub list endpoint.

        Preconditions:
            - ``path`` is a repo-relative API path; ``params`` is sent on the first
              request only (later pages are driven entirely by the ``Link`` header's
              ``rel="next"`` URL).
            - When ``cap`` is not None, ``cap_label`` is a %-style format string
              containing exactly one ``%d``, matching the call site's own warning text.
        Postconditions:
            - Yields one raw payload ``dict`` per item, across all pages, in GitHub's
              response order.
            - When ``not_found_ok`` is True and the first response is a 404, returns
              immediately without yielding anything and without raising. Any other
              non-2xx response, regardless of ``not_found_ok``, raises
              ``GitHubAPIError`` via ``self._check``.
            - When ``cap`` is not None, every item pulled off a page counts toward it
              before it is yielded (i.e. before any caller-side filtering). Once the
              count exceeds ``cap``, logs ``cap_label`` and stops -- the item that
              tipped it over is never yielded, and the caller's own comprehension or
              loop then ends with whatever partial result it already accumulated.
        """
        seen = 0
        url: Optional[str] = path
        while url:
            response = self._request("GET", url, params=params)
            params = None  # only on first page
            if not_found_ok and response.status_code == 404:
                return
            response = self._check(response)
            for item in response.json() or []:
                if cap is not None:
                    seen += 1
                    if seen > cap:
                        logger.warning(cap_label, cap)
                        return
                yield item
            url = _parse_next_link(response.headers.get("Link"))
