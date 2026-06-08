"""Background worker that drains agent memory into the knowledge graph.

Mirrors ``agent_console.prune.run_pruner``: an ``asyncio`` loop started from the
unified API lifespan. Each pass visits every agent with episodic memory and
ingests new events and rollup summaries (since that agent's watermark) into
Graphiti as temporal episodes, partitioned by ``group_id = agent_id``. Graphiti
runs the entity/relationship extraction and bi-temporal (recency) bookkeeping.

Async/sync boundary: Graphiti is async (awaited directly here, since we are in an
asyncio context); every Postgres read/write goes through ``asyncio.to_thread`` so
the synchronous psycopg stores never block the event loop — exactly the pattern
``run_pruner`` uses.

Enablement: the worker is a clean no-op (logs once and returns without looping)
when either backend is unconfigured (``NEO4J_BOLT_URL`` / ``POSTGRES_HOST``
unset), so the unit-test suite and a graph-less deployment never start it.

At-least-once delivery: the watermark advances only *after* a batch's episodes are
added. A crash mid-pass re-ingests from the last committed watermark; episodes use
stable ``name``s (``event:<id>`` / ``summary:<id>``) so Graphiti treats a re-add as
an update, not a duplicate.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from agent_cognition.graph import watermark_store
from agent_cognition.memory import store as memory_store
from agent_cognition.memory.store import AgentCognitionStorageUnavailable
from shared_neo4j import get_graphiti, is_neo4j_enabled, register_graph_indices
from shared_postgres import is_postgres_enabled

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 300
_MIN_INTERVAL_S = 30
_DEFAULT_BATCH = 50


def graph_sync_interval_seconds() -> int:
    """Seconds between sync passes (env ``AGENT_COGNITION_GRAPH_SYNC_INTERVAL_S``)."""
    raw = os.environ.get("AGENT_COGNITION_GRAPH_SYNC_INTERVAL_S", str(_DEFAULT_INTERVAL_S))
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid AGENT_COGNITION_GRAPH_SYNC_INTERVAL_S=%r; using %d", raw, _DEFAULT_INTERVAL_S
        )
        return _DEFAULT_INTERVAL_S
    return max(_MIN_INTERVAL_S, value)


def graph_sync_batch() -> int:
    """Max episodes of each kind ingested per agent per pass (env ``..._BATCH``)."""
    raw = os.environ.get("AGENT_COGNITION_GRAPH_SYNC_BATCH", str(_DEFAULT_BATCH))
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid AGENT_COGNITION_GRAPH_SYNC_BATCH=%r; using %d", raw, _DEFAULT_BATCH)
        return _DEFAULT_BATCH
    return max(1, value)


def _agent_graph_scope(agent_id: str) -> tuple[bool, bool]:
    """Return ``(ingest_events, ingest_summaries)`` for an agent.

    Reads the agent's ``cognition.knowledge_graph`` manifest block (default-on for
    a manifest-less agent); an agent with the graph disabled ingests neither.
    """
    from agent_cognition.manifest_scope import graph_scope  # noqa: PLC0415

    return graph_scope(agent_id)


async def run_graph_sync(*, interval_s: int | None = None, batch: int | None = None) -> None:
    """Periodic memory→graph ingestion loop. Cancel via the task handle to stop.

    Postconditions:
        * Returns immediately (no loop) when the knowledge-graph layer or Postgres
          is disabled.
        * Otherwise ensures the graph indices once, then every ``interval`` seconds
          ingests each agent's new memory into the graph. Storage outages are
          retried next cycle; unexpected errors are logged and the loop continues;
          ``CancelledError`` propagates so shutdown is clean.
    """
    if not is_neo4j_enabled() or not is_postgres_enabled():
        logger.info(
            "Agent Cognition graph sync worker disabled (neo4j=%s, postgres=%s); not starting",
            is_neo4j_enabled(),
            is_postgres_enabled(),
        )
        return

    interval = interval_s if interval_s is not None else graph_sync_interval_seconds()
    size = batch if batch is not None else graph_sync_batch()
    logger.info(
        "Agent Cognition graph sync worker started (interval=%ds, batch=%d)", interval, size
    )

    try:
        await register_graph_indices()
    except Exception:
        logger.warning("graph sync: building indices failed; will proceed and retry", exc_info=True)

    while True:
        try:
            await asyncio.sleep(interval)
            ingested = await _sync_once(size)
            if ingested:
                logger.info("Agent Cognition graph sync ingested %d episodes", ingested)
        except asyncio.CancelledError:
            raise
        except AgentCognitionStorageUnavailable:
            logger.debug("graph sync: storage unavailable; will retry next cycle")
        except Exception:
            logger.exception("graph sync iteration failed; continuing")


async def _sync_once(batch: int) -> int:
    """Run one full pass over all agents; return total episodes ingested."""
    agent_ids = await asyncio.to_thread(watermark_store.list_agent_ids_with_events)
    graphiti = get_graphiti()
    total = 0
    for agent_id in agent_ids:
        total += await _sync_one_agent(graphiti, agent_id, batch)
    return total


async def _sync_one_agent(graphiti: Any, agent_id: str, batch: int) -> int:
    """Ingest one agent's new events + summaries into the graph.

    Postconditions:
        * At most ``batch`` events and ``batch`` summaries are added to the graph
          (scoped to ``group_id = agent_id``), and the agent's watermark advances
          past whatever was ingested. Returns the episode count added this pass.
    """
    ingest_events, ingest_summaries = _agent_graph_scope(agent_id)
    wm = await asyncio.to_thread(watermark_store.get_watermark, agent_id)
    count = 0
    if ingest_events:
        count += await _ingest_events(graphiti, agent_id, wm, batch)
    if ingest_summaries:
        count += await _ingest_summaries(graphiti, agent_id, wm, batch)
    return count


async def _ingest_events(graphiti: Any, agent_id: str, wm: Any, batch: int) -> int:
    """Add up to ``batch`` new episodic events, then advance the event cursor."""
    from graphiti_core.nodes import EpisodeType  # noqa: PLC0415

    after_recorded_at = wm.last_event_recorded_at if wm else None
    after_id = wm.last_event_id if wm else None
    rows = await asyncio.to_thread(
        memory_store.fetch_events_recorded_after,
        agent_id,
        after_recorded_at=after_recorded_at,
        after_id=after_id,
        limit=batch,
    )
    if not rows:
        return 0
    for event, _recorded_at in rows:
        await graphiti.add_episode(
            name=f"event:{event.id}",
            episode_body=f"[{event.kind.value}] {event.content}",
            source=EpisodeType.text,
            source_description="agent episodic memory event",
            reference_time=event.occurred_at,
            group_id=agent_id,
        )
    last_event, last_recorded_at = rows[-1]
    await asyncio.to_thread(
        watermark_store.upsert_watermark,
        agent_id,
        last_event_recorded_at=last_recorded_at,
        last_event_id=last_event.id,
        ingested_delta=len(rows),
    )
    return len(rows)


async def _ingest_summaries(graphiti: Any, agent_id: str, wm: Any, batch: int) -> int:
    """Add up to ``batch`` new rollup summaries, then advance the summary cursor."""
    from graphiti_core.nodes import EpisodeType  # noqa: PLC0415

    after_created_at = wm.last_summary_created_at if wm else None
    after_id = wm.last_summary_id if wm else None
    summaries = await asyncio.to_thread(
        memory_store.fetch_summaries_created_after,
        agent_id,
        after_created_at=after_created_at,
        after_id=after_id,
        limit=batch,
    )
    if not summaries:
        return 0
    for summary in summaries:
        await graphiti.add_episode(
            name=f"summary:{summary.id}",
            episode_body=_render_summary(summary),
            source=EpisodeType.text,
            source_description=f"{summary.scale.value} rollup summary",
            reference_time=summary.period_start,
            group_id=agent_id,
        )
    last = summaries[-1]
    await asyncio.to_thread(
        watermark_store.upsert_watermark,
        agent_id,
        last_summary_created_at=last.created_at,
        last_summary_id=last.id,
        ingested_delta=len(summaries),
    )
    return len(summaries)


def _render_summary(summary: Any) -> str:
    """Render a summary as episode text: the digest plus any highlights."""
    body = summary.summary or ""
    if summary.highlights:
        body += "\nHighlights: " + "; ".join(str(h) for h in summary.highlights)
    return body
