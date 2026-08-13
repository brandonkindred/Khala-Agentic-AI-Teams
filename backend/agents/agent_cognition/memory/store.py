"""Postgres data-access layer for episodic memory events and rollups.

Follows the ``agent_platform.console`` store idiom:
  * stateless module-level functions (the pool lives in ``shared.postgres``)
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

When ``POSTGRES_HOST`` is unset, ``shared.postgres.get_conn`` is unavailable,
so :func:`_conn` raises :class:`AgentCognitionStorageUnavailable` for the API
layer to translate into a clean 503.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from psycopg.rows import dict_row
from psycopg.types.json import Json

from agent_cognition.models import MemoryEvent, PeriodSummary, Scale
from shared.postgres import get_conn, is_postgres_enabled
from shared.postgres.metrics import timed_query

logger = logging.getLogger(__name__)
_STORE = "agent_cognition"

# Full column lists so ``SELECT`` results round-trip straight into the models.
# ``events.recorded_at`` and ``summaries.computed_at`` are store-managed write
# bookkeeping (set by append's DEFAULT NOW() and by upsert_summary) and stay out
# of these lists. ``summaries.events_pruned`` *is* surfaced to readers (it is the
# durable recompute-vs-amend regime, see _SUMMARY_READ_COLS) but is never written
# by upsert_summary — only prune_events latches it.
_EVENT_COLS = "id, agent_id, kind, content, data, salience, occurred_at, source_run_id, source_seq"
_SUMMARY_COLS = (
    "id, agent_id, scale, period_start, period_end, summary, highlights, "
    "source_count, covers_through, version, stale, created_at"
)
# Read projection: the write columns plus the store-managed regime flag, so a
# rollup that rediscovers a stale summary can tell pruned (amend) from retained
# (recompute) without re-calling mark_period_stale.
_SUMMARY_READ_COLS = f"{_SUMMARY_COLS}, events_pruned"


class AgentCognitionStorageUnavailable(RuntimeError):
    """Postgres isn't configured, unreachable, or the pool is shut down."""


# ---------------------------------------------------------------------------
# Episodic events
# ---------------------------------------------------------------------------
def _event_row(event: MemoryEvent) -> tuple:
    """Build the positional INSERT tuple for ``_EVENT_COLS`` from an event.

    Single source of truth for the column→value mapping shared by
    :func:`append_event` and :func:`append_events`, so the two write paths can
    never drift out of sync with ``_EVENT_COLS``.
    """
    return (
        event.id,
        event.agent_id,
        event.kind.value,
        event.content,
        Json(event.data),
        event.salience,
        event.occurred_at,
        event.source_run_id,
        event.source_seq,
    )


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
            _event_row(event),
        )


@timed_query(store=_STORE, op="append_events")
def append_events(agent_id: str, events: list[MemoryEvent]) -> int:
    """Append many episodic events in a single transaction, idempotent per event.

    The batched sibling of :func:`append_event`: one connection, one
    transaction, one ``executemany`` — so a multi-event writeback persists
    **atomically** (all rows commit together or none do) instead of leaving a
    torn partial write when one row fails midway.

    Preconditions:
        * ``agent_id`` is non-empty and every ``event.agent_id == agent_id``.
        * Each ``event.id`` is unique across the batch and the table (the caller
          owns minting ids; the ``id`` PK is not part of the ``ON CONFLICT``
          target, so a duplicate id would abort the whole batch).
    Postconditions:
        * Each event is inserted, or — when its ``(agent_id, source_run_id,
          source_seq)`` already exists — skipped, all within one transaction.
        * Returns the number of rows **actually inserted** (``cur.rowcount``), so
          a fully-deduped re-persist returns ``0`` and the caller can tell a real
          append from an idempotent no-op. A no-op on an empty list.
    """
    assert agent_id, "append_events: agent_id must be non-empty"
    assert all(e.agent_id == agent_id for e in events), (
        "append_events: every event.agent_id must match agent_id"
    )
    if not events:
        return 0
    with _conn() as conn, conn.cursor() as cur:
        cur.executemany(
            f"""INSERT INTO agent_cognition_events ({_EVENT_COLS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (agent_id, source_run_id, source_seq) DO NOTHING""",
            [_event_row(e) for e in events],
        )
        # rowcount after executemany is the total rows actually inserted across
        # all param sets (conflicting rows count 0) — the honest "newly stored".
        return cur.rowcount


