"""Per-agent knowledge-graph ingestion watermarks (Postgres).

Tracks how far the :mod:`agent_cognition.graph.sync_worker` has consumed each
agent's memory into the knowledge graph: a keyset cursor over episodic events
(``(recorded_at, id)``) plus a keyset cursor over rollup summaries on their
content-write time (``(updated_at, id)``), so a recomputed (version-advanced)
summary re-sorts after the cursor and is re-ingested. Mirrors
:mod:`agent_cognition.memory.store` — synchronous psycopg via the shared
``_conn`` helper, one ``@timed_query`` function per operation, agent-scoped, and a
no-op-free contract (it raises :class:`AgentCognitionStorageUnavailable` when
Postgres is unconfigured rather than silently skipping).

Invariants:
    * One row per ``agent_id`` (PK). The worker single-flights ingestion per
      agent (the scheduler owns cadence), so the cursor advances monotonically by
      construction; :func:`upsert_watermark` accumulates ``ingested_count`` and
      overwrites only the cursor fields the caller supplies (``None`` leaves the
      stored value untouched), so an event-only pass never clobbers the summary
      cursor and vice versa.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from psycopg.rows import dict_row

from agent_cognition.memory.store import _conn
from shared_postgres.metrics import timed_query

_STORE = "agent_cognition_graph"


@dataclass(frozen=True)
class GraphWatermark:
    """Ingestion progress for one agent's knowledge graph.

    Two symmetric keyset cursors: ``(last_event_recorded_at, last_event_id)`` over
    episodic events (arrival time) and ``(last_summary_updated_at, last_summary_id)``
    over rollup summaries (content-write time). ``None`` on a cursor pair means the
    worker has ingested nothing of that kind yet (cold start).
    """

    agent_id: str
    last_event_recorded_at: datetime | None
    last_event_id: str | None
    last_summary_updated_at: datetime | None
    last_summary_id: str | None
    ingested_count: int


@timed_query(store=_STORE, op="get_watermark")
def get_watermark(agent_id: str) -> GraphWatermark | None:
    """Return the agent's ingestion watermark, or ``None`` if it has none yet.

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * Returns the stored :class:`GraphWatermark`, or ``None`` for an agent the
          worker has never ingested (cold start). No rows are modified.
    """
    assert agent_id, "get_watermark: agent_id must be non-empty"
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT agent_id, last_event_recorded_at, last_event_id,
                      last_summary_updated_at, last_summary_id, ingested_count
               FROM agent_cognition_graph_watermarks
               WHERE agent_id = %s""",
            (agent_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return GraphWatermark(
        agent_id=row["agent_id"],
        last_event_recorded_at=row["last_event_recorded_at"],
        last_event_id=row["last_event_id"],
        last_summary_updated_at=row["last_summary_updated_at"],
        last_summary_id=row["last_summary_id"],
        ingested_count=row["ingested_count"],
    )


@timed_query(store=_STORE, op="upsert_watermark")
def upsert_watermark(
    agent_id: str,
    *,
    last_event_recorded_at: datetime | None = None,
    last_event_id: str | None = None,
    last_summary_updated_at: datetime | None = None,
    last_summary_id: str | None = None,
    ingested_delta: int = 0,
) -> None:
    """Insert or advance the agent's watermark.

    Preconditions:
        * ``agent_id`` is non-empty and ``ingested_delta >= 0``.
        * The event cursor is advanced as a pair: ``last_event_recorded_at`` and
          ``last_event_id`` are both set or both ``None``.
    Postconditions:
        * The row exists. Each supplied cursor field overwrites the stored value;
          a ``None`` cursor field leaves the stored value untouched (so an
          event-only pass never clears the summary cursor). ``ingested_count`` is
          incremented by ``ingested_delta``. ``updated_at`` is refreshed.

    The worker passes only forward-advancing cursors (it reads the watermark, then
    drains strictly after it), so the overwrite is monotonic by construction — the
    scheduler single-flights ingestion per agent, mirroring how the rollup/reflect
    engines rely on the scheduler for per-agent serialization.
    """
    assert agent_id, "upsert_watermark: agent_id must be non-empty"
    assert ingested_delta >= 0, "upsert_watermark: ingested_delta must be non-negative"
    assert (last_event_recorded_at is None) == (last_event_id is None), (
        "upsert_watermark: last_event_recorded_at and last_event_id must both be set or both None"
    )
    assert (last_summary_updated_at is None) == (last_summary_id is None), (
        "upsert_watermark: last_summary_updated_at and last_summary_id must both be set or both None"
    )
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO agent_cognition_graph_watermarks
                   (agent_id, last_event_recorded_at, last_event_id,
                    last_summary_updated_at, last_summary_id, ingested_count, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, NOW())
               ON CONFLICT (agent_id) DO UPDATE SET
                   last_event_recorded_at = COALESCE(
                       EXCLUDED.last_event_recorded_at,
                       agent_cognition_graph_watermarks.last_event_recorded_at),
                   last_event_id = COALESCE(
                       EXCLUDED.last_event_id,
                       agent_cognition_graph_watermarks.last_event_id),
                   last_summary_updated_at = COALESCE(
                       EXCLUDED.last_summary_updated_at,
                       agent_cognition_graph_watermarks.last_summary_updated_at),
                   last_summary_id = COALESCE(
                       EXCLUDED.last_summary_id,
                       agent_cognition_graph_watermarks.last_summary_id),
                   ingested_count = agent_cognition_graph_watermarks.ingested_count
                       + EXCLUDED.ingested_count,
                   updated_at = NOW()""",
            (
                agent_id,
                last_event_recorded_at,
                last_event_id,
                last_summary_updated_at,
                last_summary_id,
                ingested_delta,
            ),
        )


@timed_query(store=_STORE, op="list_agent_ids_with_events")
def list_agent_ids_with_events() -> list[str]:
    """Return the distinct agent ids that have at least one episodic event.

    Drives which agents the sync worker visits each pass. Cheap and index-backed.

    Postconditions:
        * A list of distinct ``agent_id`` values, sorted ascending for
          deterministic iteration. No rows are modified.
    """
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT agent_id FROM agent_cognition_events ORDER BY agent_id ASC")
        return [row[0] for row in cur.fetchall()]
