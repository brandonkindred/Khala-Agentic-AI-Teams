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

* **Preconditions** — every call asserts a non-empty ``agent_id``; mutating
  calls additionally assert that the row's own ``agent_id`` matches, and that
  count/retention arguments are non-negative.
* **Postconditions** — ``append_event``, ``upsert_summary``, and
  ``mark_period_stale`` are idempotent: re-running them with the same inputs
  does not duplicate rows or advance ``version`` a second time.
* **Invariant** — *every* statement in this module is filtered by
  ``agent_id``; no query reads or writes another agent's rows.

When ``POSTGRES_HOST`` is unset, ``shared_postgres.get_conn`` is unavailable,
so :func:`_conn` raises :class:`AgentCognitionStorageUnavailable` for the API
layer to translate into a clean 503.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Json

from agent_cognition.models import MemoryEvent, PeriodSummary, Scale
from shared_postgres import get_conn, is_postgres_enabled
from shared_postgres.metrics import timed_query

logger = logging.getLogger(__name__)
_STORE = "agent_cognition"

# Full column lists so ``SELECT`` results round-trip straight into the models.
# ``events_pruned`` is store-managed retention bookkeeping (see prune_events /
# mark_period_stale) and is intentionally not part of the PeriodSummary model,
# so it is absent here — upsert_summary never writes it and reads never need it.
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
        * ``agent_id`` is non-empty and ``event.agent_id == agent_id`` — the
          caller owns the row.
        * ``event.id`` and ``event.source_run_id`` are caller-supplied (the
          store never mints them).
    Postconditions:
        * The event is inserted, or — when ``(agent_id, source_run_id,
          source_seq)`` already exists — the call is a no-op (a duplicated or
          retried writeback never skews rollups).
    """
    assert agent_id, "append_event: agent_id must be non-empty"
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

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * Ordered by ``(occurred_at, id)`` ascending (stable); only rows owned
          by ``agent_id`` with ``period_start <= occurred_at < period_end``.
    """
    assert agent_id, "fetch_events_for_period: agent_id must be non-empty"
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""SELECT {_EVENT_COLS}
                FROM agent_cognition_events
                WHERE agent_id = %s AND occurred_at >= %s AND occurred_at < %s
                ORDER BY occurred_at ASC, id ASC""",
            (agent_id, period_start, period_end),
        )
        return [MemoryEvent.model_validate(row) for row in cur.fetchall()]


@timed_query(store=_STORE, op="fetch_recent_events")
def fetch_recent_events(agent_id: str, top_n: int, by_salience: bool = True) -> list[MemoryEvent]:
    """Return the ``top_n`` most relevant recent events for this agent.

    Preconditions:
        * ``agent_id`` is non-empty and ``top_n >= 0``.
    Postconditions:
        * Ordered by ``(salience DESC, occurred_at DESC, id)`` when
          ``by_salience`` else ``(occurred_at DESC, id)``; the trailing ``id``
          breaks ties so the order is deterministic. At most ``top_n`` rows,
          all owned by ``agent_id``.
    """
    assert agent_id, "fetch_recent_events: agent_id must be non-empty"
    assert top_n >= 0, "fetch_recent_events: top_n must be non-negative"
    order_by = (
        "salience DESC, occurred_at DESC, id ASC" if by_salience else "occurred_at DESC, id ASC"
    )
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
        * ``agent_id`` is non-empty and ``summary.agent_id == agent_id``.
    Postconditions:
        * Exactly one row exists for ``(agent_id, scale, period_start)``; a
          second call with the same key updates the mutable columns in place
          rather than inserting a duplicate. ``id`` and ``created_at`` of the
          original row are preserved on update.
        * ``version`` is monotonic non-decreasing: an update carrying a lower
          ``version`` than the stored row (e.g. a freshly-built summary after
          ``mark_period_stale`` bumped it) keeps the higher stored value, so a
          recompute never regresses the ``(summary_id, version)`` evidence
          refs that proposals/rules key on.
        * The store-managed ``events_pruned`` flag is not touched here (it is
          owned solely by ``prune_events``), so a recompute can never silently
          clear it.
    """
    assert agent_id, "upsert_summary: agent_id must be non-empty"
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
                    version = GREATEST(
                        agent_cognition_summaries.version, EXCLUDED.version
                    ),
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
        * ``agent_id`` is non-empty, ``offset >= 0``, and, when supplied,
          ``limit >= 0``.
    Postconditions:
        * Ordered by ``period_start`` descending (deterministic — the period
          key is unique per scale); only rows owned by ``agent_id`` at the
          requested ``scale``.
    """
    assert agent_id, "fetch_summaries: agent_id must be non-empty"
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

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * The returned summary, if any, is owned by ``agent_id`` at ``scale``
          and has the maximal ``period_start`` of that set.
    """
    assert agent_id, "get_last_summary: agent_id must be non-empty"
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
    every existing summary whose half-open window ``[period_start,
    period_end)`` contains ``occurred_at`` — the day, week, month (and year)
    rows at once, so the staleness cascade up the scales is implicit.

    Idempotency: only the non-stale → stale transition bumps ``version`` (the
    ``stale = FALSE`` guard). A retried writeback, or a second distinct late
    event into a period already pending recompute, re-affirms ``stale`` as a
    no-op and never advances ``version`` a second time — so the
    ``(summary_id, version)`` evidence refs that proposals/rules depend on are
    not spuriously invalidated. The regime result is order-independent: it does
    not matter whether the triggering late event has already been appended.

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * Each containing summary that was not already ``stale`` is now
          ``stale`` with ``version`` incremented by one; already-stale
          summaries are untouched.
    Returns:
        Whether the period's **raw events are still retained** — the durable
        regime signal the rollup engine uses to choose recompute-from-events
        (regime a) vs. incremental amend (regime b). Read from the
        store-managed ``events_pruned`` flag that ``prune_events`` latches on a
        day summary when it deletes that day's raw events, so the answer
        survives a restart and never miscounts an in-flight late event.
        ``True`` (retained) when the containing **day** summary is not pruned,
        or when no day summary exists at all — in which case ``prune_events``
        (which only deletes under a non-stale day summary) cannot have removed
        any events.
    """
    assert agent_id, "mark_period_stale: agent_id must be non-empty"
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_cognition_summaries
               SET stale = TRUE, version = version + 1
               WHERE agent_id = %s AND stale = FALSE
                 AND period_start <= %s AND %s < period_end""",
            (agent_id, occurred_at, occurred_at),
        )
        cur.execute(
            """SELECT events_pruned
               FROM agent_cognition_summaries
               WHERE agent_id = %s AND scale = %s
                 AND period_start <= %s AND %s < period_end""",
            (agent_id, Scale.DAY.value, occurred_at, occurred_at),
        )
        day = cur.fetchone()
        if day is None:
            return True
        return not day[0]


