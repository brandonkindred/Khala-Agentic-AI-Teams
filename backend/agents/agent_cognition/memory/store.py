"""Postgres data-access layer for episodic memory events and rollups.

Follows the ``agent_console`` store idiom:
  * stateless module-level functions (the pool lives in ``shared_postgres``)
  * one public function per operation, decorated with ``@timed_query``
  * synchronous psycopg v3; ``with get_conn()`` commits on clean exit and
    rolls back on exception, so there are no explicit ``commit()`` calls
  * ``%s`` positional params; ``Json(...)`` for JSONB columns; rows are
    rebuilt into pydantic models via ``model_validate``

The cognition plan specifies free functions taking ``agent_id`` first (not a
class), because the rollup engine, retrieval builder, and invoke facade call
these directly.

Design by Contract:

* **Preconditions** — every mutating call asserts that the supplied
  ``agent_id`` matches the row's own ``agent_id`` (no cross-agent writes),
  and that count/retention arguments are non-negative.
* **Postconditions** — ``append_event`` and ``upsert_summary`` are
  idempotent on their schema unique keys
  (``(agent_id, source_run_id, source_seq)`` and
  ``(agent_id, scale, period_start)`` respectively); every reader returns
  only rows owned by ``agent_id``.
* **Invariant** — *every* statement in this module is filtered by
  ``agent_id``; no query reads or writes another agent's rows.

When ``POSTGRES_HOST`` is unset, ``shared_postgres.get_conn`` is unavailable,
so :func:`_conn` raises :class:`AgentCognitionStorageUnavailable` for the API
layer to translate into a clean 503.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Json

from agent_cognition.models import MemoryEvent, PeriodSummary, Scale
from shared_postgres import get_conn, is_postgres_enabled
from shared_postgres.metrics import timed_query

logger = logging.getLogger(__name__)
_STORE = "agent_cognition"

# Full column lists so ``SELECT`` results round-trip straight into the models.
_EVENT_COLS = "id, agent_id, kind, content, data, salience, occurred_at, source_run_id, source_seq"
_SUMMARY_COLS = (
    "id, agent_id, scale, period_start, period_end, summary, highlights, "
    "source_count, covers_through, version, stale, created_at"
)


class AgentCognitionStorageUnavailable(RuntimeError):
    """Postgres isn't configured, unreachable, or the pool is shut down."""