@timed_query(store=_STORE, op="fetch_events_for_period")
def fetch_events_for_period(
    agent_id: str,
    period_start: datetime,
    period_end: datetime,
    *,
    snapshot: datetime | None = None,
) -> list[MemoryEvent]:
    """Return this agent's events in the half-open window ``[start, end)``.

    Args:
        snapshot: Optional arrival-time bound. When set, only events with
            ``recorded_at <= snapshot`` are returned. A rollup folding this
            period **must** pass the same value it gives ``upsert_summary``'s
            ``computed_at``, so it never folds an event the prune guard would
            then consider not-yet-folded (``recorded_at > computed_at``) — i.e.
            the read and the recorded/computed prune comparison stay consistent.
            Omit it for plain time-window reads.

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * Ordered by ``(occurred_at, id)`` ascending (stable); only rows owned
          by ``agent_id`` with ``period_start <= occurred_at < period_end`` and,
          when ``snapshot`` is given, ``recorded_at <= snapshot``.
    """
    assert agent_id, "fetch_events_for_period: agent_id must be non-empty"
    sql = f"""SELECT {_EVENT_COLS}
              FROM agent_cognition_events
              WHERE agent_id = %s AND occurred_at >= %s AND occurred_at < %s"""
    params: list[object] = [agent_id, period_start, period_end]
    if snapshot is not None:
        sql += " AND recorded_at <= %s"
        params.append(snapshot)
    sql += " ORDER BY occurred_at ASC, id ASC"
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [MemoryEvent.model_validate(row) for row in cur.fetchall()]


@timed_query(store=_STORE, op="fetch_recent_events")
def fetch_recent_events(
    agent_id: str,
    top_n: int,
    by_salience: bool = True,
    *,
    since: datetime | None = None,
) -> list[MemoryEvent]:
    """Return the ``top_n`` most relevant recent events for this agent.

    Args:
        since: Optional inclusive lower bound on ``occurred_at``. When set, only
            events with ``occurred_at >= since`` are considered, so a caller can
            get the top-N salient events *within the in-progress period* — the
            ordering and limit are applied in SQL, never materializing the whole
            tail in Python.

    Preconditions:
        * ``agent_id`` is non-empty and ``top_n >= 0``.
    Postconditions:
        * Ordered by ``(salience DESC, occurred_at DESC, id)`` when
          ``by_salience`` else ``(occurred_at DESC, id)``; the trailing ``id``
          breaks ties so the order is deterministic. At most ``top_n`` rows,
          all owned by ``agent_id`` and, when ``since`` is given, with
          ``occurred_at >= since``.
    """
    assert agent_id, "fetch_recent_events: agent_id must be non-empty"
    assert top_n >= 0, "fetch_recent_events: top_n must be non-negative"
    order_by = (
        "salience DESC, occurred_at DESC, id ASC" if by_salience else "occurred_at DESC, id ASC"
    )
    sql = f"SELECT {_EVENT_COLS} FROM agent_cognition_events WHERE agent_id = %s"
    params: list[object] = [agent_id]
    if since is not None:
        sql += " AND occurred_at >= %s"
        params.append(since)
    sql += f" ORDER BY {order_by} LIMIT %s"
    params.append(top_n)
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [MemoryEvent.model_validate(row) for row in cur.fetchall()]


