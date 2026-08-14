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

Mirrors the ``agent_platform.console`` store idioms (``get_conn`` / ``Json`` /
``dict_row`` / ``@timed_query``).
"""

from __future__ import annotations

import functools
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional, TypeVar

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Json

from shared.postgres import get_conn, is_postgres_enabled
from shared.postgres.metrics import timed_query

logger = logging.getLogger(__name__)
_STORE = "coding_team"

# Columns `update_review` is permitted to write. The SET clause is composed only
# from these constants — never from caller-supplied identifiers — so the query
# can never carry user-controlled SQL.
_UPDATABLE_COLUMNS = frozenset({"status", "status_text", "review_summary", "error", "completed_at"})

_F = TypeVar("_F", bound=Callable[..., Any])


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _readonly_query(default: Any) -> Callable[[_F], _F]:
    """Decorate a read function with the "degrade to `default`" shape.

    Preconditions:
        - The wrapped function performs a Postgres read and returns the value to
          hand back to the caller on success.
    Postconditions:
        - The wrapped function only runs when Postgres is enabled. Returns
          `default` when Postgres is disabled, or when the wrapped function
          raises (the exception is logged as a warning tagged with the wrapped
          function's name and swallowed). Never raises.
    """

    def _default() -> Any:
        # `default` may be a mutable literal (e.g. `[]`) shared across every
        # degraded call; hand back a fresh copy so no caller can mutate the
        # decorator's captured value.
        return list(default) if isinstance(default, list) else default

    def decorator(func: _F) -> _F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not is_postgres_enabled():
                return _default()
            try:
                return func(*args, **kwargs)
            except Exception:  # noqa: BLE001 - degrade rather than error
                logger.warning("code_review_runs: %s failed", func.__name__, exc_info=True)
                return _default()

        return wrapper  # type: ignore[return-value]

    return decorator


def _best_effort_write(op_name: str, write_fn: Callable[[], None]) -> None:
    """Run `write_fn` when Postgres is enabled, logging and swallowing any failure.

    Preconditions:
        - Postgres is enabled (callers check `is_postgres_enabled()` first, since
          they may need to skip other work — e.g. building a query — on top of
          the write itself). `write_fn` takes no arguments and performs the
          Postgres write.
    Postconditions:
        - `write_fn` is invoked. Any exception it raises is logged as a warning
          tagged with `op_name` and swallowed. Never raises.
    """
    try:
        write_fn()
    except Exception:  # noqa: BLE001 - persistence must never break the review
        logger.warning("code_review_runs: %s failed", op_name, exc_info=True)


@timed_query(store=_STORE, op="record_review_start")
def record_review_start(
    job_id: str,
    owner: str,
    repo: str,
    pr_number: int,
    pr_url: Optional[str],
    author: str,
) -> datetime:
    """Insert a ``pending`` row for a newly-started review (best-effort).

    Preconditions:
        - ``job_id`` is the review job's id (unique per review).
    Postconditions:
        - On success a ``code_review_runs`` row exists for ``job_id``. A
          duplicate ``job_id`` is ignored. Never raises; no-op without Postgres.
        - Returns the server-clock ``created_at`` stamped on the row — the same
          value whether or not the write succeeds (and even when Postgres is
          disabled), so a caller can surface a consistent server-side start time
          (e.g. to compute a live review duration on one clock).
    """
    created_at = _now()
    if not is_postgres_enabled():
        return created_at

    def _write() -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO code_review_runs
                      (job_id, owner, repo, pr_number, pr_url, status, author, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (job_id) DO NOTHING""",
                (job_id, owner, repo, pr_number, pr_url, "pending", author, created_at),
            )

    _best_effort_write("record_review_start", _write)
    return created_at


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
    # hardcoded constants below. An allowlist guard (further down) refuses any
    # stray column, and the clause is composed with sql.Identifier — so it can
    # never carry a user-controlled identifier.
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
    unexpected = set(columns) - _UPDATABLE_COLUMNS
    if unexpected:
        # Defense-in-depth: refuse to compose a query with a non-allowlisted
        # column rather than risk an injected identifier. Cannot happen with the
        # hardcoded columns above; using an ``if`` (not ``assert``) keeps the
        # guard active even under ``python -O``.
        logger.error(
            "code_review_runs: refusing update with non-allowlisted column(s): %s", unexpected
        )
        return
    # Compose the SET clause with psycopg.sql.Identifier so column names are
    # quoted as identifiers (never string-interpolated), making the injection
    # safety structurally evident; values stay parameterized via %s placeholders.
    set_clause = sql.SQL(", ").join(
        sql.SQL("{} = %s").format(sql.Identifier(col)) for col in columns
    )
    query = sql.SQL("UPDATE code_review_runs SET {set_clause} WHERE job_id = %s").format(
        set_clause=set_clause
    )
    params: list[Any] = [val for _, val in assignments]
    params.append(job_id)

    def _write() -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(query, params)

    _best_effort_write("update_review", _write)


@timed_query(store=_STORE, op="get_review")
@_readonly_query(default=None)
def get_review(job_id: str) -> Optional[dict[str, Any]]:
    """Return one review row by ``job_id``, or None.

    The durable counterpart to the in-memory job record: used when creating
    GitHub issues from a review's stored ``pending_issue_proposals`` after the
    original job may have aged out of the job store (e.g. a server restart).

    Preconditions:
        - ``job_id`` is a review job's id.
    Postconditions:
        - Returns the ``code_review_runs`` row (including ``review_summary`` with
          any pending issue proposals, plus ``owner``/``repo``/``pr_number``/
          ``pr_url``/``status``) for ``job_id``, or None when it does not exist,
          Postgres is unavailable, or the query fails (never raises).
    """
    query = (
        "SELECT job_id, owner, repo, pr_number, pr_url, status, status_text, "
        "       review_summary, error, author, created_at, completed_at "
        "FROM code_review_runs WHERE job_id = %s"
    )
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, (job_id,))
        return cur.fetchone()


@timed_query(store=_STORE, op="list_reviews")
@_readonly_query(default=[])
def list_reviews(
    owner: str,
    repo: str,
    pr_number: Optional[int] = None,
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return review rows for a repo (optionally one PR), newest first.

    Preconditions:
        - ``owner``/``repo`` are the caller-provided repository identifiers to look up
          (matched case-insensitively); repository access is per-request, not a
          statically configured default.
    Postconditions:
        - Returns up to ``limit`` rows ordered by ``created_at`` DESC. Returns
          ``[]`` when Postgres is unavailable or the query fails (never raises).
    """
    limit = max(1, min(limit, 2000))
    # ``query`` (not ``sql``) so we don't shadow the imported ``psycopg.sql`` module.
    # Compare owner/repo case-insensitively: GitHub treats them as case-insensitive, and
    # rows may have been persisted with the operator-typed casing while lookups now use the
    # canonical casing from GET /user/repos — an exact match would silently hide history.
    query = (
        "SELECT job_id, owner, repo, pr_number, pr_url, status, status_text, "
        "       review_summary, error, author, created_at, completed_at "
        "FROM code_review_runs WHERE lower(owner) = lower(%s) AND lower(repo) = lower(%s)"
    )
    params: list[Any] = [owner, repo]
    if pr_number is not None:
        query += " AND pr_number = %s"
        params.append(pr_number)
    query += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return list(cur.fetchall())
