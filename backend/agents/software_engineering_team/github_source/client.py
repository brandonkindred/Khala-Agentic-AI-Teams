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
from urllib.parse import urlsplit

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

# Per-thread comment page size used by the single review-thread GraphQL query
# below (`_REVIEW_THREADS_FULL_QUERY`, shared by both of its callers).
# A thread whose comments exceed this in one page trips the `hasNextPage` check
# near the "review thread has more than N comments" error — named so that check
# (and its error message) can never drift from the query's actual `first:` value.
_REVIEW_THREAD_COMMENTS_PAGE_SIZE = 100

# GitHub's GraphQL page-size maximum; named rather than inlined so the query
# below and any future reader agree on where the number comes from.
_REVIEW_THREADS_PAGE_SIZE = 100

# Hard bound on how many `reviewThreads` PAGES one traversal may fetch.
# `MAX_REVIEW_THREADS_TRAVERSED` bounds NODES, which a well-behaved endpoint
# makes equivalent — but an endpoint returning empty `nodes` with
# `hasNextPage: true` and a fresh `endCursor` every time (a malformed proxy, a
# buggy mock, a compromised endpoint) advances neither counter and would loop
# forever issuing GraphQL requests. Derived from the node cap so the two can
# never disagree, plus slack for pages GitHub returns short.
_MAX_REVIEW_THREAD_PAGES = (
    MAX_REVIEW_THREADS_TRAVERSED // _REVIEW_THREADS_PAGE_SIZE
) + 10

# GraphQL query for the review-thread listing: resolution state (GitHub's REST
# API has no "resolved" field on a review comment, so thread resolution -- the
# "Resolve conversation" button on GitHub -- can only be read via GraphQL's
# `isResolved`) plus the thread's node id and each comment's databaseId.
# Shared by both callers of :meth:`_iter_review_thread_nodes`:
# ``get_resolved_review_thread_comment_ids`` (``strict=False``) only needs
# ``isResolved``/``databaseId`` and simply ignores the extra ``id`` field;
# ``list_review_threads`` (``strict=True``) additionally needs the thread node
# ``id`` so a thread can be resolved via the ``resolveReviewThread`` mutation.
_REVIEW_THREADS_FULL_QUERY = f"""
query($owner: String!, $repo: String!, $number: Int!, $after: String) {{
  repository(owner: $owner, name: $repo) {{
    pullRequest(number: $number) {{
      reviewThreads(first: {_REVIEW_THREADS_PAGE_SIZE}, after: $after) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          id
          isResolved
          comments(first: {_REVIEW_THREAD_COMMENTS_PAGE_SIZE}) {{
            pageInfo {{ hasNextPage }}
            nodes {{ databaseId }}
          }}
        }}
      }}
    }}
  }}
}}
"""

