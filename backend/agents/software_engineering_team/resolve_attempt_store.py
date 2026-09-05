"""Postgres data-access for the address-comments resolve-attempt ledger.

``address_comments._unresolved_comments`` cannot tell, from GitHub's read APIs
alone, whether an unresolved thread whose latest message is Khala's own
generated reply means "our resolve mutation failed, retry it" or "a reviewer
clicked Reopen conversation with no new comment" — ``PullRequestReviewThread``
exposes ``isResolved`` as a plain boolean with no history/audit trail, and both
cases look identical on read. This module lets the resolve step itself record,
per thread, whether ITS OWN resolve call ran and failed, so the next run can
tell "we know this failed" (safe to retry) from "no evidence either way"
(ambiguous — treat as a possible reviewer reopen, never auto-resolve).

One row per thread with a known-failed resolve lives in
``address_comments_resolve_attempts`` (see
``software_engineering_team.postgres``), keyed by
``(owner, repo, pr_number, thread_id)``.

Design contract:
  * **Keyed on raw ``owner``/``repo`` casing.** Unlike ``idx_code_review_runs_pr_ci``'s
    ``lower(owner), lower(repo)`` normalization, every function here matches on
    the exact strings given. All current callers (``address_comments``) pass
    the casing GitHub's REST/GraphQL responses return for a given PR
    consistently within a run, so this is safe today; a caller that mixed
    casing for the same repo across calls would degrade to "no evidence"
    (never crash — see the read contract below), not corrupt data.
  * **Writes are best-effort.** ``record_resolve_failure``,
    ``clear_resolve_attempt``, and ``clear_resolve_attempts_for_pr`` never raise: persistence here
    is a safety net, not a prerequisite for the resolve step itself, so a
    missing/unreachable Postgres degrades to "no evidence" rather than failing
    the run. No-ops when ``POSTGRES_HOST`` is unset.
  * **Reads degrade to "no evidence".** ``has_recorded_resolve_failure``
    returns False when Postgres is unavailable, the query fails, or no
    matching row exists — the safer default, since "no evidence" routes the
    thread away from the auto-resolve retry path (see
    ``address_comments._unresolved_comments``).
  * **Cleanup.** A row is deleted as soon as the thread the ledger recorded a
    failure for is actually resolved (``clear_resolve_attempt``, called from
    both the initial reply-and-resolve step and the retry loop on success), and
    in bulk once a PR is discovered no longer open
    (``clear_resolve_attempts_for_pr``, called from the retry loop's PR-state
    check) — an address-comments run never touches a closed PR again, so any
    rows left for it would otherwise never be cleared. Rows for a thread whose
    resolve has never yet been attempted, or last attempt succeeded, never
    exist in the first place.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Callable, Optional

from shared.postgres import get_conn, is_postgres_enabled
from shared.postgres.helpers import best_effort_write as _shared_best_effort_write
from shared.postgres.metrics import timed_query

logger = logging.getLogger(__name__)
_STORE = "coding_team"
# Named for the same reason ``review_history_store._REVIEW_RUNS_TABLE`` is: the
# table name reaches the best-effort-write label and the SQL below, and a bare
# literal in the ``partial`` drifts from the SQL silently. The SQL still spells
# the name inline -- interpolating a table name into a query string is the one
# habit worth not establishing here, even from a module constant.
_RESOLVE_ATTEMPTS_TABLE = "address_comments_resolve_attempts"

_best_effort_write: Callable[[str, Callable[[], None]], None] = partial(
    _shared_best_effort_write, _RESOLVE_ATTEMPTS_TABLE
)


@timed_query(store=_STORE, op="record_resolve_failure")
def record_resolve_failure(
    owner: str, repo: str, pr_number: int, thread_id: str, khala_reply_comment_id: Optional[int]
) -> None:
    """Record that Khala's own resolve mutation for ``thread_id`` just failed.

    Preconditions:
        - ``thread_id`` is a review thread's GraphQL node id
          (:attr:`ReviewThread.id`); ``khala_reply_comment_id`` is the id of
          the Khala-generated reply the failed resolve followed, when known.
    Postconditions:
        - Upserts a row keyed by ``(owner, repo, pr_number, thread_id)``,
          always refreshing ``failed_at``.
        - ``khala_reply_comment_id`` is updated only when the NEW value is
          non-NULL; a later failure recorded with an UNKNOWN reply id keeps the
          id already on record (``COALESCE``) rather than nulling it out.
          Overwriting it with NULL would destroy the very evidence this ledger
          exists to hold: :func:`has_recorded_resolve_failure` matches the
          stored id against the caller's with ``IS NOT DISTINCT FROM``, so a
          NULLed row no longer matches the real reply id and the thread silently
          reverts to "no evidence". A row can therefore carry an id recorded by
          an EARLIER attempt than its ``failed_at`` -- deliberate: an id that was
          right once still identifies the Khala reply the failures concern,
          while NULL identifies nothing.
        - Never raises; no-op without Postgres.
    """
    if not is_postgres_enabled():
        return

    def _write() -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO address_comments_resolve_attempts
                      (owner, repo, pr_number, thread_id, khala_reply_comment_id, failed_at)
                   VALUES (%s, %s, %s, %s, %s, NOW())
                   ON CONFLICT (owner, repo, pr_number, thread_id) DO UPDATE SET
                       khala_reply_comment_id = COALESCE(
                           EXCLUDED.khala_reply_comment_id,
                           address_comments_resolve_attempts.khala_reply_comment_id
                       ),
                       failed_at = EXCLUDED.failed_at""",
                (owner, repo, pr_number, thread_id, khala_reply_comment_id),
            )

    _best_effort_write("record_resolve_failure", _write)


