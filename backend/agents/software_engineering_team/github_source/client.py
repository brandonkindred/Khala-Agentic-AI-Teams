"""
Synchronous GitHub REST client used by the coding team's run-from-github flow.

Kept intentionally small: just the endpoints `_run_with_github_hooks` needs
(`/repos`, `/issues`, `/issues/{n}/sub_issues`, `/issues/{n}/comments`,
`/pulls`). Includes pagination via the `Link` header, retries for transient
failures (502/503/504/transport), and rate-limit-aware backoff for 403s.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import httpx

from .client_http import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_S,
    MAX_ISSUES_TRAVERSED,
    MAX_REVIEW_COMMENTS_TRAVERSED,
    MAX_REVIEW_THREADS_TRAVERSED,
    GitHubAPIError,
    _GitHubHttpMixin,
)

logger = logging.getLogger(__name__)

# GraphQL query for review-thread resolution state: GitHub's REST API has no
# "resolved" field on a review comment, so thread resolution (the "Resolve
# conversation" button on GitHub) can only be read via GraphQL's `isResolved`.
_REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          comments(first: 100) {
            nodes { databaseId }
          }
        }
      }
    }
  }
}
"""

# Appended (as an HTML comment — invisible in GitHub's rendered view) to every issue/PR
# conversation comment Khala posts, so the "@khala review" webhook can recognize and skip
# Khala's own output. Comments are posted with the operator's PAT, so author identity
# cannot distinguish Khala's comments from the operator's genuine commands — and the PAT
# owner is often exactly the person expected to trigger reviews, so filtering by author
# would break them. The webhook handler keeps its own copy of this literal (it must not
# import this module at module scope); a cross-module test asserts the two stay equal.
KHALA_COMMENT_MARKER = "<!-- khala-generated -->"


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """Metadata for a GitHub issue.

    ``number`` is the repository-local issue number used by most endpoints.
    ``id`` is GitHub's global numeric issue id -- distinct from ``number`` and
    intentionally named to match the API field -- required for native
    sub-issue linkage via :meth:`GitHubClient.add_sub_issue`.
    """

    number: int
    title: str
    body: str
    state: str
    html_url: str
    labels: tuple[str, ...]
    id: int


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
class PullRequestDetail:
    """Rich detail for a single pull request, including the head commit SHA.

    The head SHA is the ``commit_id`` an inline review must be anchored to so its
    comments resolve against the exact commit that was reviewed.
    """

    number: int
    html_url: str
    head: str
    base: str
    head_sha: str
    title: str
    body: str
    draft: bool
    author: str
    state: str
    updated_at: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class PullRequestFile:
    """One file changed by a pull request.

    ``patch`` is the unified-diff hunk text GitHub returns; it is empty for
    binary files and files whose diff is too large to inline. ``previous_filename``
    is set only for renames.
    """

    filename: str
    status: str
    patch: str
    additions: int
    deletions: int
    previous_filename: Optional[str]


@dataclass(frozen=True)
class ReviewComment:
    """One existing review comment already on a pull request.

    ``line`` is ``None`` for a file-level comment: GitHub's read-back payload
    for one omits ``line``/``position`` entirely (there is no ``subject_type``
    echo to check instead).
    """

    id: int
    path: str
    line: Optional[int]
    body: str
    html_url: str


@dataclass(frozen=True)
class IssueComment:
    """One existing standalone issue/PR conversation comment."""

    id: int
    body: str
    html_url: str


@dataclass(frozen=True)
class Repo:
    default_branch: str


class NotAnIssueError(GitHubAPIError):
    """`get_issue` was called for a number that points at a pull request.

    Carried as a `GitHubAPIError` subclass so the existing single
    ``try/except GitHubAPIError`` blocks still catch it, while letting the
    route handler distinguish operator error (400) from upstream failure (502).
    """

    def __init__(self, number: int) -> None:
        """Record the pull-request number that was requested as an issue.

        Preconditions:
            - ``number`` is the issue/PR number `get_issue` was called with.
        Postconditions:
            - Sets ``self.number`` and initializes the ``GitHubAPIError`` base with
              status ``0`` (no real HTTP status applies) and a message identifying
              ``number`` as a pull request, not an issue.
        """
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
        id=int(payload["id"]),
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


