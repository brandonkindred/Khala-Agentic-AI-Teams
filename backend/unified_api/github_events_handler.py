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
import threading
import time
from concurrent import futures
from itertools import islice
from typing import Any

from shared_env_config import env_float, env_int
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

# Marker `GitHubClient.add_issue_comment` appends to every comment Khala posts (an HTML
# comment — invisible in GitHub's rendered view). Comments carrying it are Khala's own
# output and must never re-trigger a review, even when they quote "@khala review" (e.g. a
# review finding echoing a diff that contains the command). Khala posts with the
# operator's PAT, so author identity CANNOT be the loop guard: the PAT owner is often
# exactly the maintainer expected to trigger reviews from PR comments, and filtering by
# author would silently break their commands. Duplicated from
# ``coding_team.github_source.client.KHALA_COMMENT_MARKER`` (this module must stay
# importable without the coding-team package at module scope); a cross-module test
# asserts the two literals stay equal.
_KHALA_COMMENT_MARKER = "<!-- khala-generated -->"


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
        wrong scheme, or mismatch returns ``False``. Never raises — the digests are
        compared as ``bytes`` (both ``.encode("utf-8")``d), NOT as ``str``, because
        ``hmac.compare_digest`` raises ``TypeError`` on a ``str`` with non-ASCII
        characters and Starlette decodes header bytes as latin-1, so an attacker-supplied
        ``X-Hub-Signature-256`` with a byte >= 0x80 would otherwise crash verification
        (turning a would-be 401 into an unauthenticated 500). A non-ASCII ``provided``
        simply encodes to bytes that cannot match the ASCII-hex expected → ``False``.
    """
    if not secret or not signature_header:
        return False
    scheme, _, provided = signature_header.partition("=")
    if scheme != "sha256" or not provided:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


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


def _add_comment_reaction(owner: str, repo: str, comment_id: int, token: str | None, content: str = "eyes") -> None:
    """Best-effort reaction on the triggering comment (👀 acknowledgement, 😕 failure).

    Preconditions: ``token`` is the GitHub PAT to authenticate with, or ``None`` when no
        credential is available; ``content`` is a GitHub reaction name (``eyes`` for "a
        review is on it", ``confused`` for "your request was seen but could not run").
    Postconditions: adds the reaction to comment ``comment_id`` only when both a token
        and a non-zero comment id are present; otherwise a no-op. Never raises — the
        reaction is a courtesy signal and must not block (or fail) the review flow.
    """
    if not comment_id or not token:
        return
    try:
        from coding_team.github_source.client import GitHubClient

        with GitHubClient(token) as client:
            client.create_comment_reaction(owner, repo, comment_id, content=content)
    except Exception:
        # The reaction is a courtesy signal; a failure here must not prevent the review
        # from starting (or mask why it didn't).
        logger.warning("GitHub webhook: could not add reaction to comment %s", comment_id, exc_info=True)


def _start_review(owner: str, repo: str, pr_number: int, token: str | None) -> str:
    """Start the existing PR-review flow for ``pr_number`` (runs the async helper).

    Preconditions: ``owner``/``repo`` are the repository the caller already validated
        against the configured integration; ``pr_number`` is a positive PR number;
        ``token`` is a pre-resolved GitHub PAT reused so the review path does not
        re-read the credential store, or ``None`` to let that path resolve it.
    Postconditions: forwards to the same ``_start_pr_review`` path used by
        ``POST /api/integrations/github/review-pr``, passing ``owner``/``repo`` as the
        expected target so the review is refused (not run against a different repo) if the
        configured owner/repo changed since dispatch validated them. Returns one of:
        - ``"started"`` — a review job was created;
        - ``"already_running"`` — the coding team answered 409 because a review is
          already in flight for this PR (the *intended* duplicate outcome: logged at
          info, never as an error);
        - ``"failed"`` — any other failure (stale-config 409, service unreachable,
          unexpected error), logged at warning/error.
        Errors are logged, not raised — the webhook has already returned 200 and there
        is no client to surface them to.
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

        from fastapi import HTTPException

        from unified_api.routes.integrations import _start_pr_review

        try:
            result = asyncio.run(
                _start_pr_review(pr_number, None, token=token, expected_owner=owner, expected_repo=repo)
            )
        except HTTPException as exc:
            # 409 "already running" is the duplicate guard doing its job — an expected
            # outcome, not an error. Match on the coding team's detail text (forwarded
            # verbatim by _start_pr_review) to distinguish it from the stale-config 409.
            if exc.status_code == 409 and "already running" in str(exc.detail):
                logger.info("GitHub webhook: review already running for PR #%s: %s", pr_number, exc.detail)
                return "already_running"
            logger.warning(
                "GitHub webhook: review for PR #%s was not started (HTTP %s): %s",
                pr_number,
                exc.status_code,
                exc.detail,
            )
            return "failed"
        logger.info("GitHub webhook: started review job %s for PR #%s", getattr(result, "job_id", "?"), pr_number)
        return "started"
    except Exception:
        logger.exception("GitHub webhook: failed to start review for PR #%s", pr_number)
        return "failed"


def process_review_request(
    owner: str,
    repo: str,
    pr_number: int,
    comment_id: int,
    delivery_id: str = "",
) -> None:
    """Validate config, start the code review, and signal the outcome. Runs in a worker thread.

    Preconditions: ``owner``/``repo`` are the repository coordinates from the *webhook
        payload* (validated against the configured integration here, on the worker, so
        the config read never blocks the event loop); ``pr_number`` is a positive PR
        number; ``comment_id`` is the triggering comment's id (0 when unknown);
        ``delivery_id`` is the ``X-GitHub-Delivery`` header value ("" when absent).
    Postconditions:
        - Returns without starting a review when the integration is unconfigured/disabled
          or the payload repo does not match the configured owner/repo; in both cases the
          delivery is forgotten (see :func:`_forget_delivery`) so a redelivery after the
          operator fixes the configuration is not swallowed by the dedup table.
        - Resolves the GitHub PAT once and reuses it for the review start and the
          reaction — a single credential-store read per webhook.
        - Signals the outcome on the triggering comment: 👀 when a review started or one
          is already running (either way, a review is in flight for this PR), 😕
          (``confused``) when the request was seen but could not run. On a failed start
          the delivery is forgotten so GitHub's "Redeliver" can retry it.
        - Best-effort throughout — never raises (the webhook has already returned 200).
    """
    cfg_owner, cfg_repo = _configured_owner_repo()
    if not cfg_owner or not cfg_repo:
        logger.warning("GitHub webhook: no configured owner/repo; ignoring review request")
        _forget_delivery(delivery_id)
        return
    if (owner.casefold(), repo.casefold()) != (cfg_owner.casefold(), cfg_repo.casefold()):
        logger.warning(
            "GitHub webhook: repo %s/%s does not match configured %s/%s; ignoring",
            owner,
            repo,
            cfg_owner,
            cfg_repo,
        )
        _forget_delivery(delivery_id)
        return

    token = _resolve_github_token()
    outcome = _start_review(cfg_owner, cfg_repo, pr_number, token)
    if outcome in ("started", "already_running"):
        # Either way a review is in flight for this PR — acknowledge the request.
        _add_comment_reaction(cfg_owner, cfg_repo, comment_id, token, content="eyes")
    else:
        # Seen-but-could-not-run: give the commenter a visible signal (😕) instead of
        # silence, and forget the delivery so GitHub's "Redeliver" button can retry it.
        _add_comment_reaction(cfg_owner, cfg_repo, comment_id, token, content="confused")
        _forget_delivery(delivery_id)


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
# GitHub retries a delivery (same ID) on a non-2xx response or a timeout; this suppresses
# re-dispatch of an identical redelivery that lands on the SAME worker process.
#
# This is only a fast-path optimization, NOT the authoritative duplicate-review guard: it
# is in-memory and per-process, so a redelivery landing on a different worker in a
# multi-worker deployment (the documented `make deploy` prod config) slips past it. The
# real, cross-worker guard lives one layer deeper — the coding-team `POST /review-pr`
# endpoint rejects a second review for a PR that already has a running job (see
# `_running_review_for_pr` there), which also covers the manual UI trigger. This table
# just avoids the wasted HTTP round-trip for the common same-worker retry.
#
# A delivery stays marked seen only while its work is (or ended up) in flight: when the
# review fails to start — or the submitted work is dropped/misconfigured — the worker
# calls :func:`_forget_delivery`, so GitHub's "Redeliver" button (which reuses the same
# delivery GUID) can retry a delivery whose review never actually ran.
#
# Tunable via env; see docs/ENV_VARS.md.
_SEEN_DELIVERY_IDS: dict[str, float] = {}
_SEEN_DELIVERY_LOCK = threading.Lock()
_SEEN_DELIVERY_TTL_S = env_float("GITHUB_WEBHOOK_DEDUP_TTL_S", 600.0, floor=1.0)
_SEEN_DELIVERY_MAX_ENTRIES = env_int("GITHUB_WEBHOOK_DEDUP_MAX_ENTRIES", 1000, floor=2)


def _is_duplicate_delivery(delivery_id: str) -> bool:
    """Return ``True`` if ``delivery_id`` was already processed within the TTL window.

    Preconditions: ``delivery_id`` is the (possibly empty) ``X-GitHub-Delivery`` header
        value.
    Postconditions: an empty ``delivery_id`` is never treated as a duplicate (returns
        ``False``, recording nothing) — GitHub always sends this header on a real
        delivery, so an empty value signals an unusual/test request, not a redelivery.
        Returns ``True`` iff ``delivery_id`` is tracked AND not yet expired; otherwise
        records it (with a fresh ``now + TTL`` expiry, refreshing an expired entry's
        position) and returns ``False``. Expiry is checked lazily per-key (no full-table
        scan per call); bulk reclamation is the size cap, which drops the oldest-inserted
        half once the table exceeds ``_SEEN_DELIVERY_MAX_ENTRIES``. Because every insert
        uses a non-decreasing ``now`` and dict order is insertion order, insertion order
        equals expiry order, so "oldest half" is just the first ``MAX//2`` keys — no sort
        needed. Never raises.

    Invariants: ``len(_SEEN_DELIVERY_IDS) <= _SEEN_DELIVERY_MAX_ENTRIES`` after every call;
        each value is the monotonic-clock expiry of its key. All table mutation happens
        under ``_SEEN_DELIVERY_LOCK`` (held for dict operations only — microseconds):
        dispatch records on the event-loop thread while pool workers may concurrently
        :func:`_forget_delivery` a failed delivery.
    """
    if not delivery_id:
        return False
    now = time.monotonic()
    with _SEEN_DELIVERY_LOCK:
        expires_at = _SEEN_DELIVERY_IDS.get(delivery_id)
        if expires_at is not None and expires_at >= now:
            return True
        # New, or seen-but-expired: (re)record it. Pop first so a refreshed key moves to
        # the end (newest) and insertion order keeps matching expiry order.
        _SEEN_DELIVERY_IDS.pop(delivery_id, None)
        _SEEN_DELIVERY_IDS[delivery_id] = now + _SEEN_DELIVERY_TTL_S
        if len(_SEEN_DELIVERY_IDS) > _SEEN_DELIVERY_MAX_ENTRIES:
            for seen_id in list(islice(_SEEN_DELIVERY_IDS, _SEEN_DELIVERY_MAX_ENTRIES // 2)):
                _SEEN_DELIVERY_IDS.pop(seen_id, None)
        return False


def _forget_delivery(delivery_id: str) -> None:
    """Remove ``delivery_id`` from the dedup table so a redelivery can retry it.

    Preconditions: ``delivery_id`` is the delivery's ``X-GitHub-Delivery`` value, or ``""``.
    Postconditions: the delivery is no longer marked seen (no-op for ``""`` or an unknown
        id). Called by the worker when the dispatched work did not result in a review —
        failed start, config mismatch, or a dropped submission — because GitHub's
        "Redeliver" reuses the same delivery GUID and must not be swallowed for a delivery
        whose review never ran. Never raises.
    """
    if not delivery_id:
        return
    with _SEEN_DELIVERY_LOCK:
        _SEEN_DELIVERY_IDS.pop(delivery_id, None)


def dispatch_github_event(event_type: str, payload: dict[str, Any], delivery_id: str = "") -> None:
    """Validate an ``issue_comment`` webhook and trigger a review when warranted.

    Submits work only when ALL hold (pure payload checks — no I/O on this thread):
    - ``event_type == "issue_comment"`` and ``payload["action"] == "created"``
    - the comment is on a pull request (``issue.pull_request`` present)
    - the comment is not from a bot, and is not Khala's own output (marked with
      ``_KHALA_COMMENT_MARKER`` — Khala posts with the operator's PAT, so only the
      marker, never the author, distinguishes its comments from the operator's
      genuine commands)
    - the body contains ``@khala review``
    - the commenter's ``author_association`` is OWNER/MEMBER/COLLABORATOR
    - ``issue.number`` is a positive non-bool int (the PR number)
    - ``delivery_id`` (the ``X-GitHub-Delivery`` header) has not already been processed
      within the dedup TTL — see :func:`_is_duplicate_delivery`

    The configured-repo match (a settings read — file I/O under a lock shared with
    config writers) deliberately happens on the pool worker inside
    :func:`process_review_request`, NOT here: this function runs synchronously on the
    async route's event-loop thread, and blocking that thread on disk I/O (or behind a
    concurrent config write) would stall every request on the loop. The dedup check runs
    before submission, so a duplicate delivery costs one dict lookup and zero I/O; a
    consequence is that deliveries for non-matching repos are also recorded (and then
    forgotten by the worker) — benign, since the worker forgets any delivery that did
    not start a review.

    Preconditions: ``event_type`` is the value of the ``X-GitHub-Event`` header;
        ``payload`` is the parsed webhook body (already signature-verified by the route);
        ``delivery_id`` is the ``X-GitHub-Delivery`` header value, or ``""`` if absent.
    Postconditions: submits :func:`process_review_request` to the bounded dispatch
        executor (see :func:`_get_dispatch_executor`) exactly once when every condition
        above holds; any unmet condition (including a non-``dict`` ``payload``) is a
        no-op. If the executor rejects the work (process teardown), the delivery is
        forgotten again so a redelivery is not swallowed. Never raises — the submit goes
        through :func:`unified_api.bounded_executor.submit_safely`, which swallows the
        ``RuntimeError`` a shut-down executor would otherwise raise, so this contract
        holds even during process teardown.
    """
    if event_type != "issue_comment":
        return
    # Defensive: the route already rejects a non-object body with 400, but honor the
    # "never raises" contract for any other caller — a non-dict payload can't be an
    # issue_comment event, so no-op rather than letting ``.get`` raise AttributeError.
    if not isinstance(payload, dict):
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
    body = str(comment.get("body", ""))
    if _KHALA_COMMENT_MARKER in body:
        # Khala's own posted comment (see the marker's comment above) — never
        # self-trigger, even when it quotes the command.
        logger.info("GitHub webhook: ignoring @khala review inside a Khala-generated comment")
        return
    if not parse_review_command(body):
        return
    if not is_authorized(str(comment.get("author_association", ""))):
        logger.info(
            "GitHub webhook: ignoring @khala review from unauthorized association %r",
            comment.get("author_association"),
        )
        return

    pr_number = issue.get("number")
    # bool is an int subclass and GitHub PR numbers are strictly positive; reject both
    # here so process_review_request's "positive PR number" precondition always holds.
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        return
    comment_id = comment.get("id")
    comment_id = comment_id if isinstance(comment_id, int) else 0

    repository = payload.get("repository") or {}
    repo_owner = str((repository.get("owner") or {}).get("login", "")).strip()
    repo_name = str(repository.get("name", "")).strip()

    if _is_duplicate_delivery(delivery_id):
        logger.info("GitHub webhook: ignoring redelivered delivery %s", delivery_id)
        return

    submitted = submit_safely(
        _get_dispatch_executor(),
        process_review_request,
        repo_owner,
        repo_name,
        pr_number,
        comment_id,
        delivery_id,
        logger=logger,
        log_prefix="GitHub webhook dispatch",
    )
    if not submitted:
        # The work never ran; don't let the dedup table swallow a redelivery of it.
        _forget_delivery(delivery_id)