@timed_query(store=_STORE, op="fetch_unfolded_events")
def fetch_unfolded_events(
    agent_id: str,
    scale: Scale,
    period_start: datetime,
    period_end: datetime,
    *,
    snapshot: datetime,
) -> list[MemoryEvent]:
    """Return events in ``[start, end)`` not yet folded into the period summary.

    For the incremental-amend path on a pruned period: an event is *unfolded*
    when it arrived (``recorded_at``) **after** the matching summary's last
    ``computed_at`` fold point and at or before ``snapshot``. This is the exact
    complement of :func:`prune_events`' folded set (``recorded_at <=
    computed_at``), so an event already amended in on a previous pass — but not
    yet pruned — is never re-folded, which would otherwise double-count
    ``source_count`` and duplicate digest content.

    Preconditions:
        * ``agent_id`` is non-empty; the window is half-open; ``snapshot`` is
          the rollup's read-time.
    Postconditions:
        * Ordered by ``(occurred_at, id)`` ascending; only this agent's events
          with ``period_start <= occurred_at < period_end`` and ``fold_point <
          recorded_at <= snapshot``, where ``fold_point`` is the matching
          summary's ``computed_at`` (treated as ``-infinity`` when the summary
          is absent or never computed, so every event qualifies).
    """
    assert agent_id, "fetch_unfolded_events: agent_id must be non-empty"
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""SELECT {_EVENT_COLS}
                FROM agent_cognition_events e
                WHERE e.agent_id = %s
                  AND e.occurred_at >= %s AND e.occurred_at < %s
                  AND e.recorded_at <= %s
                  AND e.recorded_at > COALESCE(
                      (SELECT s.computed_at FROM agent_cognition_summaries s
                       WHERE s.agent_id = e.agent_id AND s.scale = %s
                         AND s.period_start = %s),
                      '-infinity'::timestamptz)
                ORDER BY e.occurred_at ASC, e.id ASC""",
            (agent_id, period_start, period_end, snapshot, scale.value, period_start),
        )
        return [MemoryEvent.model_validate(row) for row in cur.fetchall()]


@timed_query(store=_STORE, op="fetch_recent_unfolded_events")
def fetch_recent_unfolded_events(
    agent_id: str, scale: Scale, top_n: int, *, snapshot: datetime
) -> list[MemoryEvent]:
    """Top-N salient unfolded events across *all* stale summaries at ``scale``.

    The bounded, single-query counterpart to calling :func:`fetch_unfolded_events`
    once per stale period: it joins events to this agent's ``stale`` ``scale``
    summaries and returns the late-arriving rows not yet folded
    (``fold_point < recorded_at <= snapshot``), ranked by salience and capped — so
    a backlog of stale periods costs one ordered, limited query instead of an N+1
    scan. ``fold_point`` is the matching summary's ``computed_at`` (``-infinity``
    when never computed, so every event in the period qualifies). Periods at a
    given scale are non-overlapping, so each event matches at most one summary.

    Preconditions:
        * ``agent_id`` is non-empty and ``top_n >= 0``; ``snapshot`` is the
          read-time bound.
    Postconditions:
        * At most ``top_n`` events owned by ``agent_id``, each inside a ``stale``
          ``scale`` summary's half-open window with ``fold_point < recorded_at <=
          snapshot``, ordered ``(salience DESC, occurred_at DESC, id ASC)``.
    """
    assert agent_id, "fetch_recent_unfolded_events: agent_id must be non-empty"
    assert top_n >= 0, "fetch_recent_unfolded_events: top_n must be non-negative"
    event_cols = ", ".join(f"e.{col}" for col in _EVENT_COLS.split(", "))
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""SELECT {event_cols}
                FROM agent_cognition_events e
                JOIN agent_cognition_summaries s
                  ON s.agent_id = e.agent_id AND s.scale = %s AND s.stale = TRUE
                 AND e.occurred_at >= s.period_start AND e.occurred_at < s.period_end
                WHERE e.agent_id = %s
                  AND e.recorded_at <= %s
                  AND e.recorded_at > COALESCE(s.computed_at, '-infinity'::timestamptz)
                ORDER BY e.salience DESC, e.occurred_at DESC, e.id ASC
                LIMIT %s""",
            (scale.value, agent_id, snapshot, top_n),
        )
        return [MemoryEvent.model_validate(row) for row in cur.fetchall()]