def _pr_detail_from_payload(payload: dict[str, Any]) -> PullRequestDetail:
    head = payload.get("head") or {}
    base = payload.get("base") or {}
    return PullRequestDetail(
        number=int(payload["number"]),
        html_url=payload.get("html_url") or "",
        head=head.get("ref") or "",
        base=base.get("ref") or "",
        head_sha=head.get("sha") or "",
        title=payload.get("title") or "",
        body=payload.get("body") or "",
        draft=bool(payload.get("draft", False)),
        author=(payload.get("user") or {}).get("login") or "",
        state=payload.get("state") or "open",
        updated_at=payload.get("updated_at") or "",
        labels=tuple(
            label["name"]
            for label in (payload.get("labels") or [])
            if isinstance(label, dict) and label.get("name")
        ),
    )


def _pr_file_from_payload(payload: dict[str, Any]) -> PullRequestFile:
    return PullRequestFile(
        filename=payload.get("filename") or "",
        status=payload.get("status") or "",
        patch=payload.get("patch") or "",
        additions=int(payload.get("additions") or 0),
        deletions=int(payload.get("deletions") or 0),
        previous_filename=payload.get("previous_filename"),
    )


def _review_comment_from_payload(payload: dict[str, Any]) -> ReviewComment:
    return ReviewComment(
        id=int(payload["id"]),
        path=payload.get("path") or "",
        line=payload.get("line"),
        body=payload.get("body") or "",
        html_url=payload.get("html_url") or "",
    )