# ---------------------------------------------------------------------------
# Episodic events
# ---------------------------------------------------------------------------
@timed_query(store=_STORE, op="append_event")
def append_event(agent_id: str, event: MemoryEvent) -> None:
    """Append one episodic event, idempotent on the writeback key.

    Preconditions:
        * ``event.agent_id == agent_id`` — the caller owns the row.
        * ``event.id`` and ``event.source_run_id`` are caller-supplied (the
          store never mints them).
    Postconditions:
        * The event is inserted, or — when ``(agent_id, source_run_id,
          source_seq)`` already exists — the call is a no-op (a duplicated or
          retried writeback never skews rollups).
    """
    assert event.agent_id == agent_id, "append_event: event.agent_id must match agent_id"
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO agent_cognition_events ({_EVENT_COLS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (agent_id, source_run_id, source_seq) DO NOTHING""",
            (
                event.id,
                event.agent_id,
                event.kind.value,
                event.content,
                Json(event.data),
                event.salience,
                event.occurred_at,
                event.source_run_id,
                event.source_seq,
            ),
        )


@timed_query(store=_STORE, op="fetch_events_for_period")
def fetch_events_for_period(
    agent_id: str, period_start: datetime, period_end: datetime
) -> list[MemoryEvent]:
    """Return this agent's events in the half-open window ``[start, end)``.

    Postconditions:
        * Ordered by ``occurred_at`` ascending; only rows owned by
          ``agent_id`` and with ``period_start <= occurred_at < period_end``.
    """
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""SELECT {_EVENT_COLS}
                FROM agent_cognition_events
                WHERE agent_id = %s AND occurred_at >= %s AND occurred_at < %s
                ORDER BY occurred_at ASC""",
            (agent_id, period_start, period_end),
        )
        return [MemoryEvent.model_validate(row) for row in cur.fetchall()]


@timed_query(store=_STORE, op="fetch_recent_events")
def fetch_recent_events(agent_id: str, top_n: int, by_salience: bool = True) -> list[MemoryEvent]:
    """Return the ``top_n`` most relevant recent events for this agent.

    Preconditions:
        * ``top_n >= 0``.
    Postconditions:
        * Ordered by ``(salience DESC, occurred_at DESC)`` when
          ``by_salience`` else ``occurred_at DESC``; at most ``top_n`` rows,
          all owned by ``agent_id``.
    """
    assert top_n >= 0, "fetch_recent_events: top_n must be non-negative"
    order_by = "salience DESC, occurred_at DESC" if by_salience else "occurred_at DESC"
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""SELECT {_EVENT_COLS}
                FROM agent_cognition_events
                WHERE agent_id = %s
                ORDER BY {order_by}
                LIMIT %s""",
            (agent_id, top_n),
        )
        return [MemoryEvent.model_validate(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Rollup summaries
# ---------------------------------------------------------------------------
@timed_query(store=_STORE, op="upsert_summary")
def upsert_summary(agent_id: str, summary: PeriodSummary) -> None:
    """Insert or replace one rollup, idempotent on the period unique key.

    Preconditions:
        * ``summary.agent_id == agent_id``.
    Postconditions:
        * Exactly one row exists for ``(agent_id, scale, period_start)``; a
          second call with the same key updates the mutable columns
          (including ``version`` / ``stale``) in place rather than inserting
          a duplicate. ``id`` and ``created_at`` of the original row are
          preserved on update.
    """
    assert summary.agent_id == agent_id, "upsert_summary: summary.agent_id must match agent_id"
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO agent_cognition_summaries ({_SUMMARY_COLS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (agent_id, scale, period_start) DO UPDATE SET
                    period_end = EXCLUDED.period_end,
                    summary = EXCLUDED.summary,
                    highlights = EXCLUDED.highlights,
                    source_count = EXCLUDED.source_count,
                    covers_through = EXCLUDED.covers_through,
                    version = EXCLUDED.version,
                    stale = EXCLUDED.stale""",
            (
                summary.id,
                summary.agent_id,
                summary.scale.value,
                summary.period_start,
                summary.period_end,
                summary.summary,
                Json(summary.highlights),
                summary.source_count,
                summary.covers_through,
                summary.version,
                summary.stale,
                summary.created_at,
            ),
        )


@timed_query(store=_STORE, op="fetch_summaries")
def fetch_summaries(
    agent_id: str, scale: Scale, limit: int | None = None, offset: int = 0
) -> list[PeriodSummary]:
    """Return this agent's summaries at ``scale``, newest period first.

    Preconditions:
        * ``offset >= 0`` and, when supplied, ``limit >= 0``.
    Postconditions:
        * Ordered by ``period_start`` descending; only rows owned by
          ``agent_id`` at the requested ``scale``.
    """
    assert offset >= 0, "fetch_summaries: offset must be non-negative"
    assert limit is None or limit >= 0, "fetch_summaries: limit must be non-negative"
    sql = f"""SELECT {_SUMMARY_COLS}
              FROM agent_cognition_summaries
              WHERE agent_id = %s AND scale = %s
              ORDER BY period_start DESC"""
    params: list[object] = [agent_id, scale.value]
    if limit is not None:
        sql += " LIMIT %s OFFSET %s"
        params.extend([limit, offset])
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [PeriodSummary.model_validate(row) for row in cur.fetchall()]