@timed_query(store=_STORE, op="has_events_recorded_after")
def has_events_recorded_after(
    agent_id: str, period_start: datetime, period_end: datetime, *, after: datetime
) -> bool:
    """True iff an event in ``[start, end)`` was *recorded* after ``after``.

    The rollup's first-summary re-probe: when a period gets its very first
    summary, an event appended after the rollup's read snapshot but before the
    summary row existed cannot be flagged stale by the writeback's
    :func:`mark_period_stale` (there is no row yet). After inserting the first
    summary the engine calls this with ``after = computed_at`` (the read
    snapshot); a ``True`` result means such a concurrent append happened, so the
    engine self-flags the just-created period stale and a later pass folds it.

    Preconditions:
        * ``agent_id`` is non-empty; the window is half-open.
    Postconditions:
        * ``True`` iff at least one event owned by ``agent_id`` has
          ``period_start <= occurred_at < period_end`` **and** ``recorded_at >
          after``; no rows are modified.
    """
    assert agent_id, "has_events_recorded_after: agent_id must be non-empty"
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT EXISTS (
                   SELECT 1 FROM agent_cognition_events
                   WHERE agent_id = %s
                     AND occurred_at >= %s AND occurred_at < %s
                     AND recorded_at > %s
               )""",
            (agent_id, period_start, period_end, after),
        )
        return bool(cur.fetchone()[0])


class RecordedEvent(NamedTuple):
    """An event paired with its store-managed ``recorded_at`` arrival time.

    ``MemoryEvent`` deliberately omits ``recorded_at`` (write bookkeeping), but the
    knowledge-graph sync worker keysets on ``(recorded_at, id)``, so this carries
    the arrival time alongside the event for the worker to advance its watermark.
    """

    event: MemoryEvent
    recorded_at: datetime


class RecordedSummary(NamedTuple):
    """A summary paired with its store-managed ``updated_at`` content-write time.

    ``PeriodSummary`` carries ``created_at`` (preserved across recompute) but not
    ``updated_at`` (write bookkeeping), so this rides the content-write time
    alongside the summary for the knowledge-graph sync worker, which keysets on
    ``(updated_at, id)`` to advance its watermark — symmetric with
    :class:`RecordedEvent`.
    """

    summary: PeriodSummary
    updated_at: datetime


@timed_query(store=_STORE, op="fetch_events_recorded_after")
def fetch_events_recorded_after(
    agent_id: str,
    *,
    after_recorded_at: datetime | None,
    after_id: str | None,
    limit: int,
) -> list[RecordedEvent]:
    """Return this agent's events after a ``(recorded_at, id)`` keyset cursor.

    The forward, append-ordered scan the knowledge-graph sync worker uses to drain
    new episodic events into the graph. Ordering on ``recorded_at`` (arrival time,
    not ``occurred_at``) means a late event — appended with a fresh ``recorded_at``
    after an earlier sync pass — sorts *after* the watermark and is picked up on a
    subsequent pass rather than skipped. The ``(recorded_at, id)`` row comparison
    plus the matching index makes this a true keyset scan (no OFFSET walk).

    Args:
        after_recorded_at / after_id: the exclusive cursor — only events with
            ``(recorded_at, id) > (after_recorded_at, after_id)`` are returned.
            Pass both ``None`` (the cold-start watermark) for no lower bound; they
            are advanced together by the worker, so a half-set cursor is a caller
            bug.
        limit: maximum rows to return (the worker's batch size).

    Preconditions:
        * ``agent_id`` is non-empty and ``limit >= 1``.
        * ``after_recorded_at`` and ``after_id`` are both set or both ``None``.
    Postconditions:
        * At most ``limit`` rows owned by ``agent_id``, ordered ``(recorded_at
          ASC, id ASC)``, each strictly after the cursor; ``recorded_at`` rides
          back on each :class:`RecordedEvent`. No rows are modified.
    """
    assert agent_id, "fetch_events_recorded_after: agent_id must be non-empty"
    assert limit >= 1, "fetch_events_recorded_after: limit must be >= 1"
    assert (after_recorded_at is None) == (after_id is None), (
        "fetch_events_recorded_after: after_recorded_at and after_id must both be set or both None"
    )
    sql = f"SELECT {_EVENT_COLS}, recorded_at FROM agent_cognition_events WHERE agent_id = %s"
    params: list[object] = [agent_id]
    if after_recorded_at is not None:
        # Row-comparison keyset: strictly after the (recorded_at, id) cursor.
        sql += " AND (recorded_at, id) > (%s, %s)"
        params.extend([after_recorded_at, after_id])
    sql += " ORDER BY recorded_at ASC, id ASC LIMIT %s"
    params.append(limit)
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [
            RecordedEvent(MemoryEvent.model_validate(row), row["recorded_at"])
            for row in cur.fetchall()
        ]


@timed_query(store=_STORE, op="fetch_summaries_updated_after")
def fetch_summaries_updated_after(
    agent_id: str,
    *,
    after_updated_at: datetime | None,
    after_id: str | None,
    limit: int,
) -> list[RecordedSummary]:
    """Return this agent's rollup summaries after an ``(updated_at, id)`` cursor.

    The forward, content-write-ordered scan the knowledge-graph sync worker uses to
    drain rollup summaries into the graph, symmetric with
    :func:`fetch_events_recorded_after`. Ordering on ``updated_at`` (the
    content-write time advanced by every accepted ``upsert_summary``, not
    ``created_at``, which is preserved across recompute) means a recomputed summary
    — whose ``version`` advanced — re-sorts *after* the watermark and is re-fetched
    on a subsequent pass, where the worker ingests it as a fresh per-version
    episode. A summary that is never re-written keeps its ``updated_at`` and is
    returned once; note any accepted ``upsert_summary`` advances ``updated_at``,
    so a content-unchanged re-write (e.g. the rollup engine clearing a spurious
    stale flag) re-emits the row under the *same* ``summary:<id>:<version>`` episode
    name — an idempotent graph update, not a duplicate. No ``stale`` filter — a
    momentarily stale period is still ingested; recency is resolved at retrieval
    time and by the fresher event episodes.

    Args:
        after_updated_at / after_id: the exclusive ``(updated_at, id)`` cursor;
            pass both ``None`` for no lower bound (cold start). Advanced together.
        limit: maximum rows to return (the worker's batch size).

    Preconditions:
        * ``agent_id`` is non-empty and ``limit >= 1``.
        * ``after_updated_at`` and ``after_id`` are both set or both ``None``.
    Postconditions:
        * At most ``limit`` summaries owned by ``agent_id``, ordered
          ``(updated_at ASC, id ASC)``, each strictly after the cursor;
          ``updated_at`` rides back on each :class:`RecordedSummary`. No rows are
          modified.
    """
    assert agent_id, "fetch_summaries_updated_after: agent_id must be non-empty"
    assert limit >= 1, "fetch_summaries_updated_after: limit must be >= 1"
    assert (after_updated_at is None) == (after_id is None), (
        "fetch_summaries_updated_after: after_updated_at and after_id must both be set or both None"
    )
    sql = f"SELECT {_SUMMARY_READ_COLS}, updated_at FROM agent_cognition_summaries WHERE agent_id = %s"
    params: list[object] = [agent_id]
    if after_updated_at is not None:
        # Row-comparison keyset: strictly after the (updated_at, id) cursor.
        sql += " AND (updated_at, id) > (%s, %s)"
        params.extend([after_updated_at, after_id])
    sql += " ORDER BY updated_at ASC, id ASC LIMIT %s"
    params.append(limit)
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    summaries: list[RecordedSummary] = []
    for row in rows:
        # ``updated_at`` is the keyset cursor, not a PeriodSummary field — pop it
        # before validation so the read never depends on the model tolerating an
        # extra key (defensive if PeriodSummary ever becomes strict / extra='forbid').
        updated_at = row.pop("updated_at")
        summaries.append(RecordedSummary(PeriodSummary.model_validate(row), updated_at))
    return summaries


@timed_query(store=_STORE, op="upsert_summary")
def upsert_summary(agent_id: str, summary: PeriodSummary, *, computed_at: datetime) -> None:
    """Insert or replace one rollup, idempotent on the period unique key.

    Args:
        computed_at: **Required.** The snapshot time up to which this rollup
            folded its inputs — i.e. the instant the caller read the event set
            it summarized, *not* the time of this write. ``prune_events`` only
            deletes events with ``recorded_at <= computed_at``, so this must be
            the read time: an event appended after the snapshot (even if before
            this upsert commits) then has ``recorded_at > computed_at`` and is
            correctly treated as not-yet-folded, so it survives pruning until a
            later recompute amends it in. The rollup should therefore capture
            ``computed_at`` before reading events and bound its read by it.
            Passing the post-generation/write time instead would let a
            concurrently-appended late event be pruned before it is folded.

    Preconditions:
        * ``agent_id`` is non-empty and ``summary.agent_id == agent_id``.
    Postconditions:
        * Exactly one row exists for ``(agent_id, scale, period_start)``; a
          second call with the same key updates the mutable columns in place
          rather than inserting a duplicate. ``id`` and ``created_at`` of the
          original row are preserved on update; ``computed_at`` is refreshed, and
          ``updated_at`` is advanced to ``NOW()`` on every *accepted* update (the
          knowledge-graph keyset cursor) but not on a skipped/superseded one.
        * ``version`` is monotonic non-decreasing: an update carrying a lower
          ``version`` than the stored row (e.g. a freshly-built summary after
          ``mark_period_stale`` bumped it) keeps the higher stored value, so a
          recompute never regresses the ``(summary_id, version)`` evidence
          refs that proposals/rules key on.
        * The store-managed ``events_pruned`` flag is not touched here (it is
          owned solely by ``prune_events``), so a recompute can never silently
          clear it.
        * ``stale`` is **not** cleared if the period was flagged stale after
          this rollup's ``computed_at`` snapshot (``stale_since > computed_at``)
          — a slow rollup that read before a late arrival cannot clobber the
          stale flag raised after its read, so the late event is still folded
          on the next recompute. ``stale_since`` is preserved while stale stays
          set and reset to NULL when it is cleared.
        * ``computed_at`` is monotonic: an update whose ``computed_at`` is older
          than the stored row's is **skipped entirely** (no field changes), so
          two rollups finishing out of order can't let the staler one overwrite
          the fresher summary, regress ``computed_at``, or reset ``stale`` /
          ``version``. A first recompute of a back-filled row (stored
          ``computed_at IS NULL``) is always allowed.
    """
    assert agent_id, "upsert_summary: agent_id must be non-empty"
    assert summary.agent_id == agent_id, "upsert_summary: summary.agent_id must match agent_id"
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO agent_cognition_summaries ({_SUMMARY_COLS}, computed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (agent_id, scale, period_start) DO UPDATE SET
                    period_end = EXCLUDED.period_end,
                    summary = EXCLUDED.summary,
                    highlights = EXCLUDED.highlights,
                    source_count = EXCLUDED.source_count,
                    covers_through = EXCLUDED.covers_through,
                    version = GREATEST(
                        agent_cognition_summaries.version, EXCLUDED.version
                    ),
                    -- Don't clear a stale flag raised *after* this rollup read
                    -- its inputs: if the stored period went stale at a time the
                    -- caller's computed_at snapshot doesn't cover, keep it stale
                    -- (and keep stale_since) so the late event is folded next.
                    stale = (
                        EXCLUDED.stale
                        OR (
                            agent_cognition_summaries.stale_since IS NOT NULL
                            AND agent_cognition_summaries.stale_since > EXCLUDED.computed_at
                        )
                    ),
                    stale_since = CASE
                        WHEN EXCLUDED.stale
                            OR (
                                agent_cognition_summaries.stale_since IS NOT NULL
                                AND agent_cognition_summaries.stale_since > EXCLUDED.computed_at
                            )
                        THEN agent_cognition_summaries.stale_since
                        ELSE NULL
                    END,
                    computed_at = EXCLUDED.computed_at,
                    -- Content-write time for the knowledge-graph keyset cursor:
                    -- advanced only on an accepted update, so a recomputed (version-
                    -- advanced) summary re-sorts after the graph watermark and is
                    -- re-ingested, while a superseded/no-op upsert (skipped by the
                    -- WHERE below) leaves it untouched and is not re-ingested.
                    updated_at = NOW()
                WHERE agent_cognition_summaries.computed_at IS NULL
                    OR EXCLUDED.computed_at >= agent_cognition_summaries.computed_at""",
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
                computed_at,
            ),
        )