@timed_query(store=_STORE, op="prune_events")
def prune_events(agent_id: str, retention_days: int) -> int:
    """Delete raw events older than the cutoff, losslessly.

    An event is only deleted when the **day** summary containing it exists and
    is non-stale — so nothing that hasn't been folded into a current summary is
    ever lost, and the rollup engine always has a base summary to amend if a
    still-later event arrives for that day.

    Before deleting, the affected day summaries are latched
    ``events_pruned = TRUE``. That flag is the durable recompute-vs-amend
    marker read by :func:`mark_period_stale`: once a day's raw events are gone,
    a later late arrival into that day must be amended onto the existing
    summary rather than recomputed from the now-incomplete event set.

    Preconditions:
        * ``agent_id`` is non-empty and ``retention_days >= 0``.
    Postconditions:
        * Only this agent's events with ``occurred_at < now - retention_days``
          whose containing day summary exists and is non-stale are removed, and
          exactly those day summaries are marked ``events_pruned``.
        * Events with no day summary, or under a ``stale`` day summary, or
          newer than the cutoff, are retained.
    Returns:
        The number of events deleted.
    """
    assert agent_id, "prune_events: agent_id must be non-empty"
    assert retention_days >= 0, "prune_events: retention_days must be non-negative"
    cutoff = _now() - timedelta(days=retention_days)
    with _conn() as conn, conn.cursor() as cur:
        # Latch the durable regime marker on every non-stale day summary that
        # is about to lose events. Done before the DELETE — afterwards the
        # raw rows are gone and we could no longer tell which days were hit.
        cur.execute(
            """UPDATE agent_cognition_summaries s
               SET events_pruned = TRUE
               WHERE s.agent_id = %s AND s.scale = %s AND s.stale = FALSE
                 AND EXISTS (
                     SELECT 1 FROM agent_cognition_events e
                     WHERE e.agent_id = s.agent_id
                       AND e.occurred_at < %s
                       AND e.occurred_at >= s.period_start
                       AND e.occurred_at < s.period_end
                 )""",
            (agent_id, Scale.DAY.value, cutoff),
        )
        cur.execute(
            """DELETE FROM agent_cognition_events AS e
               WHERE e.agent_id = %s
                 AND e.occurred_at < %s
                 AND EXISTS (
                     SELECT 1 FROM agent_cognition_summaries s
                     WHERE s.agent_id = e.agent_id
                       AND s.scale = %s
                       AND s.stale = FALSE
                       AND e.occurred_at >= s.period_start
                       AND e.occurred_at < s.period_end
                 )""",
            (agent_id, cutoff, Scale.DAY.value),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
@contextmanager
def _conn():
    """Yield a pooled connection, translating *acquisition* failures only.

    Preconditions:
        * Postgres is configured (``POSTGRES_HOST`` set).
    Postconditions:
        * Errors raised while *acquiring* the connection surface as
          :class:`AgentCognitionStorageUnavailable`; errors raised inside the
          ``with`` body propagate unchanged, so a genuine query bug is never
          masked as an infrastructure outage. Commit-on-success and
          rollback-on-error are delegated to the underlying ``shared_postgres``
          pool context.
    """
    if not is_postgres_enabled():
        raise AgentCognitionStorageUnavailable(
            "POSTGRES_HOST is not configured; Agent Cognition storage is unavailable."
        )
    pool_ctx = get_conn()
    try:
        conn = pool_ctx.__enter__()
    except Exception as exc:  # pragma: no cover — pool/connection failure path
        raise AgentCognitionStorageUnavailable(str(exc)) from exc
    try:
        yield conn
    except BaseException as exc:
        if not pool_ctx.__exit__(type(exc), exc, exc.__traceback__):
            raise
    else:
        pool_ctx.__exit__(None, None, None)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)
