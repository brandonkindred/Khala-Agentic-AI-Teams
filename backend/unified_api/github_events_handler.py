"""
GitHub webhook handler: receive PR-comment events from GitHub and trigger the
code-review agent when a collaborator comments ``@khala review`` on a pull request.

Supports:
- ``ping`` events (GitHub's webhook setup probe) — handled by the route itself
- ``issue_comment`` events (``action == "created"``) on pull requests
- Signature verification via HMAC-SHA256 (``X-Hub-Signature-256``)

The actual review is the existing PR-review flow (``POST /api/integrations/github/review-pr``
→ coding-team ``/review-pr``); this module only recognizes the command and starts it.
All heavy work runs in a background thread so the webhook returns a fast 2xx, matching
GitHub's delivery-timeout expectation.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Commenters whose association with the repo authorizes a review trigger. Outside
# contributors (CONTRIBUTOR / FIRST_TIMER / NONE / ...) are intentionally excluded so a
# drive-by comment cannot spend review budget.
_AUTHORIZED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# Matches "@khala review" anywhere in the comment, case-insensitive, tolerant of extra
# whitespace between the mention and the command.
_REVIEW_COMMAND = re.compile(r"@khala\s+review\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def verify_github_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """Verify a GitHub webhook payload using its ``X-Hub-Signature-256`` header.

    Args:
        secret: The webhook signing secret configured on both GitHub and Khala.
        body: Raw request body bytes (must be the unmodified delivered bytes).
        signature_header: Value of the ``X-Hub-Signature-256`` header
            (``sha256=<hex digest>``).

    Preconditions: ``secret`` is a non-empty string (callers skip verification when no
        secret is configured).
    Postconditions: returns ``True`` only when the header is a well-formed
        ``sha256=...`` digest that matches an HMAC-SHA256 of ``body`` under ``secret``,
        compared with :func:`hmac.compare_digest` (constant-time). Any malformed header,
        wrong scheme, or mismatch returns ``False``. Never raises.
    """
    if not secret or not signature_header:
        return False
    scheme, _, provided = signature_header.partition("=")
    if scheme != "sha256" or not provided:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


# ---------------------------------------------------------------------------
# Command + authorization parsing (pure helpers)
# ---------------------------------------------------------------------------


def parse_review_command(comment_body: str) -> bool:
    """Return ``True`` if the comment body contains the ``@khala review`` command."""
    if not comment_body:
        return False
    return _REVIEW_COMMAND.search(comment_body) is not None


def is_authorized(author_association: str) -> bool:
    """Return ``True`` if the commenter's repo association may trigger a review.

    GitHub reports ``author_association`` as one of OWNER/MEMBER/COLLABORATOR/
    CONTRIBUTOR/FIRST_TIMER/FIRST_TIME_CONTRIBUTOR/MANNEQUIN/NONE. Only the first three
    are treated as authorized.
    """
    return str(author_association or "").strip().upper() in _AUTHORIZED_ASSOCIATIONS


def _comment_is_from_bot(comment: dict[str, Any]) -> bool:
    """True when the comment was authored by a bot (avoid reacting to our own output)."""
    user = comment.get("user") or {}
    return str(user.get("type", "")).strip().lower() == "bot"


# ---------------------------------------------------------------------------
# Review trigger (runs in a background thread)
# ---------------------------------------------------------------------------


def _resolve_github_token() -> str | None:
    """Return the configured GitHub PAT, or ``None`` if unavailable.

    Postconditions: reads the encrypted PAT via the credential store; never raises.
    """
    try:
        from unified_api.integration_credentials import get_credential_status

        token, _ = get_credential_status("github", "personal_access_token")
        return token or None
    except Exception:
        logger.exception("GitHub webhook: failed to resolve PAT for reaction")
        return None


def _add_eyes_reaction(owner: str, repo: str, comment_id: int) -> None:
    """Best-effort 👀 reaction on the triggering comment (never raises)."""
    if not comment_id:
        return
    token = _resolve_github_token()
    if not token:
        return
    try:
        from coding_team.github_source.client import GitHubClient

        with GitHubClient(token) as client:
            client.create_comment_reaction(owner, repo, comment_id, content="eyes")
    except Exception:
        # The reaction is a courtesy acknowledgement; a failure here must not prevent
        # the review from starting.
        logger.warning("GitHub webhook: could not add reaction to comment %s", comment_id, exc_info=True)


def _start_review(pr_number: int) -> None:
    """Start the existing PR-review flow for ``pr_number`` (runs the async helper).

    Postconditions: forwards to the same ``_start_pr_review`` path used by
        ``POST /api/integrations/github/review-pr``. Errors are logged, not raised — the
        webhook has already returned 200 and there is no client to surface them to.
    """
    try:
        import asyncio

        from unified_api.routes.integrations import _start_pr_review

        result = asyncio.run(_start_pr_review(pr_number, None))
        logger.info("GitHub webhook: started review job %s for PR #%s", getattr(result, "job_id", "?"), pr_number)
    except Exception:
        logger.exception("GitHub webhook: failed to start review for PR #%s", pr_number)


def process_review_request(owner: str, repo: str, pr_number: int, comment_id: int) -> None:
    """Acknowledge with a reaction, then start the code review. Runs in a worker thread."""
    _add_eyes_reaction(owner, repo, comment_id)
    _start_review(pr_number)


# ---------------------------------------------------------------------------
# Event dispatch (called from the route handler)
# ---------------------------------------------------------------------------


def _configured_owner_repo() -> tuple[str, str]:
    """Return the configured (owner, repo), or ("", "") if unavailable."""
    try:
        from unified_api.integrations_store import get_github_config_meta

        meta = get_github_config_meta()
        return str(meta.get("owner", "")).strip(), str(meta.get("repo", "")).strip()
    except Exception:
        logger.exception("GitHub webhook: failed to read configured owner/repo")
        return "", ""


def dispatch_github_event(event_type: str, payload: dict[str, Any]) -> None:
    """Validate an ``issue_comment`` webhook and trigger a review when warranted.

    Acts only when ALL hold:
    - ``event_type == "issue_comment"`` and ``payload["action"] == "created"``
    - the comment is on a pull request (``issue.pull_request`` present)
    - the comment is not from a bot
    - the body contains ``@khala review``
    - the commenter's ``author_association`` is OWNER/MEMBER/COLLABORATOR
    - the repository matches the configured owner/repo (never review an arbitrary repo)

    On all checks passing, spawns a daemon thread running :func:`process_review_request`.
    Any unmet check is a silent no-op. Never raises.
    """
    if event_type != "issue_comment":
        return
    if str(payload.get("action", "")).strip() != "created":
        return

    issue = payload.get("issue") or {}
    if not issue.get("pull_request"):
        # A comment on a regular issue, not a PR — nothing to review.
        return

    comment = payload.get("comment") or {}
    if _comment_is_from_bot(comment):
        return
    if not parse_review_command(str(comment.get("body", ""))):
        return
    if not is_authorized(str(comment.get("author_association", ""))):
        logger.info(
            "GitHub webhook: ignoring @khala review from unauthorized association %r",
            comment.get("author_association"),
        )
        return

    repository = payload.get("repository") or {}
    repo_owner = str((repository.get("owner") or {}).get("login", "")).strip()
    repo_name = str(repository.get("name", "")).strip()
    cfg_owner, cfg_repo = _configured_owner_repo()
    if not cfg_owner or not cfg_repo:
        logger.warning("GitHub webhook: no configured owner/repo; ignoring review request")
        return
    if (repo_owner.lower(), repo_name.lower()) != (cfg_owner.lower(), cfg_repo.lower()):
        logger.warning(
            "GitHub webhook: repo %s/%s does not match configured %s/%s; ignoring",
            repo_owner,
            repo_name,
            cfg_owner,
            cfg_repo,
        )
        return

    pr_number = issue.get("number")
    if not isinstance(pr_number, int):
        return
    comment_id = comment.get("id")
    comment_id = comment_id if isinstance(comment_id, int) else 0

    threading.Thread(
        target=process_review_request,
        args=(repo_owner, repo_name, pr_number, comment_id),
        daemon=True,
    ).start()