@timed_query(store=_STORE, op="fetch_summaries")
def fetch_summaries(
    agent_id: str,
    scale: Scale,
    limit: int | None = None,
    offset: int = 0,
    *,
    exclude_stale: bool = False,
) -> list[PeriodSummary]:
    """Return this agent's summaries at ``scale``, newest period first.

    Preconditions:
        * ``agent_id`` is non-empty, ``offset >= 0``, and, when supplied,
          ``limit >= 0``.
    Postconditions:
        * Ordered by ``period_start`` descending (deterministic — the period
          key is unique per scale); only rows owned by ``agent_id`` at the
          requested ``scale``.
        * When ``exclude_stale`` is set, ``stale`` rows are filtered in SQL —
          *before* ``limit`` — so ``limit`` counts non-stale rows (a caller after
          the newest *fresh* summaries isn't starved when the most recent periods
          are stale but older ones are fresh).
    """
    assert agent_id, "fetch_summaries: agent_id must be non-empty"
    assert offset >= 0, "fetch_summaries: offset must be non-negative"
    assert limit is None or limit >= 0, "fetch_summaries: limit must be non-negative"
    stale_clause = " AND stale = FALSE" if exclude_stale else ""
    sql = f"""SELECT {_SUMMARY_READ_COLS}
              FROM agent_cognition_summaries
              WHERE agent_id = %s AND scale = %s{stale_clause}
              ORDER BY period_start DESC"""
    params: list[object] = [agent_id, scale.value]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    # OFFSET is independent of LIMIT — apply it whenever the caller skips rows,
    # so offset-only pagination doesn't silently return the first page again.
    if offset:
        sql += " OFFSET %s"
        params.append(offset)
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
            f"""SELECT {_SUMMARY_READ_COLS}
                FROM agent_cognition_summaries
                WHERE agent_id = %s AND scale = %s
                ORDER BY period_start DESC
                LIMIT 1""",
            (agent_id, scale.value),
        )
        row = cur.fetchone()
        return PeriodSummary.model_validate(row) if row else None