# GraphQL mutation that resolves a review thread (the "Resolve conversation"
# button). The only write-side GraphQL in this client; posted through the same
# ``_request``/``_check`` machinery as the read query.
_RESOLVE_REVIEW_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
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

    ``head_repo_full_name`` is the ``owner/repo`` of the repository the head
    branch lives in — the same repo (equal to the PR's own ``owner/repo``) for
    an ordinary branch, a different ``owner/repo`` for a fork-opened PR, and
    ``""`` when GitHub reports no head repository at all (the fork was deleted
    after the PR was opened). A caller that needs to fetch/push the head branch
    must resolve the correct remote from this field — ``head`` alone is only
    the branch's short ref and is ambiguous for a fork PR.
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
    head_repo_full_name: str = ""


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

    ``author`` is the commenter's GitHub login (``""`` if GitHub omitted
    ``user``, e.g. a deleted account). Callers that trust a marker string in
    ``body`` (e.g. ``KHALA_COMMENT_MARKER``) as proof Khala itself posted a
    comment MUST also check ``author`` against the authenticated identity
    (:meth:`GitHubClient.get_authenticated_login`) — the marker is a public,
    literal string in ``body`` and anyone who can comment on the PR can
    include it, accidentally or deliberately.
    """

    id: int
    path: str
    line: Optional[int]
    body: str
    html_url: str
    author: str = ""
    # GitHub sets `line` to null once a comment's diff hunk becomes outdated
    # (the file changed at that spot since), but keeps `original_line`
    # populated with the comment's line at the time it was posted. Consumers
    # that need SOME line to center on (e.g. a cited-code excerpt) should
    # fall back to this when `line` is None rather than treating an outdated
    # comment as file-level — it reflects the ORIGINAL commit's line
    # numbering, not necessarily the current head's, but is still a much
    # better anchor than none for a file that has not shifted drastically
    # around that spot.
    original_line: Optional[int] = None


@dataclass(frozen=True)
class ReviewThread:
    """One review conversation thread on a pull request.

    ``id`` is GitHub's GraphQL node id for the thread — the handle the
    ``resolveReviewThread`` mutation takes. ``comment_ids`` are the numeric
    ``databaseId``s (same values as :attr:`ReviewComment.id`) of every comment
    in the thread, oldest first, so a reply can target the thread's root
    comment. ``is_resolved`` mirrors GitHub's "Resolve conversation" state.
    """

    id: str
    is_resolved: bool
    comment_ids: tuple[int, ...]


class ReviewThreadsUnavailableError(GitHubAPIError):
    """Raised when a PR's review-thread state could not be fully retrieved.

    Review-thread resolution state is GraphQL-only. When that query fails
    (transport/HTTP error, GraphQL-level error, missing GraphQL permission, or an
    unexpected payload shape on any page), a caller that decides "unresolved vs
    resolved" must NOT treat the unknown state as "everything is unresolved":
    doing so would re-triage already-resolved discussions and post duplicate
    replies. :meth:`GitHubClient.list_review_threads` therefore fails closed by
    raising this instead of returning a partial/empty list. Carried as a
    ``GitHubAPIError`` subclass so existing ``except GitHubAPIError`` handlers
    still catch it.
    """

    def __init__(self, owner: str, repo: str, number: int, detail: str) -> None:
        """Record the PR whose thread state was unavailable and why.

        Postconditions:
            - Initializes the ``GitHubAPIError`` base with status ``0`` (no single
              HTTP status applies — the failure may be transport-level or a GraphQL
              body error) and a message identifying the PR and the underlying detail.
        """
        super().__init__(
            0, f"review-thread state unavailable for {owner}/{repo}#{number}: {detail}"
        )


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
# Bare GitHub credential prefixes (ghp_/gho_/ghu_/ghs_/ghr_ personal, OAuth,
# user-to-server, server-to-server and refresh tokens, plus ``github_pat_``
# fine-grained PATs). A URL-embedded credential is only ONE of the shapes a
# token reaches us in: git can also echo a bare token, and a code-review
# finding routinely quotes the credential line it is flagging. The
# fine-grained family needs its own alternative because its body contains
# underscores, so the ``gh[pousr]_[A-Za-z0-9]+`` shape can never match it.
# Length-bounded (>=20) so ordinary prose like "ghp_x" in a sentence is not
# mangled, while every real GitHub token (36+ chars) matches.
_BARE_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")


def scrub_token_from_text(msg: str) -> str:
    """Best-effort redact GitHub credentials from arbitrary text.

    Covers both shapes a token reaches us in: ``https://user:token@host/...``
    style remote URLs that git echoes to stderr, and a bare ``ghp_``-family or
    ``github_pat_`` fine-grained token appearing anywhere in the text. Applied
    before a message is put into a public issue comment, a job error field, a
    log line, a Temporal activity exception, or an LLM prompt.

    Postconditions:
        - Returns ``msg`` with every URL-embedded credential rewritten to
          ``https://***@`` and every bare ``gh[pousr]_``/``github_pat_`` token
          replaced with ``***``. Best-effort pattern matching, not a proof of absence: a
          caller that knows the exact secret should ALSO redact it by value.
          Pure; never raises. Empty/falsy input is returned unchanged.
    """
    if not msg:
        return msg
    return _BARE_TOKEN_RE.sub("***", _TOKEN_URL_RE.sub("https://***@", msg))


def redact_secret(text: str, secret: Optional[str]) -> str:
    """Redact a KNOWN secret from ``text`` by value, then pattern-scrub the rest.

    :func:`scrub_token_from_text` is pattern matching over arbitrary text and
    is therefore best-effort. When the caller already holds the exact
    credential in scope (a resolved GitHub token, say) it can do strictly
    better: remove that literal first, then let the pattern pass catch any
    OTHER credential shape in the same text.

    Preconditions:
        - ``text`` is arbitrary, possibly untrusted text (git stderr, an
          exception message). ``secret`` is the literal credential in scope, or
          ``None``/empty when the caller holds none.
    Postconditions:
        - Returns ``text`` with every occurrence of a non-empty ``secret``
          replaced by ``***``, then passed through
          :func:`scrub_token_from_text`. Secrets shorter than 8 characters are
          NOT substituted by value (a short literal would mangle unrelated
          text, and no real credential is that short) -- the pattern pass still
          applies. Pure; never raises.
    """
    if not text:
        return text
    if secret and len(secret) >= 8:
        text = text.replace(secret, "***")
    return scrub_token_from_text(text)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


_DIAGNOSTIC_PREVIEW_CHARS = 500


def _preview(value: Any, limit: int = _DIAGNOSTIC_PREVIEW_CHARS) -> str:
    """Bound an arbitrary payload fragment for safe interpolation into a diagnostic.

    Degrade diagnostics in :meth:`GitHubClient._iter_review_thread_nodes` describe
    a malformed GraphQL payload, and a malformed payload is exactly the case where
    ``repr()`` has no useful bound: a response body of any size, or a review-comment
    node carrying a user-authored comment body, would otherwise be copied verbatim
    into a WARNING log line (non-strict mode) or an exception message (strict mode).
    Both are unbounded and, for comment bodies, needlessly reproduce repository text
    written by third parties. Truncating keeps the diagnostic's discriminating
    prefix -- the type and shape of what arrived -- without either cost.

    Preconditions:
        - ``limit`` is a positive int.
    Postconditions:
        - Returns ``repr(value)`` when it is at most ``limit`` characters, otherwise
          its first ``limit`` characters followed by a ``"... (truncated, N chars)"``
          marker naming the full repr length, so a reader can tell a genuinely short
          payload from a clipped one.
        - Pure; never raises for a value whose ``repr()`` succeeds.
    """
    assert limit > 0, "limit must be positive"
    text = repr(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... (truncated, {len(text)} chars)"


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


def pr_detail_from_payload(payload: dict[str, Any]) -> PullRequestDetail:
    """Map one raw GitHub pull-request REST payload onto a :class:`PullRequestDetail`.

    Public (no leading underscore) for the same reason :func:`default_api_base`
    is: callers OUTSIDE this package parse the same GitHub PR payload —
    ``unified_api.routes.integrations`` builds its PR-panel model from one —
    and a second hand-rolled extraction is exactly how the null-``head.repo``
    and missing-field handling below drifts between modules. Reaching across
    the service boundary for an underscore-private name would pin this
    module's internals as an accidental contract instead.

    Preconditions:
        - ``payload`` is a decoded GitHub pull-request object carrying at
          least ``number`` (the one field with no default; a payload without
          it raises ``KeyError``/``ValueError`` rather than inventing a PR).
    Postconditions:
        - Returns a fully populated :class:`PullRequestDetail`; every field
          other than ``number`` falls back to its empty/false default when
          absent or null, with ONE deviation: ``state`` falls back to the
          sentinel ``"open"`` rather than ``""``, since a PR payload with no
          usable ``state`` is far likelier to be an open PR whose field was
          dropped than a PR in no state at all. Pure — no network, no mutation
          of ``payload``.
    """
    head = payload.get("head") or {}
    base = payload.get("base") or {}
    # ``head.repo`` is null when the fork the PR was opened from has since been
    # deleted; treat that the same as "unknown" (empty string) rather than let a
    # dict-typed .get default mask the distinction from an ordinary same-repo PR.
    head_repo = head.get("repo")
    if isinstance(head_repo, dict):
        head_repo_full_name = head_repo.get("full_name") or ""
    else:
        head_repo_full_name = ""
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
        head_repo_full_name=head_repo_full_name,
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
        author=(payload.get("user") or {}).get("login") or "",
        original_line=payload.get("original_line"),
    )


def _issue_comment_from_payload(payload: dict[str, Any]) -> IssueComment:
    return IssueComment(
        id=int(payload["id"]),
        body=payload.get("body") or "",
        html_url=payload.get("html_url") or "",
    )


def _is_already_exists_422(response: httpx.Response) -> bool:
    """True when a 422 ``response`` body reports GitHub's "already exists" error.

    GitHub returns 422 both for genuine "resource already exists" conflicts
    (e.g. creating a label whose name is taken) AND for ordinary validation
    failures (e.g. an invalid label ``color``) — the status code alone cannot
    tell the two apart, so callers that want to swallow only the former must
    inspect the body first.

    Preconditions:
        - ``response`` is the raw ``httpx.Response`` for a request that
          returned status 422 (this function does not itself check the status).
    Postconditions:
        - Returns ``True`` when the response body is JSON containing an
          ``errors`` list with at least one entry whose ``code`` field is
          exactly ``"already_exists"`` (GitHub's documented error code for
          this case), OR whose top-level ``message`` field contains the
          substring "already exist" (case-insensitive), covering endpoints
          that report this case as free text instead of a structured code.
          Returns ``False`` for any other 422 body,
          including one that fails to parse as JSON — never raises.
    """
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    errors = body.get("errors")
    if isinstance(errors, list):
        for err in errors:
            if isinstance(err, dict) and err.get("code") == "already_exists":
                return True
    message = body.get("message")
    if isinstance(message, str) and "already exist" in message.lower():
        return True
    return False


def web_host_for_api_base_url(base_url: str) -> str:
    """The web (clone/browse) host matching a configured GitHub API base URL.

    Standalone so both a :class:`GitHubClient` instance (via the ``web_host``
    property) and code with no client handy (e.g. remote-URL validation that
    only has ``GITHUB_API_URL``/a bare base URL string) can derive the same
    clone/browse host without instantiating a client just to read a property.

    Postconditions:
        - Returns ``"github.com"`` for the default ``api.github.com`` host
          (github.com Cloud's API and web hosts differ by an "api." prefix).
          The comparison is made on the parsed HOSTNAME, so it is both
          case-INSENSITIVE and port-insensitive: RFC 3986 defines the host
          component as case-insensitive and ``https://api.github.com:443`` names
          the same host as ``https://api.github.com``, so neither spelling may
          be mistaken for a GHES instance and returned as its own web host
          (which would derive a clone URL pointing at the API host, which serves
          no git traffic).
        - For any other host (a GitHub Enterprise Server instance, whose API
          and web UI share one host, typically at an ``/api/v3`` path),
          returns that host unchanged — GHES's clone URLs use the bare host,
          not the API path. Falls back to the raw ``base_url`` on any parse
          error OR an empty parsed ``netloc`` (e.g. a relative-path base
          URL) rather than raising, since this only feeds a display/
          clone-URL convenience, never an auth-relevant decision — except
          that a bare ``api.github.com`` host string with no scheme (which
          parses to an empty ``netloc`` too) is still recognized as the cloud
          API host and maps to ``"github.com"``, since that is what such a
          string names.
        - The returned GHES host never carries a userinfo component: a
          ``https://user:token@ghes.example.com/api/v3`` base URL yields
          ``"ghes.example.com"``, so a credential embedded in
          ``GITHUB_API_URL`` cannot reach a clone/browse URL or a log line
          through this function.
    """
    try:
        parsed = urlsplit(base_url)
        # ``hostname`` lowercases and strips any explicit port, so
        # ``https://Api.GitHub.com:443`` still compares equal to the cloud host;
        # ``netloc`` (which keeps the port) is what gets RETURNED for a GHES
        # instance, whose clone URLs need the port when one is configured.
        hostname = parsed.hostname
        host = parsed.netloc or base_url
    except ValueError:
        return base_url
    if (hostname or host).lower() == "api.github.com":
        return "github.com"
    if host and "@" in host:
        # Drop the userinfo component (``user:token@``): ``netloc`` keeps it,
        # and this value feeds clone/browse URLs and operator-facing display,
        # which must never echo a credential embedded in ``GITHUB_API_URL``.
        # ``rsplit`` so a password containing "@" still leaves only the host.
        host = host.rsplit("@", 1)[1]
    # A GHES host is returned in its ORIGINAL casing (only the comparison
    # casefolds): the value feeds clone/browse URLs, where echoing the
    # operator's own spelling back is friendlier than a silently lowercased one.
    return host


def default_api_base() -> str:
    """This deployment's configured GitHub API base URL, normalized.

    THE single source of truth for the ``GITHUB_API_URL`` env var /
    :data:`DEFAULT_BASE_URL` fallback. Public (unlike the private helper it
    replaces) because callers outside this package build GitHub REST URLs of
    their own — ``unified_api.routes.integrations`` most of all — and each
    re-deriving ``os.environ.get("GITHUB_API_URL") or DEFAULT_BASE_URL`` is
    exactly how the env var name, the fallback constant, and the trailing-slash
    normalization drift apart between modules.

    Postconditions:
        - Returns the ``GITHUB_API_URL`` env var when set and non-empty, else
          :data:`DEFAULT_BASE_URL`, with any trailing slash(es) stripped — the
          same normalization :class:`GitHubClient.__init__` applies — so a
          caller building ``f"{default_api_base()}/repos/..."`` never produces
          a double slash from an operator's trailing-slash env var.
    """
    return (os.environ.get("GITHUB_API_URL") or DEFAULT_BASE_URL).rstrip("/")


def configured_web_host() -> str:
    """The web (clone/browse) host for this deployment's configured GitHub API.

    Postconditions:
        - Derives the host the same way a :class:`GitHubClient` constructed
          with no explicit ``base_url`` would (:func:`default_api_base`), via
          :func:`web_host_for_api_base_url` — for use by callers validating a
          remote URL who have no live client instance to read ``web_host``
          from.
    """
    return web_host_for_api_base_url(default_api_base())


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
            - ``base_url`` defaults to :func:`default_api_base` when unset
              (``GITHUB_API_URL`` env var, else :data:`DEFAULT_BASE_URL`), with
              any trailing slash stripped. ``max_retries`` is floored at 1 so a
              retry loop always attempts at least once. ``sleep`` is injectable
              (tests pass a no-op) so retry/backoff delays never actually
              block. Raises ``ValueError`` when ``token`` is empty.
        """
        if not token:
            raise ValueError("GitHubClient requires a token")
        self._token = token
        # `default_api_base()` is already normalized; the rstrip still applies to
        # an explicitly-passed `base_url`, which nothing else normalizes.
        self._base_url = (base_url or default_api_base()).rstrip("/")
        self._timeout = timeout
        self._max_retries = max(1, max_retries)
        self._sleep = sleep
        self._client = httpx.Client(timeout=timeout)

    @property
    def web_host(self) -> str:
        """The web (clone/browse) host matching this client's configured API host.

        Postconditions:
            - See :func:`web_host_for_api_base_url` — this property is a thin
              instance-scoped wrapper over it, applied to ``self._base_url``.
        """
        return web_host_for_api_base_url(self._base_url)

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

    def create_label(
        self, owner: str, repo: str, name: str, color: str = "ededed", description: str = ""
    ) -> None:
        """Create a repository label (``POST /repos/{owner}/{repo}/labels``), idempotently.

        Preconditions:
            - ``name`` is the label's exact display name; ``color`` is a 6-digit hex
              string without a leading ``#`` (GitHub's own format for this field).
        Postconditions:
            - The repository has a label named ``name`` (freshly created with
              ``color``/``description``, or already present from before this call —
              both are treated as success). GitHub responds 422 with an
              ``already_exists`` error code when a label with this name already
              exists; that specific case is swallowed here so callers can call
              this unconditionally as a create-if-missing guard before applying a
              label, without first doing a separate existence check. GitHub also
              returns 422 for other validation failures (e.g. an invalid
              ``color``) — those are inspected for the ``already_exists`` code
              first and, when absent, still raise ``GitHubAPIError`` via
              ``_check``, matching this docstring's contract that an invalid
              color raises rather than being swallowed as "already exists".
              Raises ``GitHubAPIError`` for any other non-2xx (auth, rate limit,
              server error) as well.
        """
        response = self._request(
            "POST",
            f"/repos/{owner}/{repo}/labels",
            json={"name": name, "color": color, "description": description},
        )
        if response.status_code == 422 and _is_already_exists_422(response):
            return
        self._check(response)

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
        return pr_detail_from_payload(r.json())

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

    def _iter_review_thread_nodes(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        query: str,
        strict: bool,
    ) -> Iterator[tuple[Optional[str], bool, tuple[int, ...]]]:
        """Page through a PR's review threads, yielding one entry per thread.

        Shared transport/pagination/cap machinery for both
        :meth:`get_resolved_review_thread_comment_ids` and
        :meth:`list_review_threads`: both callers currently post the same
        GraphQL query (``_REVIEW_THREADS_FULL_QUERY``, passed explicitly via
        ``query`` so a future caller can still substitute its own) and walk
        the exact same ``reviewThreads`` connection the exact same way, but
        need different failure semantics. ``strict`` selects which: ``True``
        (``list_review_threads``) fails closed on ANY anomaly — an unexpected
        payload shape must never be mistaken for "no more threads". ``False``
        (``get_resolved_review_thread_comment_ids``, which never uses the
        thread's ``id`` field even though the query returns it) degrades
        instead: a malformed or missing field is treated as absent (skipped,
        or a threads page as exhausted) rather than raised, since a
        resolution-lookup only loses de-duplication, never correctness, when
        data is missing. "Absent" is load-bearing for ``isResolved`` in
        particular: it degrades to ``False`` (unresolved), never to a
        ``bool()`` coercion, because an inverted-to-``True`` value would
        suppress an unresolved thread instead of merely losing de-duplication.

        Preconditions:
            - ``number`` names an existing pull request.
            - ``query`` is one of the module's ``reviewThreads`` GraphQL query
              constants (same response shape assumed below).
        Postconditions:
            - Yields ``(thread_id, is_resolved, comment_ids)`` once per review
              thread, in GitHub's response order, until every page is
              consumed or (``strict=False`` only) an anomaly ends the walk
              early. ``thread_id`` is ``None`` unless ``query`` requested it
              and it parsed as a non-empty string. The missing-id check is
              unconditional (it does not know what ``query`` asked for), so
              under ``strict=True`` a ``query`` that omits the ``id`` field
              makes EVERY thread invalid and aborts the walk on the first one:
              an id-less ``query`` is only meaningful with ``strict=False``,
              where it yields ``thread_id=None`` per thread as documented
              above.
            - ``strict=True`` raises :class:`ReviewThreadsUnavailableError`
              itself only for GraphQL-level errors and payload-shape
              anomalies: an unexpected payload shape, exceeding
              :data:`MAX_REVIEW_THREADS_TRAVERSED` or
              :data:`_MAX_REVIEW_THREAD_PAGES`, a thread missing/invalid
              ``id``, or a thread with more than
              :data:`_REVIEW_THREAD_COMMENTS_PAGE_SIZE` comments. A genuine
              GraphQL transport/HTTP error (a non-2xx status from the initial
              ``self._check(self._request(...))`` call) is NOT caught here in
              either mode — it propagates as ``GitHubAPIError``/an ordinary
              exception, per this client's established non-2xx contract. What
              each caller does with that propagated error is documented on
              that caller.
            - ``strict=False`` never raises for shape anomalies (an unexpected
              payload shape, a malformed node, etc.) — those degrade to
              whatever was accumulated so far. A genuine transport/HTTP error
              or malformed JSON response still propagates as an ordinary
              exception in this mode too, for the caller's own broad
              ``except`` to catch.
            - EVERY degrade point is announced in both modes — strict raises,
              non-strict logs a warning naming ``owner``/``repo``/``number``
              (see the ``_unavailable`` helper below) — so this generator has
              no path that silently returns less data than it found. That
              includes the two cases where non-strict still yields the thread:
              a non-bool ``isResolved`` (yielded as ``False`` — UNRESOLVED —
              rather than coerced via ``bool()``, which would turn a truthy
              non-bool like the string ``"false"`` into ``True`` and make a
              resolution-lookup consumer SKIP an unresolved thread; treating
              unknown state as unresolved is safe in both directions, since
              re-addressing an already-resolved thread is harmless while
              suppressing an unresolved one is not), and a thread whose
              comments do not fit one
              :data:`_REVIEW_THREAD_COMMENTS_PAGE_SIZE` page (only the first
              page's ids are yielded).
            - A thread whose ``comments`` payload is UNREADABLE (not a dict, or
              its ``nodes`` not a list) is never yielded with an empty comment
              tuple — the guarantee that holds in BOTH modes. What happens
              instead differs by mode: strict aborts the walk with
              ``ReviewThreadsUnavailableError``, non-strict skips the thread and
              announces the loss with the warning. A consumer cannot distinguish "this
              thread has no comments" from "this thread's comments could not be
              read", and reads the former as "nothing outstanding here" —
              enough for ``address_comments`` to resolve a thread whose real
              comments were never addressed. Skipping loses the thread from the
              listing, which the warning announces; yielding it loses its
              comments while claiming completeness.
            - A threads page whose ``pageInfo`` is a dict but carries a
              missing or non-bool ``hasNextPage`` is an anomaly, not an
              implicit "no more pages": strict raises and non-strict warns
              before ending the walk, since ending pagination on unknown state
              is exactly the silent partial result strict mode exists to
              prevent. The SAME rule applies one level down, to each thread's
              ``comments.pageInfo``: a missing or non-bool ``hasNextPage``
              there is announced too, rather than read as "this thread's
              comments all fit on one page".
            - Traversal is bounded on BOTH axes: at most
              :data:`MAX_REVIEW_THREADS_TRAVERSED` thread nodes and at most
              :data:`_MAX_REVIEW_THREAD_PAGES` pages. The page bound is what
              terminates a walk against an endpoint that returns empty
              ``nodes`` with ``hasNextPage: true`` forever (which advances the
              node count not at all); exceeding either is announced through
              ``_unavailable`` like every other degrade point.
        """
        def _unavailable(message: str) -> None:
            """Fail closed (``strict``) or log-and-degrade (non-strict) for one anomaly.

            The strict/non-strict duality is the same at every one of this
            generator's degrade points, so it lives here ONCE instead of being
            re-spelled inline per site: the caller decides what to do with
            control flow afterwards (``return``, ``continue``, or yield a
            partial thread), this decides only whether the anomaly is fatal.

            Preconditions:
                - ``message`` describes the anomaly and is safe to log (it is
                  also the ``ReviewThreadsUnavailableError`` detail in strict
                  mode).
            Postconditions:
                - ``strict``: raises :class:`ReviewThreadsUnavailableError`
                  carrying ``owner``/``repo``/``number`` and ``message``.
                - Non-strict: logs a warning naming ``owner``/``repo``/
                  ``number`` and ``message``, and returns ``None`` — the call
                  site then degrades however it chooses. Never raises.
            """
            if strict:
                raise ReviewThreadsUnavailableError(owner, repo, number, message)
            logger.warning(
                "_iter_review_thread_nodes: %s/%s#%s: %s", owner, repo, number, message
            )

        after: Optional[str] = None
        seen = 0
        pages = 0
        while True:
            pages += 1
            if pages > _MAX_REVIEW_THREAD_PAGES:
                # Announced like every other degrade point rather than silently
                # returning what we have: an endpoint that keeps claiming more
                # pages without ever advancing the node count is broken, and a
                # truncated listing must never look complete.
                _unavailable(
                    f"exceeded _MAX_REVIEW_THREAD_PAGES={_MAX_REVIEW_THREAD_PAGES} "
                    f"reviewThreads pages after {seen} threads; stopping"
                )
                return
            variables: dict[str, Any] = {
                "owner": owner,
                "repo": repo,
                "number": number,
                "after": after,
            }
            payload = self._execute_graphql(query, variables)
            if not isinstance(payload, dict):
                _unavailable(f"non-object GraphQL response: {_preview(payload)}")
                return
            if payload.get("errors"):
                _unavailable(f"GraphQL errors: {payload['errors']}")
                return
            data = payload.get("data")
            repository = data.get("repository") if isinstance(data, dict) else None
            pr_data = repository.get("pullRequest") if isinstance(repository, dict) else None
            if not isinstance(pr_data, dict):
                _unavailable("missing pullRequest payload")
                return
            review_threads = pr_data.get("reviewThreads")
            if not isinstance(review_threads, dict) or not isinstance(
                review_threads.get("nodes"), list
            ):
                _unavailable(f"invalid reviewThreads payload: {_preview(review_threads)}")
                return
            for node in review_threads["nodes"]:
                seen += 1
                if seen > MAX_REVIEW_THREADS_TRAVERSED:
                    # Exceeding the cap means the listing is incomplete; strict
                    # fails closed rather than yielding a partial view a caller
                    # could mistake for the full set.
                    _unavailable(
                        f"exceeded MAX_REVIEW_THREADS_TRAVERSED={MAX_REVIEW_THREADS_TRAVERSED}; "
                        "stopping"
                    )
                    return
                if not isinstance(node, dict):
                    _unavailable(
                        f"invalid review-thread node: {_preview(node)}; a non-strict caller "
                        "would skip it"
                    )
                    continue
                thread_id = node.get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    _unavailable(
                        "review-thread node has a missing or invalid id (expected non-empty "
                        f"str, got {thread_id!r}); a non-strict caller would yield it with a "
                        "None id"
                    )
                    thread_id = None
                is_resolved_raw = node.get("isResolved")
                if not isinstance(is_resolved_raw, bool):
                    # Fail SAFE, not merely loud: bool() would turn a truthy
                    # non-bool (the string "false", say) into True, and a True
                    # here makes ``get_resolved_review_thread_comment_ids``
                    # classify a genuinely UNRESOLVED thread as resolved, so
                    # address-comments never addresses it — silent data loss.
                    # Unknown resolution state therefore reads as UNRESOLVED:
                    # the worst case is addressing an already-resolved thread
                    # redundantly, which is harmless in both directions.
                    _unavailable(
                        "review-thread node has a missing or invalid isResolved (expected "
                        f"bool, got {is_resolved_raw!r}); a non-strict caller would treat "
                        "the thread as unresolved"
                    )
                    is_resolved = False
                else:
                    is_resolved = is_resolved_raw
                comments = node.get("comments")
                if not isinstance(comments, dict) or not isinstance(comments.get("nodes"), list):
                    # SKIP the thread rather than yielding it with an empty
                    # comment tuple. "No comments" and "comments unreadable"
                    # are different facts, and this generator's consumers
                    # cannot tell them apart: address-comments would treat an
                    # unreadable thread as a thread with nothing outstanding
                    # and resolve it, discarding comments it never read. That
                    # is the same silent data loss the invalid-node path above
                    # already avoids by skipping, so this matches it.
                    _unavailable(
                        f"invalid review-thread comments payload (thread {thread_id!r}): "
                        f"{_preview(comments)}; a non-strict caller would skip the thread"
                    )
                    continue
                # Per-thread comment pagination is checked in BOTH modes: a
                # thread with more comments than one page holds would otherwise
                # yield only its first page's ids in non-strict mode, and the
                # address-comments run would reply to and resolve a thread whose
                # later comments it never saw. Strict fails closed; non-strict
                # warns and proceeds with the first page.
                comments_page_info = comments.get("pageInfo")
                if not isinstance(comments_page_info, dict):
                    _unavailable(
                        f"invalid review-thread comments pageInfo (thread {thread_id!r}): "
                        f"{comments_page_info!r}; a non-strict caller would yield only the "
                        "first page of comment ids"
                    )
                else:
                    # Same rule as the threads-level pageInfo below: hasNextPage
                    # must be PRESENT and a bool. A dict carrying no (or a
                    # non-bool) hasNextPage says NOTHING about whether this
                    # thread has more comments, and a bare truthiness test reads
                    # that unknown state as "no more comments" -- silently
                    # yielding a first page as if it were the whole thread, in
                    # BOTH modes. It would also read a truthy non-bool like the
                    # string "false" as "more comments", warning spuriously.
                    comments_has_next = comments_page_info.get("hasNextPage")
                    if not isinstance(comments_has_next, bool):
                        _unavailable(
                            f"review-thread comments pageInfo (thread {thread_id!r}) has a "
                            "missing or invalid hasNextPage (expected bool, got "
                            f"{comments_has_next!r}); a non-strict caller would yield only "
                            "the first page of comment ids"
                        )
                    elif comments_has_next:
                        _unavailable(
                            f"review thread {thread_id!r} has more than "
                            f"{_REVIEW_THREAD_COMMENTS_PAGE_SIZE} comments; a non-strict "
                            "caller would yield only the first page of comment ids"
                        )
                comment_ids_list: list[int] = []
                for comment in comments["nodes"]:
                    # ``bool`` subclasses ``int``, so a JSON ``true`` would
                    # otherwise pass as an id and enter the resolved-id set as
                    # the integer 1 (``True == 1`` and hashes equal). Excluded
                    # explicitly, matching the same rule applied to
                    # ``hasNextPage`` above.
                    database_id = comment.get("databaseId") if isinstance(comment, dict) else None
                    if (
                        not isinstance(comment, dict)
                        or isinstance(database_id, bool)
                        or not isinstance(database_id, int)
                    ):
                        # "missing OR invalid", matching every sibling
                        # diagnostic in this function: this branch also fires
                        # for a PRESENT id that is a bool or a non-int, and
                        # calling those "missing" misdirects anyone debugging
                        # a fail-closed strict-mode error.
                        _unavailable(
                            "review comment has a missing or invalid databaseId "
                            f"(expected non-bool int, got {database_id!r}; comment "
                            f"{_preview(comment)}, thread {thread_id!r})"
                        )
                        continue
                    comment_ids_list.append(database_id)
                yield thread_id, is_resolved, tuple(comment_ids_list)
            page_info = review_threads.get("pageInfo")
            if not isinstance(page_info, dict):
                _unavailable(f"invalid reviewThreads pageInfo: {page_info!r}")
                return
            # ``hasNextPage`` must be PRESENT and a bool: a page whose pageInfo
            # is a dict but carries no (or a non-bool) hasNextPage tells us
            # nothing about whether more threads exist, so treating it as
            # "no more pages" would end pagination on unknown state — exactly
            # the silent partial result strict mode exists to prevent.
            has_next_page = page_info.get("hasNextPage")
            if not isinstance(has_next_page, bool):
                _unavailable(
                    "reviewThreads pageInfo has a missing or invalid hasNextPage "
                    f"(expected bool, got {has_next_page!r})"
                )
                return
            if not has_next_page:
                return
            after = page_info.get("endCursor")
            if not isinstance(after, str) or not after:
                # Type-checked like every sibling validation in this function
                # (``pageInfo`` dict, ``hasNextPage`` bool, ``databaseId``
                # non-bool int): a truthy NON-string cursor would otherwise be
                # forwarded as ``after`` on the next request and surface as a
                # raw GraphQL type error instead of this structured
                # fail-closed diagnostic. "missing or invalid", since a
                # present-but-wrongly-typed cursor is not missing.
                _unavailable(
                    "reviewThreads page has a missing or invalid endCursor "
                    f"(expected non-empty str, got {after!r})"
                )
                return

    def get_resolved_review_thread_comment_ids(
        self, owner: str, repo: str, number: int
    ) -> set[int]:
        """Return the ids of every review comment belonging to a RESOLVED review thread.

        GitHub's REST API has no "resolved" field on a review comment — thread
        resolution (the "Resolve conversation" button) is exposed only via the
        GraphQL API's ``isResolved``. Posted through the same
        ``_request``/``_check`` machinery as every REST call (``_absolute_url``
        joins ``/graphql`` onto ``base_url`` unchanged), as a ``POST`` with a
        ``{"query", "variables"}`` body. Consumes the shared
        :meth:`_iter_review_thread_nodes` pagination/cap machinery in its
        degrade-to-empty (``strict=False``) mode, then also wraps the call in
        its own broad ``except`` for the genuine transport/HTTP/JSON failures
        that mode still lets through.

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
              response shape is logged as a warning and degrades to whatever
              was accumulated so far (possibly empty) — a resolution-lookup
              failure must not fail an otherwise working review. Treating
              every comment as unresolved in that case only means a duplicate
              finding is kept and cross-referenced rather than dropped, never
              silently lost.
        """
        resolved: set[int] = set()
        try:
            for _thread_id, is_resolved, comment_ids in self._iter_review_thread_nodes(
                owner, repo, number, query=_REVIEW_THREADS_FULL_QUERY, strict=False
            ):
                if is_resolved:
                    resolved.update(comment_ids)
        except Exception as e:  # noqa: BLE001 - a resolution-lookup failure must degrade to
            # whatever was gathered so far, never fail the review (see the "Never
            # raises" postcondition above); covers the genuine transport/HTTP/JSON
            # failures the generator's own degrade-to-empty mode still lets through.
            logger.warning(
                "get_resolved_review_thread_comment_ids failed for %s/%s#%s: %s",
                owner,
                repo,
                number,
                e,
            )
        return resolved

    def list_review_threads(self, owner: str, repo: str, number: int) -> list["ReviewThread"]:
        """Return every review thread on a pull request (GraphQL).

        Unlike :meth:`get_resolved_review_thread_comment_ids` (which returns only
        the comment ids of *resolved* threads), this returns the full set of
        threads with their GraphQL node ``id`` and per-thread comment
        ``databaseId``s, so a caller can (a) tell resolved from unresolved and
        (b) reply to / resolve a specific thread. Consumes the shared
        :meth:`_iter_review_thread_nodes` pagination/cap machinery in its
        fail-closed (``strict=True``) mode directly — any anomaly it raises
        propagates as this method's own fail-closed contract.

        Preconditions:
            - ``number`` names an existing pull request.
        Postconditions:
            - Returns one :class:`ReviewThread` per thread, in GitHub's response
              order, each carrying its node ``id``, ``is_resolved``, and the
              ordered tuple of its comments' numeric ids — the COMPLETE set of
              threads, provided every thread's own comments fit within
              :data:`_REVIEW_THREAD_COMMENTS_PAGE_SIZE` (this method does not
              paginate WITHIN a thread's comments, only across threads).
            - Fails closed: any GraphQL transport/HTTP error, non-2xx status,
              GraphQL-level error, unexpected payload shape, exceeding
              :data:`MAX_REVIEW_THREADS_TRAVERSED`, or any single thread having
              more than :data:`_REVIEW_THREAD_COMMENTS_PAGE_SIZE` comments (i.e.
              any case where the returned list would be partial or the state
              unknown) raises :class:`ReviewThreadsUnavailableError` instead of
              returning a partial/empty list. A caller that classifies
              "unresolved vs resolved" must not treat unknown state as
              unresolved, so this never silently degrades. (This differs
              deliberately from :meth:`get_resolved_review_thread_comment_ids`,
              which degrades to an empty set because a review only loses
              de-duplication, not correctness, when resolution state is
              missing.)
            - The fail-closed contract holds END-TO-END, not just for the
              anomalies :meth:`_iter_review_thread_nodes` raises itself: that
              generator deliberately lets a genuine transport/HTTP/JSON error
              propagate as an ordinary exception in both modes, and the broad
              ``except`` below re-raises any such exception as
              :class:`ReviewThreadsUnavailableError`. The wrapper lives here,
              next to the ``except`` that implements it, rather than in the
              generator's own contract.
        """
        threads: list[ReviewThread] = []
        try:
            for thread_id, is_resolved, comment_ids in self._iter_review_thread_nodes(
                owner, repo, number, query=_REVIEW_THREADS_FULL_QUERY, strict=True
            ):
                if not thread_id:
                    # Fail closed EXPLICITLY rather than via `assert`: strict=True
                    # already makes the generator raise for a missing or invalid
                    # id, so this is unreachable in practice -- but an assert is
                    # stripped under `python -O`, and there the fallthrough would
                    # build a ReviewThread with a falsy id that a caller would
                    # then try to resolve. The fail-closed contract this method
                    # advertises must not depend on assertions being enabled.
                    raise ReviewThreadsUnavailableError(
                        owner,
                        repo,
                        number,
                        "review-thread node yielded without an id despite strict=True",
                    )
                threads.append(
                    ReviewThread(id=thread_id, is_resolved=is_resolved, comment_ids=comment_ids)
                )
            return threads
        except ReviewThreadsUnavailableError:
            # Already the fail-closed signal — propagate as-is.
            raise
        except Exception as e:  # noqa: BLE001 - any other failure is also "state unknown"
            logger.warning("list_review_threads failed for %s/%s#%s: %s", owner, repo, number, e)
            raise ReviewThreadsUnavailableError(owner, repo, number, str(e)) from e

    def reply_to_review_comment(
        self, *, owner: str, repo: str, number: int, comment_id: int, body: str
    ) -> dict[str, Any]:
        """Post a threaded reply under an existing review comment.

        Uses ``POST /repos/{owner}/{repo}/pulls/{number}/comments/{comment_id}/replies``,
        the REST endpoint dedicated to replying inside an existing review thread
        (so the reply lands in the same conversation as the comment being
        addressed, not as a new top-level review comment).

        Preconditions:
            - ``comment_id`` is the numeric id of a review comment on this PR (a
              :attr:`ReviewComment.id`, ideally the thread's root comment).
            - ``body`` is a non-empty string (already token-scrubbed by the
              caller, matching the other ``create_*`` methods here).
        Postconditions:
            - Returns the created reply payload (carries ``id`` and ``html_url``).
              Raises ``ValueError`` for an empty ``body`` (rather than sending a
              request GitHub would reject with an opaque 422) and
              ``GitHubAPIError`` on any non-2xx response.
            - Posts ``body`` with :data:`KHALA_COMMENT_MARKER` appended when not
              already present (matching :meth:`add_issue_comment`/
              :meth:`create_issue`'s provenance convention) so a caller that
              later re-reads review comments (e.g. ``_unresolved_comments``'s
              marker check) can recognize — and skip — Khala's own reply even
              if a subsequent step (like resolving the thread) failed and the
              same comment is re-triaged on a retry.
        """
        if not body:
            raise ValueError("reply_to_review_comment requires a non-empty 'body'")
        if KHALA_COMMENT_MARKER not in body:
            body = f"{body}\n\n{KHALA_COMMENT_MARKER}"
        r = self._check(
            self._request(
                "POST",
                f"/repos/{owner}/{repo}/pulls/{number}/comments/{comment_id}/replies",
                json={"body": body},
            )
        )
        return r.json()

    def _execute_graphql(self, query: str, variables: dict[str, Any]) -> Any:
        """POST a GraphQL document and return the raw, parsed response payload.

        Shared transport plumbing for :meth:`resolve_review_thread` and
        :meth:`_iter_review_thread_nodes`, which otherwise hand-roll the same
        ``POST /graphql`` + JSON-decode pattern independently.

        Preconditions:
            - ``query`` is a GraphQL document string; ``variables`` is its
              variables mapping.
        Postconditions:
            - Returns ``response.json()`` for the request. Raises
              ``GitHubAPIError`` on transport/HTTP failure (a non-2xx status),
              via the same ``self._check(self._request(...))`` contract every
              other method in this client uses. Callers own the
              payload-level contract entirely: this helper does not inspect
              ``payload.get("errors")`` or unwrap ``payload["data"]`` — each
              call site keeps its own distinct failure semantics (fail-closed
              vs. degrade-and-log) for those.
            - JSON decoding is NOT wrapped: a 2xx response whose body is not
              valid JSON raises ``requests.exceptions.JSONDecodeError`` (a
              ``ValueError`` subclass) straight out of ``response.json()``, not
              ``GitHubAPIError``. Callers that must not let a malformed body
              escape as an unexpected ``ValueError`` catch broadly —
              :meth:`list_review_threads` does exactly that and re-raises it as
              :class:`ReviewThreadsUnavailableError`.
        """
        response = self._check(
            self._request("POST", "/graphql", json={"query": query, "variables": variables})
        )
        return response.json()

    def resolve_review_thread(self, thread_id: str) -> bool:
        """Mark a review thread resolved (GitHub's "Resolve conversation", GraphQL).

        Preconditions:
            - ``thread_id`` is a review thread's GraphQL node id (a
              :attr:`ReviewThread.id`).
        Postconditions:
            - Returns True when GitHub reports the thread resolved after the
              mutation. Returns False (never raises) on any GraphQL transport/HTTP
              error, GraphQL-level error, or unexpected response shape — resolving
              is the last, best-effort step of addressing a comment, so a failure
              to flip the switch must not fail the whole flow (the reply and code
              change already landed).
        """
        try:
            payload = self._execute_graphql(
                _RESOLVE_REVIEW_THREAD_MUTATION, {"threadId": thread_id}
            )
            if payload.get("errors"):
                logger.warning(
                    "resolve_review_thread: GraphQL errors for thread %s: %s",
                    thread_id,
                    payload["errors"],
                )
                return False
            thread = ((payload.get("data") or {}).get("resolveReviewThread") or {}).get(
                "thread"
            ) or {}
            return bool(thread.get("isResolved"))
        except Exception as e:  # noqa: BLE001 - best-effort resolve; degrade to False
            logger.warning("resolve_review_thread failed for thread %s: %s", thread_id, e)
            return False

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
        content, _missing = self.get_file_contents_detailed(owner, repo, path, ref)
        return content

    def get_file_contents_detailed(
        self, owner: str, repo: str, path: str, ref: str
    ) -> tuple[Optional[str], bool]:
        """Like :meth:`get_file_contents`, but also reports whether GitHub responded 404.

        Preconditions:
            - Same as :meth:`get_file_contents`.
        Postconditions:
            - Returns ``(content, missing)``. ``content`` follows exactly the same
              rules as :meth:`get_file_contents`. ``missing`` is ``True`` only when
              GitHub responded 404 to this exact ``(path, ref)`` request — which
              does NOT prove the path itself is absent: GitHub also 404s when
              ``ref`` itself doesn't resolve (a branch/tag/SHA that doesn't
              exist) or when the repository is inaccessible, and this method
              cannot distinguish those cases from a genuinely-missing path.
              Callers that need to tell them apart must verify ``ref`` resolves
              separately. Directory/non-file entries and undecodable payloads
              report ``missing=False`` alongside ``content=None`` (GitHub gave
              no 404 for those). Raises ``GitHubAPIError`` only for a non-404
              error status, same as :meth:`get_file_contents`.
            - JSON decoding is NOT wrapped: a 2xx response whose body is not
              valid JSON raises ``requests.exceptions.JSONDecodeError`` (a
              ``ValueError`` subclass) straight out of ``response.json()``, not
              ``GitHubAPIError`` — the same uncaught shape :meth:`_execute_graphql`
              documents. Callers that must not let a malformed body escape as an
              unexpected ``ValueError`` have to catch it themselves.
        """
        response = self._request(
            "GET", f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref}
        )
        if response.status_code == 404:
            return None, True
        payload = self._check(response).json()
        if not isinstance(payload, dict) or payload.get("type") != "file":
            return None, False
        if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
            return None, False
        try:
            raw = base64.b64decode(payload["content"])
        except (binascii.Error, ValueError):
            return None, False
        return raw.decode("utf-8", errors="replace"), False

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
    "ReviewThread",
    "ReviewThreadsUnavailableError",
    "SubIssue",
    "redact_secret",
    "scrub_token_from_text",
]