@timed_query(store=_STORE, op="get_last_summary")
def get_last_summary(agent_id: str, scale: Scale) -> PeriodSummary | None:
    """Return the most recent (latest ``period_start``) summary, or ``None``.

    Postconditions:
        * The returned summary, if any, is owned by ``agent_id`` at ``scale``
          and has the maximal ``period_start`` of that set.
    """
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""SELECT {_SUMMARY_COLS}
                FROM agent_cognition_summaries
                WHERE agent_id = %s AND scale = %s
                ORDER BY period_start DESC
                LIMIT 1""",
            (agent_id, scale.value),
        )
        row = cur.fetchone()
        return PeriodSummary.model_validate(row) if row else None


@timed_query(store=_STORE, op="mark_period_stale")
def mark_period_stale(agent_id: str, occurred_at: datetime) -> bool:
    """Flag every summary that contains ``occurred_at`` for recompute.

    A late event landing inside an already-summarized period must trigger a
    re-summarization. This sets ``stale = true`` and bumps ``version`` on
    *every* existing summary whose half-open window ``[period_start,
    period_end)`` contains ``occurred_at`` — i.e. the day, week, and month
    (and year) rows at once, so the staleness cascade up the scales is
    implicit.

    Preconditions:
        * **Call this before persisting the triggering late event.** The
          retained-count below counts the events currently stored in the
          period; it must reflect only the events the day summary folded
          (plus any earlier, not-yet-rolled-up arrivals) and *not* the
          in-flight late event. Counting the late event would let a
          partially-pruned day read back up to ``source_count`` and wrongly
          report the period as retained, so the rollup engine would recompute
          from an incomplete raw set and silently drop the pruned history.
    Postconditions:
        * All containing summaries (any scale) are now ``stale`` with
          ``version`` incremented by one.
    Returns:
        Whether the period's **raw events are still retained** — the regime
        signal the rollup engine uses to choose recompute-from-events
        (regime a) vs. incremental amend (regime b). ``True`` unless a
        containing **day** summary exists whose ``source_count`` exceeds the
        number of events currently stored in its window (meaning some were
        pruned). With no day summary, the period was never summarized, so
        events are trivially retained and ``True`` is returned.
    """
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_cognition_summaries
               SET stale = TRUE, version = version + 1
               WHERE agent_id = %s AND period_start <= %s AND %s < period_end""",
            (agent_id, occurred_at, occurred_at),
        )
        cur.execute(
            """SELECT period_start, period_end, source_count
               FROM agent_cognition_summaries
               WHERE agent_id = %s AND scale = %s
                 AND period_start <= %s AND %s < period_end""",
            (agent_id, Scale.DAY.value, occurred_at, occurred_at),
        )
        day = cur.fetchone()
        if day is None:
            return True
        period_start, period_end, source_count = day
        cur.execute(
            """SELECT count(*) FROM agent_cognition_events
               WHERE agent_id = %s AND occurred_at >= %s AND occurred_at < %s""",
            (agent_id, period_start, period_end),
        )
        current_count = cur.fetchone()[0]
        return current_count >= source_count


@timed_query(store=_STORE, op="prune_events")
def prune_events(agent_id: str, retention_days: int) -> int:
    """Delete raw events older than the cutoff, losslessly.

    An event is only deleted when the **day** summary containing it exists
    and is non-stale — so nothing that hasn't been folded into a current
    summary is ever lost, and the rollup engine always has a base summary to
    amend if a still-later event arrives for that day.

    Preconditions:
        * ``retention_days >= 0``.
    Postconditions:
        * Only this agent's events with ``occurred_at < now - retention_days``
          whose containing day summary exists and is non-stale are removed.
        * Events with no day summary, or under a ``stale`` day summary, or
          newer than the cutoff, are retained.
    Returns:
        The number of events deleted.
    """
    assert retention_days >= 0, "prune_events: retention_days must be non-negative"
    cutoff = _now() - timedelta(days=retention_days)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """DELETE FROM agent_cognition_events AS e
               WHERE e.agent_id = %s
                 AND e.occurred_at < %s
                 AND EXISTS (
                     SELECT 1 FROM agent_cognition_summaries s
                     WHERE s.agent_id = e.agent_id
                       AND s.scale = 'day'
                       AND s.stale = FALSE
                       AND e.occurred_at >= s.period_start
                       AND e.occurred_at < s.period_end
                 )""",
            (agent_id, cutoff),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _conn():
    if not is_postgres_enabled():
        raise AgentCognitionStorageUnavailable(
            "POSTGRES_HOST is not configured; Agent Cognition storage is unavailable."
        )
    try:
        return get_conn()
    except Exception as exc:  # pragma: no cover — infra paths
        raise AgentCognitionStorageUnavailable(str(exc)) from exc


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)