@timed_query(store=_STORE, op="get_existing_summary")
def get_existing_summary(
    agent_id: str, scale: Scale, period_start: datetime
) -> PeriodSummary | None:
    """Return the summary for one exact ``(scale, period_start)`` key, or None.

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * The unique row for ``(agent_id, scale, period_start)`` or ``None``;
          the store-managed ``events_pruned`` regime flag is surfaced so the
          rollup engine can choose recompute vs. amend.
    """
    assert agent_id, "get_existing_summary: agent_id must be non-empty"
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""SELECT {_SUMMARY_READ_COLS}
                FROM agent_cognition_summaries
                WHERE agent_id = %s AND scale = %s AND period_start = %s""",
            (agent_id, scale.value, period_start),
        )
        row = cur.fetchone()
        return PeriodSummary.model_validate(row) if row else None


@timed_query(store=_STORE, op="fetch_stale_summaries")
def fetch_stale_summaries(agent_id: str, scale: Scale) -> list[PeriodSummary]:
    """Return this agent's ``stale`` summaries at ``scale``, oldest period first.

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * Only rows owned by ``agent_id`` at ``scale`` with ``stale = TRUE``,
          ordered by ``period_start`` ascending so a bottom-up rollup processes
          children before parents.
    """
    assert agent_id, "fetch_stale_summaries: agent_id must be non-empty"
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""SELECT {_SUMMARY_READ_COLS}
                FROM agent_cognition_summaries
                WHERE agent_id = %s AND scale = %s AND stale = TRUE
                ORDER BY period_start ASC""",
            (agent_id, scale.value),
        )
        return [PeriodSummary.model_validate(row) for row in cur.fetchall()]