@timed_query(store=_STORE, op="has_recorded_resolve_failure")
def has_recorded_resolve_failure(
    owner: str, repo: str, pr_number: int, thread_id: str, khala_reply_comment_id: Optional[int]
) -> bool:
    """Return whether a resolve attempt for THIS thread/reply is on record as failed.

    Preconditions:
        - ``khala_reply_comment_id`` is the id of the Khala-generated reply
          currently the thread's latest message (from a fresh
          ``list_review_comments`` read) — ``Optional`` only to keep this
          read's signature symmetric with :func:`record_resolve_failure`'s
          write side (which can store ``NULL`` when the reply's id could not
          be captured); every current caller passes a real int, since
          ``_unresolved_comments`` only calls this for an already-fetched
          comment.
    Postconditions:
        - Returns True only when a row exists for ``(owner, repo, pr_number,
          thread_id)`` AND its recorded ``khala_reply_comment_id`` matches the
          one given, compared NULL-safely (``IS NOT DISTINCT FROM`` — plain
          ``=`` never matches when either side is ``NULL``, which would
          silently make a failure recorded with an unknown reply id
          permanently unreadable) — a row recorded against an older reply
          (superseded by a newer Khala reply since) does not count as
          evidence for the current one. Returns False on any Postgres error,
          when Postgres is disabled, or when no matching row exists — "no
          evidence" is the safe default, since callers use this to decide
          whether it is safe to auto-resolve. Never raises.
    """
    if not is_postgres_enabled():
        return False
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM address_comments_resolve_attempts
                   WHERE owner = %s AND repo = %s AND pr_number = %s AND thread_id = %s
                     AND khala_reply_comment_id IS NOT DISTINCT FROM %s""",
                (owner, repo, pr_number, thread_id, khala_reply_comment_id),
            )
            return cur.fetchone() is not None
    except Exception:  # noqa: BLE001 - degrade to "no evidence" rather than error
        logger.warning(
            "%s: has_recorded_resolve_failure failed",
            _RESOLVE_ATTEMPTS_TABLE,
            exc_info=True,
        )
        return False


@timed_query(store=_STORE, op="clear_resolve_attempt")
def clear_resolve_attempt(owner: str, repo: str, pr_number: int, thread_id: str) -> None:
    """Delete any recorded failed-resolve evidence for ``thread_id`` (best-effort).

    Postconditions:
        - No row remains for ``(owner, repo, pr_number, thread_id)``. A no-op
          when none existed. Never raises; no-op without Postgres.
    """
    if not is_postgres_enabled():
        return

    def _write() -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """DELETE FROM address_comments_resolve_attempts
                   WHERE owner = %s AND repo = %s AND pr_number = %s AND thread_id = %s""",
                (owner, repo, pr_number, thread_id),
            )

    _best_effort_write("clear_resolve_attempt", _write)


@timed_query(store=_STORE, op="clear_resolve_attempts_for_pr")
def clear_resolve_attempts_for_pr(owner: str, repo: str, pr_number: int) -> None:
    """Delete every recorded failed-resolve row for a PR (best-effort).

    Called once a PR is discovered no longer open: address-comments never
    revisits a closed PR, so any rows recorded for it would otherwise persist
    indefinitely.

    Postconditions:
        - No row remains for ``(owner, repo, pr_number)``. Never raises;
          no-op without Postgres.
    """
    if not is_postgres_enabled():
        return

    def _write() -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """DELETE FROM address_comments_resolve_attempts
                   WHERE owner = %s AND repo = %s AND pr_number = %s""",
                (owner, repo, pr_number),
            )

    _best_effort_write("clear_resolve_attempts_for_pr", _write)
