"""Postgres data-access for persisted PR code-review history.

One row per executed review lives in ``code_review_runs`` (see
``coding_team.postgres``). The Code Review page reads this back so a pull
request shows every review it has had, with each review's status and outcome,
surviving page reloads and server restarts.

Design contract:
  * **Writes are best-effort.** ``record_review_start`` / ``update_review``
    never raise and never block the review: persistence is a side benefit, not
    a prerequisite, so a missing/unreachable Postgres degrades to "no history"
    rather than failing the review. They no-op when ``POSTGRES_HOST`` is unset.
  * **Reads degrade to empty.** ``list_reviews`` returns ``[]`` when Postgres
    is unavailable or the query fails, so the page renders without history
    rather than erroring.

Mirrors the ``agent_console`` store idioms (``get_conn`` / ``Json`` /
``dict_row`` / ``@timed_query``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Json

from shared_postgres import get_conn, is_postgres_enabled
from shared_postgres.metrics import timed_query

logger = logging.getLogger(__name__)
_STORE = "coding_team"

# Columns `update_review` is permitted to write. The SET clause is composed only
# from these constants — never from caller-supplied identifiers — so the query
# can never carry user-controlled SQL.
_UPDATABLE_COLUMNS = frozenset(
    {"status", "status_text", "review_summary", "error", "completed_at"}
)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


@timed_query(store=_STORE, op="record_review_start")
def record_review_start(
    job_id: str,
    owner: str,
    repo: str,
    pr_number: int,
    pr_url: Optional[str],
    author: str,
) -> None:
    """Insert a ``pending`` row for a newly-started review (best-effort).

    Preconditions:
        - ``job_id`` is the review job's id (unique per review).
    Postconditions:
        - On success a ``code_review_runs`` row exists for ``job_id``. A
          duplicate ``job_id`` is ignored. Never raises; no-op without Postgres.
    """
    if not is_postgres_enabled():
        return
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO code_review_runs
                      (job_id, owner, repo, pr_number, pr_url, status, author, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (job_id) DO NOTHING""",
                (job_id, owner, repo, pr_number, pr_url, "pending", author, _now()),
            )
    except Exception:  # noqa: BLE001 - persistence must never break the review
        logger.warning("code_review_runs: record_review_start failed", exc_info=True)


@timed_query(store=_STORE, op="update_review")
def update_review(
    job_id: str,
    *,
    status: str,
    status_text: Optional[str] = None,
    review_summary: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
    completed: bool = False,
) -> None:
    """Update a review row's status/outcome (best-effort).

    Preconditions:
        - ``record_review_start`` was called for ``job_id`` (a missing row is a
          no-op, not an error).
    Postconditions:
        - The row's ``status`` and any provided fields are updated;
          ``completed_at`` is stamped when ``completed`` is True. Never raises;
          no-op without Postgres.
    """
    if not is_postgres_enabled():
        return
    # Build the SET clause from (column, value) pairs whose column names are all
    # hardcoded constants below. The composed clause can therefore never carry a
    # user-controlled identifier; an assertion against the allowlist makes that
    # invariant explicit and fails loudly if a future edit introduces a stray one.
    assignments: list[tuple[str, Any]] = [("status", status)]
    if status_text is not None:
        assignments.append(("status_text", status_text))
    if review_summary is not None:
        assignments.append(("review_summary", Json(review_summary)))
    if error is not None:
        assignments.append(("error", error))
    if completed:
        assignments.append(("completed_at", _now()))
    columns = [col for col, _ in assignments]
    assert set(columns) <= _UPDATABLE_COLUMNS, f"non-allowlisted column(s): {set(columns) - _UPDATABLE_COLUMNS}"
    set_clause = ", ".join(f"{col} = %s" for col in columns)
    params: list[Any] = [val for _, val in assignments]
    params.append(job_id)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE code_review_runs SET {set_clause} WHERE job_id = %s",
                params,
            )
    except Exception:  # noqa: BLE001 - persistence must never break the review
        logger.warning("code_review_runs: update_review failed", exc_info=True)


@timed_query(store=_STORE, op="list_reviews")
def list_reviews(
    owner: str,
    repo: str,
    pr_number: Optional[int] = None,
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return review rows for a repo (optionally one PR), newest first.

    Preconditions:
        - ``owner``/``repo`` name the configured repository.
    Postconditions:
        - Returns up to ``limit`` rows ordered by ``created_at`` DESC. Returns
          ``[]`` when Postgres is unavailable or the query fails (never raises).
    """
    if not is_postgres_enabled():
        return []
    limit = max(1, min(limit, 2000))
    sql = (
        "SELECT job_id, owner, repo, pr_number, pr_url, status, status_text, "
        "       review_summary, error, author, created_at, completed_at "
        "FROM code_review_runs WHERE owner = %s AND repo = %s"
    )
    params: list[Any] = [owner, repo]
    if pr_number is not None:
        sql += " AND pr_number = %s"
        params.append(pr_number)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
    except Exception:  # noqa: BLE001 - degrade to no history rather than error
        logger.warning("code_review_runs: list_reviews failed", exc_info=True)
        return []