@timed_query(store=_STORE, op="fetch_summaries_in_window")
def fetch_summaries_in_window(
    agent_id: str, scale: Scale, window_start: datetime, window_end: datetime
) -> list[PeriodSummary]:
    """Return summaries at ``scale`` whose period_start is in ``[start, end)``.

    Used to gather the calendar-correct child inputs of an aggregate rollup
    (a week/month reads its day summaries; a year reads its month summaries).

    Preconditions:
        * ``agent_id`` is non-empty; the window is half-open.
    Postconditions:
        * Only rows owned by ``agent_id`` at ``scale`` with ``window_start <=
          period_start < window_end``, ordered by ``period_start`` ascending.
    """
    assert agent_id, "fetch_summaries_in_window: agent_id must be non-empty"
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""SELECT {_SUMMARY_READ_COLS}
                FROM agent_cognition_summaries
                WHERE agent_id = %s AND scale = %s
                  AND period_start >= %s AND period_start < %s
                ORDER BY period_start ASC""",
            (agent_id, scale.value, window_start, window_end),
        )
        return [PeriodSummary.model_validate(row) for row in cur.fetchall()]


@timed_query(store=_STORE, op="flag_stale_proposals")
def flag_stale_proposals(agent_id: str, summary_id: str, new_version: int) -> int:
    """Flag pending proposals whose evidence cites an outdated summary version.

    Evidence is a JSONB array of ``{"summary_id": <str>, "version": <int>}``
    refs (the cross-step cognition contract). A recompute that advances a
    summary's ``version`` leaves any proposal citing the older version stale.

    Preconditions:
        * ``agent_id`` is non-empty; ``new_version >= 1``.
    Postconditions:
        * Every ``pending`` proposal owned by ``agent_id`` whose ``evidence``
          references ``summary_id`` at a version below ``new_version`` has
          ``stale_evidence = TRUE`` (idempotent — re-flagging is a no-op).
          Returns the number of rows updated.
    """
    assert agent_id, "flag_stale_proposals: agent_id must be non-empty"
    assert new_version >= 1, "flag_stale_proposals: new_version must be >= 1"
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_cognition_rule_proposals
               SET stale_evidence = TRUE
               WHERE agent_id = %s AND status = 'pending' AND stale_evidence = FALSE
                 AND EXISTS (
                     SELECT 1 FROM jsonb_array_elements(evidence) e
                     WHERE e->>'summary_id' = %s AND (e->>'version')::int < %s
                 )""",
            (agent_id, summary_id, new_version),
        )
        return cur.rowcount


@timed_query(store=_STORE, op="flag_rules_needing_review")
def flag_rules_needing_review(agent_id: str, summary_id: str, new_version: int) -> int:
    """Flag active derived rules whose evidence cites an outdated version.

    Companion to :func:`flag_stale_proposals` for already-active rules: an
    ``active`` rule with ``source = 'derived'`` that cited the recomputed
    summary at an older version resurfaces in the operator review queue.

    Preconditions:
        * ``agent_id`` is non-empty; ``new_version >= 1``.
    Postconditions:
        * Every ``active`` ``derived`` rule owned by ``agent_id`` whose
          ``evidence`` references ``summary_id`` at a version below
          ``new_version`` has ``needs_review = TRUE`` (idempotent). Returns the
          number of rows updated.
    """
    assert agent_id, "flag_rules_needing_review: agent_id must be non-empty"
    assert new_version >= 1, "flag_rules_needing_review: new_version must be >= 1"
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_cognition_rules
               SET needs_review = TRUE
               WHERE agent_id = %s AND status = 'active' AND source = 'derived'
                 AND needs_review = FALSE
                 AND EXISTS (
                     SELECT 1 FROM jsonb_array_elements(evidence) e
                     WHERE e->>'summary_id' = %s AND (e->>'version')::int < %s
                 )""",
            (agent_id, summary_id, new_version),
        )
        return cur.rowcount


@timed_query(store=_STORE, op="mark_period_stale")
def mark_period_stale(agent_id: str, occurred_at: datetime) -> bool:
    """Flag every summary that contains ``occurred_at`` for recompute.

    A late event landing inside an already-summarized period must trigger a
    re-summarization. This sets ``stale = true`` and bumps ``version`` on
    every existing summary whose half-open window ``[period_start,
    period_end)`` contains ``occurred_at`` — the day, week, month (and year)
    rows at once, so the staleness cascade up the scales is implicit.

    Idempotency: only the non-stale → stale transition bumps ``version`` (the
    ``CASE`` guard). A retried writeback, or a second distinct late event into a
    period already pending recompute, re-affirms ``stale`` as a no-op for
    ``version`` and never advances it a second time — so the
    ``(summary_id, version)`` evidence refs that proposals/rules depend on are
    not spuriously invalidated. ``stale_since`` *is* refreshed on every late
    arrival (it tracks the latest unfolded staleness, which ``upsert_summary``
    compares against a rollup's ``computed_at``). The regime result is
    order-independent: it does not matter whether the triggering late event has
    already been appended.

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * Each containing summary is now ``stale`` with ``stale_since`` set to
          now; ``version`` is incremented by one only for summaries that were
          not already stale (already-stale summaries keep their ``version``).
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
               SET stale = TRUE,
                   stale_since = NOW(),
                   version = version + (CASE WHEN stale THEN 0 ELSE 1 END)
               WHERE agent_id = %s AND period_start <= %s AND %s < period_end""",
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


@timed_query(store=_STORE, op="mark_summary_stale")
def mark_summary_stale(agent_id: str, scale: Scale, period_start: datetime) -> bool:
    """Flag exactly one summary ``(agent_id, scale, period_start)`` stale.

    The targeted complement of :func:`mark_period_stale` (which flags *every*
    scale containing a timestamp). Used by the rollup's first-aggregate
    re-probe to re-stale just the parent it consumes — without the cascade
    re-staling the parent's own child rows, which would force a needless child
    recompute. Idempotent on the stale flag exactly like ``mark_period_stale``:
    only the non-stale → stale transition bumps ``version``.

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * If a row for ``(agent_id, scale, period_start)`` exists it is now
          ``stale`` with ``stale_since = NOW()``; ``version`` advances by one
          only on the non-stale → stale transition. No other row is touched.
    Returns:
        ``True`` iff a matching row existed and was updated.
    """
    assert agent_id, "mark_summary_stale: agent_id must be non-empty"
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_cognition_summaries
               SET stale = TRUE,
                   stale_since = NOW(),
                   version = version + (CASE WHEN stale THEN 0 ELSE 1 END)
               WHERE agent_id = %s AND scale = %s AND period_start = %s""",
            (agent_id, scale.value, period_start),
        )
        return cur.rowcount > 0