def _issue_comment_from_payload(payload: dict[str, Any]) -> IssueComment:
    return IssueComment(
        id=int(payload["id"]),
        body=payload.get("body") or "",
        html_url=payload.get("html_url") or "",
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GitHubClient(_GitHubHttpMixin):
    """Thin synchronous wrapper around the GitHub REST API."""

    def __init__(
        self,
        token: str,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Any = time.sleep,
    ) -> None:
        """Build a client authenticated with ``token`` against ``base_url`` (or the default API host).

        Preconditions:
            - ``token`` is a non-empty GitHub token (PAT or installation token).
        Postconditions:
            - ``base_url`` defaults to ``GITHUB_API_URL`` when unset, else
              :data:`DEFAULT_BASE_URL`, with any trailing slash stripped.
              ``max_retries`` is floored at 1 so a retry loop always attempts at
              least once. ``sleep`` is injectable (tests pass a no-op) so retry/backoff
              delays never actually block. Raises ``ValueError`` when ``token`` is empty.
        """
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

    def get_repo(self, owner: str, repo: str) -> Repo:
        """Fetch repository metadata (``GET /repos/{owner}/{repo}``).

        Postconditions:
            - Returns a ``Repo`` carrying ``default_branch`` (falling back to ``"main"``
              when GitHub's payload omits it). Raises ``GitHubAPIError`` on any non-2xx.
        """
        r = self._check(self._request("GET", f"/repos/{owner}/{repo}"))
        return Repo(default_branch=r.json().get("default_branch") or "main")

    def get_authenticated_login(self) -> str:
        """Return the login of the user the token authenticates as (``GET /user``).

        Postconditions:
            - Returns the ``login`` string, or "" when it cannot be determined.
              Used to avoid requesting changes on a PR the reviewer authored.
        """
        r = self._check(self._request("GET", "/user"))
        payload = r.json()
        return (payload.get("login") or "") if isinstance(payload, dict) else ""

    def list_open_issues(
        self,
        owner: str,
        repo: str,
        label: Optional[str] = None,
    ) -> Iterator[Issue]:
        """Yield every open issue, optionally filtered by ``label``, following ``Link`` pagination.

        Preconditions:
            - ``label``, when given, is a valid label name on ``owner/repo``.
        Postconditions:
            - Yields one ``Issue`` per open issue in GitHub's response order; pull
              requests (payloads carrying a ``pull_request`` key) are silently skipped,
              since GitHub's issues endpoint returns both. Bounded by
              :data:`MAX_ISSUES_TRAVERSED`: traversal stops (with a warning logged)
              once that many items -- issues AND pull requests -- have been examined,
              rather than paginating unbounded. Counting pull requests toward this cap
              too (not just yielded issues) matters because GitHub's ``/issues``
              endpoint interleaves both: a repository with many open pull requests and
              few open issues would otherwise page through all of them unbounded
              before this generator ever stops, even though almost nothing gets
              yielded -- defeating the cap's purpose of bounding a caller's traversal
              cost (e.g. the `/review-pr` duplicate-issue check, which reads only the
              first N yielded issues via ``itertools.islice`` but would still pay for
              every page fetched to get there).
        """
        path = f"/repos/{owner}/{repo}/issues"
        params: dict[str, Any] = {"state": "open", "per_page": 100}
        if label:
            params["labels"] = label
        for item in self._paginate(
            path,
            params,
            cap=MAX_ISSUES_TRAVERSED,
            cap_label="list_open_issues hit MAX_ISSUES_TRAVERSED=%d; stopping",
        ):
            if "pull_request" in item:
                continue
            yield _issue_from_payload(item)

    def get_issue(self, owner: str, repo: str, number: int) -> Issue:
        """Fetch a single issue (``GET /repos/{owner}/{repo}/issues/{number}``).

        Preconditions:
            - ``number`` names an existing issue or pull request (GitHub serves both
              through this endpoint).
        Postconditions:
            - Returns the ``Issue`` when ``number`` is a genuine issue. Raises
              ``NotAnIssueError`` when the payload carries a ``pull_request`` key (the
              number names a pull request instead), and ``GitHubAPIError`` on any
              other non-2xx.
        """
        r = self._check(self._request("GET", f"/repos/{owner}/{repo}/issues/{number}"))
        payload = r.json()
        if "pull_request" in payload:
            raise NotAnIssueError(number)
        return _issue_from_payload(payload)

    def list_sub_issues(self, owner: str, repo: str, number: int) -> list[SubIssue]:
        """List an issue's sub-issues, following ``Link`` pagination.

        Preconditions:
            - ``number`` names an existing issue.
        Postconditions:
            - Returns every sub-issue of ``number`` in GitHub's response order.
              Returns ``[]`` when the endpoint 404s (the repository has sub-issues
              disabled, or ``number`` has none) rather than raising, so callers (e.g.
              the dependency resolver) can treat "no sub-issues" and "feature
              unavailable" the same way. Raises ``GitHubAPIError`` on any other
              non-2xx.
        """
        path = f"/repos/{owner}/{repo}/issues/{number}/sub_issues"
        params: dict[str, Any] = {"per_page": 100}
        return [
            _sub_issue_from_payload(item)
            for item in self._paginate(path, params, not_found_ok=True)
        ]

    def add_issue_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        """Post an issue/PR conversation comment, tagged as Khala-generated.

        Preconditions: ``number`` names an existing issue or pull request.
        Postconditions: posts ``body`` with :data:`KHALA_COMMENT_MARKER` (an HTML
            comment, invisible in GitHub's rendered view) appended when not already
            present. The marker lets the ``@khala review`` webhook recognize and skip
            Khala's own comments — they are posted with the operator's PAT, so author
            identity cannot distinguish them from the operator's genuine commands (and
            must not: the PAT owner is often exactly the person triggering reviews).
            Raises ``GitHubAPIError`` on any non-2xx.
        """
        if KHALA_COMMENT_MARKER not in body:
            body = f"{body}\n\n{KHALA_COMMENT_MARKER}"
        self._check(
            self._request(
                "POST",
                f"/repos/{owner}/{repo}/issues/{number}/comments",
                json={"body": body},
            )
        )

    def create_issue(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        body: str,
        labels: Optional[list[str]] = None,
    ) -> Issue:
        """Open a new issue in ``owner/repo``, tagged as Khala-generated.

        Preconditions:
            - ``title`` is a non-empty issue title.
        Postconditions:
            - Creates the issue with :data:`KHALA_COMMENT_MARKER` appended to the
              body when not already present (marking it Khala-generated, the same
              provenance convention :meth:`add_issue_comment` uses) and any
              ``labels`` applied. Returns the created :class:`Issue`, which carries
              the new issue's ``number`` and ``html_url``. Raises ``GitHubAPIError``
              on any non-2xx (e.g. a token without issue-write scope).
        """
        if KHALA_COMMENT_MARKER not in body:
            body = f"{body}\n\n{KHALA_COMMENT_MARKER}"
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = list(labels)
        r = self._check(self._request("POST", f"/repos/{owner}/{repo}/issues", json=payload))
        return _issue_from_payload(r.json())

    def update_issue(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        body: Optional[str] = None,
        labels: Optional[list[str]] = None,
    ) -> Issue:
        """Update an existing issue's body and/or labels (``PATCH /repos/{owner}/{repo}/issues/{number}``).

        Preconditions:
            - ``number`` names an existing issue.
            - At least one of ``body``/``labels`` is given (a no-op call is a caller
              bug, not a legitimate PATCH-with-no-fields request).
        Postconditions:
            - Sends only the given field(s); an omitted field is left unchanged by
              GitHub. ``labels``, when given, REPLACES the issue's full label set
              (GitHub's PATCH semantics are not additive) -- callers that want to
              preserve existing labels must pass the complete merged list themselves.
              Returns the updated ``Issue``. Raises ``ValueError`` when neither
              ``body`` nor ``labels`` is given, ``GitHubAPIError`` on any non-2xx.
        """
        if body is None and labels is None:
            raise ValueError("update_issue requires at least one of body/labels")
        payload: dict[str, Any] = {}
        if body is not None:
            payload["body"] = body
        if labels is not None:
            payload["labels"] = list(labels)
        r = self._check(
            self._request("PATCH", f"/repos/{owner}/{repo}/issues/{number}", json=payload)
        )
        return _issue_from_payload(r.json())

    def add_sub_issue(self, owner: str, repo: str, issue_number: int, sub_issue_id: int) -> None:
        """Link an existing issue as a native sub-issue of ``issue_number``
        (``POST /repos/{owner}/{repo}/issues/{issue_number}/sub_issues``).

        Preconditions:
            - ``issue_number`` names an existing issue in ``owner/repo``.
            - ``sub_issue_id`` is the child issue's internal numeric id
              (:attr:`Issue.id`, GitHub's global issue id) -- NOT its ``number``.
              GitHub's sub-issues API keys off ``id``, unlike almost every other
              issue endpoint in this client, which keys off ``number``.
        Postconditions:
            - Links the two issues natively (visible via ``list_sub_issues``).
              Raises ``GitHubAPIError`` on any non-2xx (e.g. the child is already
              linked to a different parent, or the repository has sub-issues
              disabled).
        """
        self._check(
            self._request(
                "POST",
                f"/repos/{owner}/{repo}/issues/{issue_number}/sub_issues",
                json={"sub_issue_id": sub_issue_id},
            )
        )

    def create_comment_reaction(
        self, owner: str, repo: str, comment_id: int, content: str = "eyes"
    ) -> None:
        """React to an issue/PR comment (``POST .../issues/comments/{id}/reactions``).

        Preconditions:
            - ``content`` is a valid GitHub reaction
              (``+1``/``-1``/``laugh``/``confused``/``heart``/``hooray``/``rocket``/``eyes``).
        Postconditions:
            - Adds the reaction to the comment. GitHub returns 200 when the reaction
              already exists and 201 when newly created; both are accepted by ``_check``.
              Raises ``GitHubAPIError`` on any non-2xx (e.g. a missing comment or a token
              without write scope) — callers that treat the reaction as best-effort must
              guard the call themselves.
        """
        self._check(
            self._request(
                "POST",
                f"/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions",
                json={"content": content},
            )
        )

    def create_issue_reaction(
        self, owner: str, repo: str, issue_number: int, content: str = "+1"
    ) -> None:
        """React to an issue or pull request itself (``POST .../issues/{n}/reactions``).

        Preconditions:
            - ``content`` is a valid GitHub reaction
              (``+1``/``-1``/``laugh``/``confused``/``heart``/``hooray``/``rocket``/``eyes``).
        Postconditions:
            - Adds the reaction to the issue/PR's own conversation (not a specific
              comment) — PRs are issues in GitHub's REST API, so this puts a
              reaction pill directly on the PR. GitHub returns 200 when the
              reaction already exists and 201 when newly created; both accepted by
              ``_check``. Raises ``GitHubAPIError`` on any non-2xx — callers that
              treat the reaction as best-effort must guard the call themselves.
        """
        self._check(
            self._request(
                "POST",
                f"/repos/{owner}/{repo}/issues/{issue_number}/reactions",
                json={"content": content},
            )
        )

    def find_existing_pr(self, owner: str, repo: str, head: str) -> Optional[PullRequest]:
        """Find an open pull request whose head branch is ``head``, if one exists.

        Preconditions:
            - ``head`` is a branch name on ``owner/repo`` (not the ``owner:branch``
              qualified form — this method builds that qualifier itself).
        Postconditions:
            - Returns the first matching open ``PullRequest`` GitHub reports, or
              ``None`` when none is open for ``head``. Used to reuse (rather than
              duplicate) a draft PR across retried runs of the same feature branch.
        """
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
        """Open a pull request from ``head`` into ``base`` (``POST /repos/{owner}/{repo}/pulls``).

        Preconditions:
            - ``head`` and ``base`` are distinct existing branches on ``owner/repo``.
        Postconditions:
            - Returns the created ``PullRequest``. Opened as a draft by default
              (``draft=True``) so a freshly-created PR does not trigger reviewers
              or CI expectations before the caller is ready. Raises
              ``GitHubAPIError`` on any non-2xx (e.g. no commits between the
              branches, or a PR already open for ``head``).
        """
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
        latest run (e.g. surfacing tasks that failed on a retry).

        Preconditions:
            - ``owner``/``repo``/``number`` identify an existing pull request.
        Postconditions:
            - Returns the updated ``PullRequest`` with ``body`` replaced. Raises
              ``GitHubAPIError`` on any non-2xx.
        """
        r = self._check(
            self._request(
                "PATCH",
                f"/repos/{owner}/{repo}/pulls/{number}",
                json={"body": body},
            )
        )
        return _pr_from_payload(r.json())

    # ----- pull-request review -----------------------------------------------

    def list_open_pull_requests(self, owner: str, repo: str) -> Iterator[PullRequest]:
        """Yield every open pull request, following ``Link``-header pagination.

        Preconditions:
            - ``owner``/``repo`` name an existing repository.
        Postconditions:
            - Yields one ``PullRequest`` per open PR in GitHub's response order,
              bounded by ``MAX_ISSUES_TRAVERSED`` to cap an unbounded traversal.
        """
        path = f"/repos/{owner}/{repo}/pulls"
        params: dict[str, Any] = {"state": "open", "per_page": 100}
        for item in self._paginate(
            path,
            params,
            cap=MAX_ISSUES_TRAVERSED,
            cap_label="list_open_pull_requests hit MAX_ISSUES_TRAVERSED=%d; stopping",
        ):
            yield _pr_from_payload(item)

    def get_pull_request(self, owner: str, repo: str, number: int) -> PullRequestDetail:
        """Fetch full detail for one pull request, including its head commit SHA.

        Preconditions:
            - ``number`` names an existing pull request.
        Postconditions:
            - Returns the full ``PullRequestDetail`` (including ``head_sha``, the
              commit id an inline review must anchor to). Raises ``GitHubAPIError``
              on any non-2xx.
        """
        r = self._check(self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}"))
        return _pr_detail_from_payload(r.json())

    def get_pull_request_files(self, owner: str, repo: str, number: int) -> list[PullRequestFile]:
        """List every file a pull request changes, following ``Link`` pagination.

        Postconditions:
            - Returns one ``PullRequestFile`` per changed file (binary/oversized
              files carry an empty ``patch``), bounded by GitHub's own 3000-file cap.
        """
        path = f"/repos/{owner}/{repo}/pulls/{number}/files"
        params: dict[str, Any] = {"per_page": 100}
        return [_pr_file_from_payload(item) for item in self._paginate(path, params)]

    def list_review_comments(self, owner: str, repo: str, number: int) -> list[ReviewComment]:
        """List every existing review comment on a pull request, following ``Link`` pagination.

        Fetches ``GET /repos/{owner}/{repo}/pulls/{number}/comments`` — inline
        (line-anchored) and file-level review comments alike; a file-level
        comment is distinguished on read by carrying no ``line`` (GitHub does
        not echo ``subject_type`` back). Used so a new review run can recognize
        a finding that duplicates a comment already posted, rather than
        re-posting it. See :meth:`list_issue_comments` for the separate
        conversation-comment surface, and
        :meth:`get_resolved_review_thread_comment_ids` for resolution state
        (not available on this endpoint).

        Preconditions:
            - ``number`` names an existing pull request.
        Postconditions:
            - Returns one ``ReviewComment`` per existing review comment, in
              GitHub's response order (oldest first), bounded by
              :data:`MAX_REVIEW_COMMENTS_TRAVERSED` to cap an unbounded
              traversal on a PR with a pathological number of comments.
              Raises ``GitHubAPIError`` on any non-2xx.
        """
        path = f"/repos/{owner}/{repo}/pulls/{number}/comments"
        params: dict[str, Any] = {"per_page": 100}
        return [
            _review_comment_from_payload(item)
            for item in self._paginate(
                path,
                params,
                cap=MAX_REVIEW_COMMENTS_TRAVERSED,
                cap_label="list_review_comments hit MAX_REVIEW_COMMENTS_TRAVERSED=%d; stopping",
            )
        ]

    def list_issue_comments(self, owner: str, repo: str, number: int) -> list[IssueComment]:
        """List every existing standalone conversation comment, following ``Link`` pagination.

        Fetches ``GET /repos/{owner}/{repo}/issues/{number}/comments``. A pull
        request is an issue in GitHub's REST API, so this returns the PR's
        conversation-tab comments — not review comments (see
        :meth:`list_review_comments`).

        Preconditions:
            - ``number`` names an existing issue or pull request.
        Postconditions:
            - Returns one ``IssueComment`` per existing conversation comment,
              in GitHub's response order (oldest first), bounded by
              :data:`MAX_REVIEW_COMMENTS_TRAVERSED` to cap an unbounded
              traversal on a PR with a pathological number of comments.
              Raises ``GitHubAPIError`` on any non-2xx.
        """
        path = f"/repos/{owner}/{repo}/issues/{number}/comments"
        params: dict[str, Any] = {"per_page": 100}
        return [
            _issue_comment_from_payload(item)
            for item in self._paginate(
                path,
                params,
                cap=MAX_REVIEW_COMMENTS_TRAVERSED,
                cap_label="list_issue_comments hit MAX_REVIEW_COMMENTS_TRAVERSED=%d; stopping",
            )
        ]

    def get_resolved_review_thread_comment_ids(
        self, owner: str, repo: str, number: int
    ) -> set[int]:
        """Return the ids of every review comment belonging to a RESOLVED review thread.

        GitHub's REST API has no "resolved" field on a review comment — thread
        resolution (the "Resolve conversation" button) is exposed only via the
        GraphQL API's ``isResolved``. Posted through the same
        ``_request``/``_check`` machinery as every REST call (``_absolute_url``
        joins ``/graphql`` onto ``base_url`` unchanged), as a ``POST`` with a
        ``{"query", "variables"}`` body.

        Preconditions:
            - ``number`` names an existing pull request.
        Postconditions:
            - Returns the set of comment ids (``ReviewComment.id`` / GraphQL
              ``databaseId``, the same numeric id) that belong to a thread
              GitHub reports as resolved; an id absent from the set is either
              unresolved or belongs to no thread. Traversal is bounded by
              :data:`MAX_REVIEW_THREADS_TRAVERSED` threads, mirroring the
              bounded-list convention used elsewhere in this client (see
              :data:`MAX_ISSUES_TRAVERSED`).
            - Never raises: a GraphQL transport/HTTP error, a non-2xx status, a
              GraphQL-level error in the response body, or an unexpected
              response shape is logged as a warning and degrades to an empty
              set — a resolution-lookup failure must not fail an otherwise
              working review. Treating every comment as unresolved in that case
              only means a duplicate finding is kept and cross-referenced
              rather than dropped, never silently lost.
        """
        resolved: set[int] = set()
        after: Optional[str] = None
        seen = 0
        try:
            while True:
                variables: dict[str, Any] = {
                    "owner": owner,
                    "repo": repo,
                    "number": number,
                    "after": after,
                }
                response = self._check(
                    self._request(
                        "POST",
                        "/graphql",
                        json={"query": _REVIEW_THREADS_QUERY, "variables": variables},
                    )
                )
                payload = response.json()
                if payload.get("errors"):
                    logger.warning(
                        "get_resolved_review_thread_comment_ids: GraphQL errors for %s/%s#%s: %s",
                        owner,
                        repo,
                        number,
                        payload["errors"],
                    )
                    return resolved
                pr_data = ((payload.get("data") or {}).get("repository") or {}).get(
                    "pullRequest"
                ) or {}
                threads = pr_data.get("reviewThreads") or {}
                for node in threads.get("nodes") or []:
                    seen += 1
                    if seen > MAX_REVIEW_THREADS_TRAVERSED:
                        logger.warning(
                            "get_resolved_review_thread_comment_ids hit "
                            "MAX_REVIEW_THREADS_TRAVERSED=%d; stopping",
                            MAX_REVIEW_THREADS_TRAVERSED,
                        )
                        return resolved
                    if not node.get("isResolved"):
                        continue
                    for c in (node.get("comments") or {}).get("nodes") or []:
                        database_id = c.get("databaseId")
                        if isinstance(database_id, int):
                            resolved.add(database_id)
                page_info = threads.get("pageInfo") or {}
                if not page_info.get("hasNextPage"):
                    return resolved
                after = page_info.get("endCursor")
                if not after:
                    return resolved
        except Exception as e:  # noqa: BLE001 - a resolution-lookup failure must degrade to
            # an empty set, never fail the review (see the "Never raises" postcondition
            # above); an enumerated exception tuple here previously included a dead
            # KeyError (every field access below uses .get()) while still missing the
            # AttributeError a non-dict GraphQL payload segment would raise.
            logger.warning(
                "get_resolved_review_thread_comment_ids failed for %s/%s#%s: %s",
                owner,
                repo,
                number,
                e,
            )
            return resolved

    def get_file_contents(self, owner: str, repo: str, path: str, ref: str) -> Optional[str]:
        """Return the decoded text of a repository file at ``ref``, or ``None``.

        Fetches ``GET /repos/{owner}/{repo}/contents/{path}?ref={ref}`` and
        base64-decodes the ``content`` field. Used to read whole files (both a
        PR's changed files at its head SHA, and existing unchanged files the
        review needs to confirm exist).

        Preconditions:
            - ``path`` is a repository-relative path; ``ref`` is a branch, tag, or
              commit SHA.
        Postconditions:
            - Returns the file's UTF-8 text (replacing undecodable bytes) for a
              regular file. Returns ``None`` for a missing path (404), a
              directory or non-file entry, or a payload that is not
              base64-decodable — so a caller treats "unreadable" as "absent"
              rather than failing. Raises ``GitHubAPIError`` only for a non-404
              error status (auth, rate limit, server), so a real API failure is
              not silently masked as an absent file.
        """
        response = self._request(
            "GET", f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref}
        )
        if response.status_code == 404:
            return None
        payload = self._check(response).json()
        if not isinstance(payload, dict) or payload.get("type") != "file":
            return None
        if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
            return None
        try:
            raw = base64.b64decode(payload["content"])
        except (binascii.Error, ValueError):
            return None
        return raw.decode("utf-8", errors="replace")

    def get_repository_tree(
        self, owner: str, repo: str, ref: str, recursive: bool = True
    ) -> list[str]:
        """Return repository-relative paths of every blob (file) at ``ref``.

        Fetches ``GET /repos/{owner}/{repo}/git/trees/{ref}?recursive=1``.

        Preconditions:
            - ``ref`` is a branch, tag, or commit SHA.
        Postconditions:
            - Returns the path of every ``blob`` entry (files, not directories) in
              the tree. When GitHub marks the response ``truncated`` (a very large
              repository), returns what it did send (a partial listing) rather
              than raising — callers use the listing only as a convenience and can
              still read any exact path via :meth:`get_file_contents`. Raises
              ``GitHubAPIError`` on a non-2xx status.
        """
        params: dict[str, Any] = {"recursive": "1"} if recursive else {}
        payload = self._check(
            self._request("GET", f"/repos/{owner}/{repo}/git/trees/{ref}", params=params)
        ).json()
        if not isinstance(payload, dict):
            return []
        if payload.get("truncated"):
            logger.warning(
                "get_repository_tree: %s/%s@%s tree is truncated; listing is partial",
                owner,
                repo,
                ref,
            )
        return [
            entry["path"]
            for entry in (payload.get("tree") or [])
            if isinstance(entry, dict) and entry.get("type") == "blob" and entry.get("path")
        ]

    def create_pull_request_review(
        self,
        *,
        owner: str,
        repo: str,
        number: int,
        commit_id: str,
        body: str,
        event: str = "COMMENT",
        comments: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Submit one pull-request review, optionally with inline comments.

        Preconditions:
            - ``event`` is one of ``COMMENT``/``REQUEST_CHANGES``/``APPROVE``.
            - Each entry in ``comments`` is a **line-anchored**
              ``{"path", "line", "side", "body"}`` whose ``line`` falls on a line
              present in the diff for ``commit_id``. The Reviews API's embedded
              ``comments`` array does not accept ``subject_type``; file-level
              comments must be posted via ``create_review_comment`` instead.
              GitHub rejects the *entire* review (422) if any comment's line is
              invalid or its path is not in the diff.
        Postconditions:
            - Returns the created review payload (carries ``id`` and ``html_url``).
              Raises ``GitHubAPIError`` on any non-2xx response.
        """
        json_body: dict[str, Any] = {
            "commit_id": commit_id,
            "body": body,
            "event": event,
        }
        if comments:
            json_body["comments"] = comments
        r = self._check(
            self._request(
                "POST",
                f"/repos/{owner}/{repo}/pulls/{number}/reviews",
                json=json_body,
            )
        )
        return r.json()

    def create_review_comment(
        self,
        *,
        owner: str,
        repo: str,
        number: int,
        commit_id: str,
        path: str,
        body: str,
        line: Optional[int] = None,
        side: str = "RIGHT",
        subject_type: Optional[str] = None,
    ) -> dict[str, Any]:
        """Post a single pull-request review comment on the dedicated endpoint.

        Unlike the embedded ``comments`` array of ``create_pull_request_review``,
        ``POST /pulls/{number}/comments`` accepts ``subject_type``, so it is the
        only way to attach a finding to a file as a whole (``subject_type="file"``)
        rather than to a specific diff line. Posting one comment at a time also
        isolates failures: a single off-diff comment 422s on its own without
        sinking a whole review's worth of inline feedback.

        Preconditions:
            - Exactly one anchor is supplied: either ``line`` (a 1-based line
              present in the diff for ``commit_id``) or ``subject_type="file"``.
            - ``line``, when supplied, is >= 1 (GitHub uses 1-based line numbers).
            - ``subject_type``, when supplied, is ``"file"`` (the only value this
              method posts — a file-level anchor).
            - ``side`` selects the diff side for a line comment (``"RIGHT"`` = the
              new file, the default; ``"LEFT"`` = the old file). Ignored when
              ``subject_type`` is used.
            - ``side``, when a line comment is posted, is ``"RIGHT"`` or ``"LEFT"``.
            - ``path`` (non-empty) names a file the PR changes; ``commit_id`` is the
              PR head SHA.
            - ``body`` is a non-empty string, already token-scrubbed by the caller
              (this method does not scrub, matching ``create_pull_request_review``).
        Postconditions:
            - Returns the created-comment payload (carries ``id`` and
              ``html_url``). Raises ``GitHubAPIError`` on any non-2xx response so
              the caller can catch a 422 and degrade.
            - Raises ``ValueError`` when a precondition is violated (neither or
              both of ``line``/``subject_type`` supplied, a non-positive ``line``,
              a ``subject_type`` other than ``"file"``, an invalid ``side``, or an
              empty ``path``/``body``), rather than sending an ambiguous request
              GitHub would reject with an opaque 422.
        """
        if not path or not body:
            raise ValueError("create_review_comment requires non-empty 'path' and 'body'")
        if (line is None) == (subject_type is None):
            raise ValueError(
                "create_review_comment requires exactly one of 'line' or 'subject_type'"
            )
        if line is not None and line < 1:
            raise ValueError("create_review_comment 'line' must be a 1-based line number (>= 1)")
        if line is not None and side not in ("LEFT", "RIGHT"):
            raise ValueError("create_review_comment 'side' must be 'LEFT' or 'RIGHT'")
        if subject_type is not None and subject_type != "file":
            raise ValueError("create_review_comment 'subject_type' must be 'file'")
        json_body: dict[str, Any] = {
            "commit_id": commit_id,
            "path": path,
            "body": body,
        }
        if line is not None:
            json_body["line"] = line
            json_body["side"] = side
        if subject_type is not None:
            json_body["subject_type"] = subject_type
        r = self._check(
            self._request(
                "POST",
                f"/repos/{owner}/{repo}/pulls/{number}/comments",
                json=json_body,
            )
        )
        return r.json()

    # ----- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP connection pool.

        Postconditions:
            - The wrapped ``httpx.Client`` is closed; the ``GitHubClient`` must not
              be used for further requests afterward. Also invoked by ``__exit__``
              when the client is used as a context manager.
        """
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
    "IssueComment",
    "MAX_ISSUES_TRAVERSED",
    "MAX_REVIEW_COMMENTS_TRAVERSED",
    "MAX_REVIEW_THREADS_TRAVERSED",
    "NotAnIssueError",
    "PullRequest",
    "PullRequestDetail",
    "PullRequestFile",
    "Repo",
    "ReviewComment",
    "SubIssue",
    "scrub_token_from_text",
]
