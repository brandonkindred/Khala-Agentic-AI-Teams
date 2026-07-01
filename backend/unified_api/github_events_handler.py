"""
GitHub webhook handler: receive PR-comment events from GitHub and trigger the
code-review agent when a collaborator comments ``@khala review`` on a pull request.

Supports:
- ``ping`` events (GitHub's webhook setup probe) — handled by the route itself
- ``issue_comment`` events (``action == "created"``) on pull requests
- Signature verification via HMAC-SHA256 (``X-Hub-Signature-256``)

The actual review is the existing PR-review flow (``POST /api/integrations/github/review-pr``
→ coding-team ``/review-pr``); this module only recognizes the command and starts it.
All heavy work runs on a bounded background thread pool so the webhook returns a fast
2xx, matching GitHub's delivery-timeout expectation.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from concurrent import futures
from typing import Any

from unified_api.bounded_executor import get_or_recreate_executor, submit_safely

logger = logging.getLogger(__name__)

# Bounds webhook-triggered review work to a fixed pool instead of spawning an
# unbounded OS thread per delivery (a burst of "@khala review" comments would
# otherwise create one thread each). Mirrors the pattern already used for the
# health-check probe pool in unified_api/main.py (`_get_probe_executor`) — both share
# the lazy-create/recreate-after-shutdown logic via `bounded_executor`.
_DISPATCH_EXECUTOR: futures.ThreadPoolExecutor | None = None


def _get_dispatch_executor() -> futures.ThreadPoolExecutor:
    """Return the shared bounded executor for webhook-triggered review work.

    Preconditions: none.
    Postconditions: returns a process-wide ``ThreadPoolExecutor`` (max_workers=4),
        created lazily on first use. Recreates it if a prior instance was already shut
        down, so tests that shut it down between runs don't leave the module unusable.
        Never raises. The check-then-assign on ``_DISPATCH_EXECUTOR`` is not
        thread-locked: this is safe because the only caller, :func:`dispatch_github_event`,
        is invoked synchronously from the async ``github_events`` route handler — i.e.
        always on the single asyncio event-loop thread, with no ``await`` in between —
        so two calls can never interleave. If a future caller ever invoked this from a
        second OS thread, the worst case is two callers both observing ``None``/shut-down
        and each creating a throwaway executor (one gets discarded) — wasted construction,
        not a correctness bug.
    """
    global _DISPATCH_EXECUTOR
    _DISPATCH_EXECUTOR = get_or_recreate_executor(
        _DISPATCH_EXECUTOR, max_workers=4, thread_name_prefix="gh-webhook-review"
    )
    return _DISPATCH_EXECUTOR


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
    """Return ``True`` if the comment body contains the ``@khala review`` command.

    Preconditions: ``comment_body`` is the raw comment text (may be empty or ``None``).
    Postconditions: returns ``True`` iff a case-insensitive ``@khala review`` token (with
        a trailing word boundary) appears anywhere in the body; ``False`` otherwise. Pure;
        never raises.
    """
    if not comment_body:
        return False
    return _REVIEW_COMMAND.search(comment_body) is not None


def is_authorized(author_association: str) -> bool:
    """Return ``True`` if the commenter's repo association may trigger a review.

    GitHub reports ``author_association`` as one of OWNER/MEMBER/COLLABORATOR/
    CONTRIBUTOR/FIRST_TIMER/FIRST_TIME_CONTRIBUTOR/MANNEQUIN/NONE.

    Preconditions: ``author_association`` is the comment's association string (may be
        empty or ``None``).
    Postconditions: returns ``True`` only for OWNER/MEMBER/COLLABORATOR (case-insensitive);
        every other value, including empty/``None``, returns ``False``. Pure; never raises.
    """
    return str(author_association or "").strip().upper() in _AUTHORIZED_ASSOCIATIONS


def _comment_is_from_bot(comment: dict[str, Any]) -> bool:
    """True when the comment was authored by a bot (avoid reacting to our own output).

    Preconditions: ``comment`` is the webhook payload's ``comment`` object (may be empty).
    Postconditions: returns ``True`` iff ``comment.user.type`` is ``"Bot"``
        (case-insensitive). Pure; never raises.
    """
    user = comment.get("user") or {}
    return str(user.get("type", "")).strip().lower() == "bot"


# ---------------------------------------------------------------------------
# Review trigger (runs on the bounded dispatch executor)
# ---------------------------------------------------------------------------


def _resolve_github_token() -> str | None:
    """Return the configured GitHub PAT, or ``None`` if unavailable.

    Preconditions: none.
    Postconditions: reads the encrypted PAT via the credential store; never raises.
    """
    try:
        from unified_api.integration_credentials import get_credential_status

        token, _ = get_credential_status("github", "personal_access_token")
        return token or None
    except Exception:
        logger.exception("GitHub webhook: failed to resolve PAT for reaction")
        return None


def _add_eyes_reaction(owner: str, repo: str, comment_id: int, token: str | None) -> None:
    """Best-effort 👀 reaction on the triggering comment.

    Preconditions: ``token`` is the GitHub PAT to authenticate with, or ``None`` when no
        credential is available.
    Postconditions: adds an ``eyes`` reaction to comment ``comment_id`` only when both a
        token and a non-zero comment id are present; otherwise a no-op. Never raises — the
        reaction is a courtesy acknowledgement and must not block the review.
    """
    if not comment_id or not token:
        return
    try:
        from coding_team.github_source.client import GitHubClient

        with GitHubClient(token) as client:
            client.create_comment_reaction(owner, repo, comment_id, content="eyes")
    except Exception:
        # The reaction is a courtesy acknowledgement; a failure here must not prevent
        # the review from starting.
        logger.warning("GitHub webhook: could not add reaction to comment %s", comment_id, exc_info=True)


def _start_review(pr_number: int, token: str | None) -> None:
    """Start the existing PR-review flow for ``pr_number`` (runs the async helper).

    Preconditions: ``pr_number`` is a positive PR number; ``token`` is a pre-resolved
        GitHub PAT reused so the review path does not re-read the credential store, or
        ``None`` to let that path resolve it.
    Postconditions: forwards to the same ``_start_pr_review`` path used by
        ``POST /api/integrations/github/review-pr``. Errors are logged, not raised — the
        webhook has already returned 200 and there is no client to surface them to.
        Runs on a bounded executor worker thread (see :func:`_get_dispatch_executor`),
        which has no event loop of its own, so a fresh ``asyncio.run()`` per call is the
        standard way to run this one coroutine to completion — the same shape as
        ``asyncio.to_thread``/``loop.run_in_executor`` — and does not create N
        concurrently-running loops beyond the pool's ``max_workers`` bound. A persistent
        per-thread loop (``asyncio.new_event_loop()`` + ``run_until_complete`` reused
        across calls) would save the loop create/destroy cost, but "@khala review"
        comments are a low-volume, human-triggered path, not a hot loop — the added
        lifecycle management (loop cleanup on thread exit, cross-call state) isn't worth
        it for a per-call cost measured in microseconds.
    """
    try:
        import asyncio

        from unified_api.routes.integrations import _start_pr_review

        result = asyncio.run(_start_pr_review(pr_number, None, token=token))
        logger.info("GitHub webhook: started review job %s for PR #%s", getattr(result, "job_id", "?"), pr_number)
    except Exception:
        logger.exception("GitHub webhook: failed to start review for PR #%s", pr_number)


def process_review_request(owner: str, repo: str, pr_number: int, comment_id: int) -> None:
    """Acknowledge with a 👀 reaction, then start the code review. Runs in a worker thread.

    Preconditions: ``owner``/``repo`` are the (config-matched) repository coordinates;
        ``pr_number`` is a positive PR number; ``comment_id`` is the triggering comment's
        id (0 when unknown).
    Postconditions: resolves the GitHub PAT once and reuses it for both the reaction and
        the review start, so a single credential-store read serves the whole webhook.
        Best-effort throughout — neither a failed reaction nor a failed review start
        raises (the webhook has already returned 200).
    """
    token = _resolve_github_token()
    _add_eyes_reaction(owner, repo, comment_id, token)
    _start_review(pr_number, token)


# ---------------------------------------------------------------------------
# Event dispatch (called from the route handler)
# ---------------------------------------------------------------------------


def _configured_owner_repo() -> tuple[str, str]:
    """Return the configured (owner, repo) when the integration is enabled, else ("", "").

    Preconditions: none.
    Postconditions: returns the stripped owner/repo from the GitHub config settings
        (JSON only — no credential read) ONLY when ``enabled`` is true. A disabled
        integration reports as unconfigured even if owner/repo/PAT are still saved from
        before it was turned off (the PUT path preserves them on disable), so the
        webhook path never submits review work — and never adds the 👀 acknowledgement
        reaction — for an integration the operator turned off. Returns ``("", "")`` when
        nothing is configured, the integration is disabled, or the settings read fails.
        Never raises.
    """
    try:
        from unified_api.integrations_store import get_github_config_meta

        meta = get_github_config_meta()
        if not meta.get("enabled"):
            return "", ""
        return str(meta.get("owner", "")).strip(), str(meta.get("repo", "")).strip()
    except Exception:
        logger.exception("GitHub webhook: failed to read configured owner/repo")
        return "", ""


# Best-effort de-dup of GitHub webhook redeliveries by the `X-GitHub-Delivery` header.
# GitHub retries a delivery (same ID) on a non-2xx response or a timeout; without this,
# a retried "@khala review" comment would start a second review job for the same PR.
# In-memory only — bounded (TTL + max-entries) so it can't grow unbounded, but it does
# NOT catch a redelivery landing on a different worker process in a multi-worker
# deployment (each process has its own table). Reduces, does not eliminate, duplicates.
_SEEN_DELIVERY_IDS: dict[str, float] = {}
_SEEN_DELIVERY_TTL_S = 600.0
_SEEN_DELIVERY_MAX_ENTRIES = 1000


def _is_duplicate_delivery(delivery_id: str) -> bool:
    """Return ``True`` if ``delivery_id`` was already processed within the TTL window.

    Preconditions: ``delivery_id`` is the (possibly empty) ``X-GitHub-Delivery`` header
        value.
    Postconditions: an empty ``delivery_id`` is never treated as a duplicate (returns
        ``False``, recording nothing) — GitHub always sends this header on a real
        delivery, so an empty value signals an unusual/test request, not a redelivery.
        Otherwise, prunes expired entries, then returns ``True`` (recording nothing
        further) if ``delivery_id`` is already tracked; else records it — expiring after
        ``_SEEN_DELIVERY_TTL_S`` — and returns ``False``. Caps memory by dropping the
        oldest half of entries once the table exceeds ``_SEEN_DELIVERY_MAX_ENTRIES``.
        Never raises.
    """
    if not delivery_id:
        return False
    now = time.monotonic()
    for seen_id, expires_at in list(_SEEN_DELIVERY_IDS.items()):
        if expires_at < now:
            _SEEN_DELIVERY_IDS.pop(seen_id, None)
    if delivery_id in _SEEN_DELIVERY_IDS:
        return True
    _SEEN_DELIVERY_IDS[delivery_id] = now + _SEEN_DELIVERY_TTL_S
    if len(_SEEN_DELIVERY_IDS) > _SEEN_DELIVERY_MAX_ENTRIES:
        oldest = sorted(_SEEN_DELIVERY_IDS.items(), key=lambda kv: kv[1])[: _SEEN_DELIVERY_MAX_ENTRIES // 2]
        for seen_id, _ in oldest:
            _SEEN_DELIVERY_IDS.pop(seen_id, None)
    return False


def dispatch_github_event(event_type: str, payload: dict[str, Any], delivery_id: str = "") -> None:
    """Validate an ``issue_comment`` webhook and trigger a review when warranted.

    Acts only when ALL hold:
    - ``event_type == "issue_comment"`` and ``payload["action"] == "created"``
    - the comment is on a pull request (``issue.pull_request`` present)
    - the comment is not from a bot
    - the body contains ``@khala review``
    - the commenter's ``author_association`` is OWNER/MEMBER/COLLABORATOR
    - the repository matches the configured owner/repo (never review an arbitrary repo)
    - ``delivery_id`` (the ``X-GitHub-Delivery`` header) has not already been processed
      within the dedup TTL — see :func:`_is_duplicate_delivery`

    Preconditions: ``event_type`` is the value of the ``X-GitHub-Event`` header;
        ``payload`` is the parsed webhook body (already signature-verified by the route);
        ``delivery_id`` is the ``X-GitHub-Delivery`` header value, or ``""`` if absent.
    Postconditions: submits :func:`process_review_request` to the bounded dispatch
        executor (see :func:`_get_dispatch_executor`) exactly once when every condition
        above holds; any unmet condition is a silent no-op. Never raises (it returns
        before the submitted work does any I/O) — the submit itself goes through
        :func:`unified_api.bounded_executor.submit_safely`, which swallows the
        ``RuntimeError`` a shut-down executor (or interpreter-shutdown race) would
        otherwise raise, so this contract holds even during process teardown.
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

    if _is_duplicate_delivery(delivery_id):
        logger.info("GitHub webhook: ignoring redelivered delivery %s", delivery_id)
        return

    submit_safely(
        _get_dispatch_executor(),
        process_review_request,
        repo_owner,
        repo_name,
        pr_number,
        comment_id,
        logger=logger,
        log_prefix="GitHub webhook dispatch",
    )