@timed_query(store=_STORE, op="prune_events")
def prune_events(agent_id: str, retention_days: int) -> int:
    """Delete raw events older than the cutoff, losslessly.

    An event is only deleted when the **day** summary containing it exists, is
    non-stale, **and the event was already folded into that summary** —
    detected by ``recorded_at <= computed_at`` (the event arrived at or before
    the summary's last recompute). This closes the race where a late event is
    appended into an already-summarized day and the pruner runs before the
    period is marked stale: such an event has ``recorded_at > computed_at`` and
    is left in place, so it can be amended into the summary later rather than
    silently dropped. Nothing unsummarized is ever lost.

    Before deleting, the affected day summaries are latched
    ``events_pruned = TRUE``. That flag is the durable recompute-vs-amend
    marker read by :func:`mark_period_stale`: once a day's raw events are gone,
    a later late arrival into that day must be amended onto the existing
    summary rather than recomputed from the now-incomplete event set.

    Preconditions:
        * ``agent_id`` is non-empty and ``retention_days >= 0``.
    Postconditions:
        * Only this agent's events with ``occurred_at < now - retention_days``
          whose containing day summary exists, is non-stale, and has
          ``computed_at >= the event's recorded_at`` are removed, and exactly
          those day summaries are marked ``events_pruned``.
        * Events with no day summary, under a ``stale`` day summary, newer than
          the cutoff, or not yet folded into the summary (``recorded_at >
          computed_at``, incl. a NULL ``computed_at``) are retained.
    Returns:
        The number of events deleted.
    """
    assert agent_id, "prune_events: agent_id must be non-empty"
    assert retention_days >= 0, "prune_events: retention_days must be non-negative"
    cutoff = _now() - timedelta(days=retention_days)
    with _conn() as conn, conn.cursor() as cur:
        # Latch the durable regime marker on every non-stale day summary that
        # is about to lose (folded) events. Done before the DELETE — afterwards
        # the raw rows are gone and we could no longer tell which days were hit.
        cur.execute(
            """UPDATE agent_cognition_summaries s
               SET events_pruned = TRUE
               WHERE s.agent_id = %s AND s.scale = %s AND s.stale = FALSE
                 AND s.computed_at IS NOT NULL
                 AND EXISTS (
                     SELECT 1 FROM agent_cognition_events e
                     WHERE e.agent_id = s.agent_id
                       AND e.occurred_at < %s
                       AND e.occurred_at >= s.period_start
                       AND e.occurred_at < s.period_end
                       AND e.recorded_at <= s.computed_at
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
                       AND s.computed_at IS NOT NULL
                       AND e.occurred_at >= s.period_start
                       AND e.occurred_at < s.period_end
                       AND e.recorded_at <= s.computed_at
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
          rollback-on-error are delegated to the underlying ``shared.postgres``
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
