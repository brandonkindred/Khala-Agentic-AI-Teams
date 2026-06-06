"""
Synchronous GitHub REST client used by the coding team's run-from-github flow.

Kept intentionally small: just the endpoints `_run_with_github_hooks` needs
(`/repos`, `/issues`, `/issues/{n}/sub_issues`, `/issues/{n}/comments`,
`/pulls`). Includes pagination via the `Link` header, retries for transient
failures (502/503/504/transport), and rate-limit-aware backoff for 403s.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.github.com"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_RETRIES = 3
RATE_LIMIT_CAP_S = 60
MAX_ISSUES_TRAVERSED = 1000


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    state: str
    html_url: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class SubIssue:
    number: int
    state: str
    title: str


@dataclass(frozen=True)
class PullRequest:
    number: int
    html_url: str
    head: str
    base: str


@dataclass(frozen=True)
class Repo:
    default_branch: str


class GitHubAPIError(RuntimeError):
    """Raised on any unrecoverable GitHub API failure."""

    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self.body = body
        super().__init__(f"GitHub API {status}: {body[:200]}")


class NotAnIssueError(GitHubAPIError):
    """`get_issue` was called for a number that points at a pull request.

    Carried as a `GitHubAPIError` subclass so the existing single
    ``try/except GitHubAPIError`` blocks still catch it, while letting the
    route handler distinguish operator error (400) from upstream failure (502).
    """

    def __init__(self, number: int) -> None:
        self.number = number
        super().__init__(0, f"#{number} is a pull request, not an issue")


# Pattern of refnames git considers safe-ish to pass on a command line.
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")


def _is_safe_ref(ref: str) -> bool:
    """Reject refnames that could be parsed as git options (leading `-`) or
    contain shell-suspect characters. Used as defense-in-depth before we hand
    a GitHub-supplied default-branch name to subprocess git."""
    return bool(ref) and not ref.startswith("-") and bool(_SAFE_REF_RE.match(ref))


_TOKEN_URL_RE = re.compile(r"https?://[^/\s@]+@", re.IGNORECASE)


def scrub_token_from_text(msg: str) -> str:
    """Best-effort redact ``https://user:token@host/...`` style remote URLs
    that git can echo to stderr, before we put the message into a public
    issue comment or job error field."""
    if not msg:
        return msg
    return _TOKEN_URL_RE.sub("https://***@", msg)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def _parse_next_link(header_value: Optional[str]) -> Optional[str]:
    if not header_value:
        return None
    m = _LINK_NEXT_RE.search(header_value)
    return m.group(1) if m else None


def _issue_from_payload(payload: dict[str, Any]) -> Issue:
    return Issue(
        number=int(payload["number"]),
        title=payload.get("title") or "",
        body=payload.get("body") or "",
        state=payload.get("state") or "open",
        html_url=payload.get("html_url") or "",
        labels=tuple(
            label["name"]
            for label in (payload.get("labels") or [])
            if isinstance(label, dict) and label.get("name")
        ),
    )


def _sub_issue_from_payload(payload: dict[str, Any]) -> SubIssue:
    return SubIssue(
        number=int(payload["number"]),
        state=payload.get("state") or "open",
        title=payload.get("title") or "",
    )


def _pr_from_payload(payload: dict[str, Any]) -> PullRequest:
    head = (payload.get("head") or {}).get("ref") or ""
    base = (payload.get("base") or {}).get("ref") or ""
    return PullRequest(
        number=int(payload["number"]),
        html_url=payload.get("html_url") or "",
        head=head,
        base=base,
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GitHubClient:
    """Thin synchronous wrapper around the GitHub REST API."""

    def __init__(
        self,
        token: str,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Any = time.sleep,
    ) -> None:
        if not token:
            raise ValueError("GitHubClient requires a token")
        self._token = token
        self._base_url = (base_url or os.environ.get("GITHUB_API_URL") or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self._timeout = timeout
        self._max_retries = max(1, max_retries)
        self._sleep = sleep
        self._client = httpx.Client(timeout=timeout)

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
        backoff = 1.0
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
                self._sleep(backoff)
                backoff *= 2
                continue

            if response.status_code in (502, 503, 504):
                logger.warning(
                    "GitHub %s %s -> %d (attempt %d)",
                    method,
                    url,
                    response.status_code,
                    attempt + 1,
                )
                self._sleep(backoff)
                backoff *= 2
                continue

            if (
                response.status_code == 403
                and response.headers.get("X-RateLimit-Remaining") == "0"
                and attempt == 0
            ):
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

            return response

        if last_exc is not None:
            raise GitHubAPIError(0, f"transport error: {last_exc}") from last_exc
        raise GitHubAPIError(0, "exceeded retries")

    def _check(self, response: httpx.Response) -> httpx.Response:
        if 200 <= response.status_code < 300:
            return response
        raise GitHubAPIError(response.status_code, response.text)

    # ----- public methods ----------------------------------------------------

    def get_repo(self, owner: str, repo: str) -> Repo:
        r = self._check(self._request("GET", f"/repos/{owner}/{repo}"))
        return Repo(default_branch=r.json().get("default_branch") or "main")

    def list_open_issues(
        self,
        owner: str,
        repo: str,
        label: Optional[str] = None,
    ) -> Iterator[Issue]:
        path = f"/repos/{owner}/{repo}/issues"
        params: dict[str, Any] = {"state": "open", "per_page": 100}
        if label:
            params["labels"] = label
        url: Optional[str] = path
        seen = 0
        while url:
            response = self._check(self._request("GET", url, params=params))
            params = None  # only on first page
            for item in response.json() or []:
                if "pull_request" in item:
                    continue
                seen += 1
                if seen > MAX_ISSUES_TRAVERSED:
                    logger.warning(
                        "list_open_issues hit MAX_ISSUES_TRAVERSED=%d; stopping",
                        MAX_ISSUES_TRAVERSED,
                    )
                    return
                yield _issue_from_payload(item)
            url = _parse_next_link(response.headers.get("Link"))

    def get_issue(self, owner: str, repo: str, number: int) -> Issue:
        r = self._check(self._request("GET", f"/repos/{owner}/{repo}/issues/{number}"))
        payload = r.json()
        if "pull_request" in payload:
            raise NotAnIssueError(number)
        return _issue_from_payload(payload)

    def list_sub_issues(self, owner: str, repo: str, number: int) -> list[SubIssue]:
        path = f"/repos/{owner}/{repo}/issues/{number}/sub_issues"
        params: dict[str, Any] = {"per_page": 100}
        url: Optional[str] = path
        out: list[SubIssue] = []
        while url:
            response = self._request("GET", url, params=params)
            params = None
            if response.status_code == 404:
                return []
            response = self._check(response)
            for item in response.json() or []:
                out.append(_sub_issue_from_payload(item))
            url = _parse_next_link(response.headers.get("Link"))
        return out

    def add_issue_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        self._check(
            self._request(
                "POST",
                f"/repos/{owner}/{repo}/issues/{number}/comments",
                json={"body": body},
            )
        )

    def find_existing_pr(self, owner: str, repo: str, head: str) -> Optional[PullRequest]:
        params = {"state": "open", "head": f"{owner}:{head}"}
        r = self._check(self._request("GET", f"/repos/{owner}/{repo}/pulls", params=params))
        items = r.json() or []
        if not items:
            return None
        return _pr_from_payload(items[0])

    def create_pull_request(
        self,
        *,
        owner: str,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str,
        draft: bool = True,
    ) -> PullRequest:
        r = self._check(
            self._request(
                "POST",
                f"/repos/{owner}/{repo}/pulls",
                json={
                    "title": title,
                    "head": head,
                    "base": base,
                    "body": body,
                    "draft": draft,
                },
            )
        )
        return _pr_from_payload(r.json())

    def update_pull_request(
        self,
        *,
        owner: str,
        repo: str,
        number: int,
        body: str,
    ) -> PullRequest:
        """Patch an existing PR's body. Used to keep a reused PR's description in sync with the
        latest run (e.g. surfacing tasks that failed on a retry)."""
        r = self._check(
            self._request(
                "PATCH",
                f"/repos/{owner}/{repo}/pulls/{number}",
                json={"body": body},
            )
        )
        return _pr_from_payload(r.json())

    # ----- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


# Re-export for convenience in tests / typing.
__all__ = [
    "GitHubAPIError",
    "GitHubClient",
    "Issue",
    "MAX_ISSUES_TRAVERSED",
    "NotAnIssueError",
    "PullRequest",
    "Repo",
    "SubIssue",
    "scrub_token_from_text",
]
