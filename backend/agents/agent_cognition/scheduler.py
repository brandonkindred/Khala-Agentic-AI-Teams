"""The cognition scheduler — the live driver for memory rollups and rule learning.

The memory rollup engine, reflection engine, and retention pruner are all pure,
idempotent functions that were built without a caller. This worker is that caller:
a periodic ``asyncio`` loop (mirroring the graph sync worker and the agent_console
pruner) that, for every agent with memory, sequences the documented pipeline:

    ensure_rollups_current(agent_id, now)   # rote events → day/week/month/year
    reflect(agent_id, now)                   # summaries → *pending* rule proposals
    prune_events(agent_id, retention_days)   # drop folded raw events past retention

The ordering is load-bearing: reflection reads the summaries the rollup just wrote,
and pruning only deletes events already folded into a non-stale day summary. This
worker produces the summaries the knowledge-graph sync worker ingests and the
``pending`` proposals the operator approval API serves — it **never** activates a
rule (reflection only writes proposals; activation stays behind the HITL gate).

Gated on ``POSTGRES_HOST``; a clean no-op (returns without looping) when unset.
Every store call runs in ``asyncio.to_thread`` so the synchronous psycopg work
never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from agent_cognition.graph import watermark_store
from agent_cognition.memory import rollup, store
from agent_cognition.memory.store import AgentCognitionStorageUnavailable
from agent_cognition.rules import reflection
from shared_postgres import is_postgres_enabled

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 3600
_MIN_INTERVAL_S = 60


def scheduler_interval_seconds() -> int:
    """Seconds between scheduler passes (env ``AGENT_COGNITION_SCHEDULER_INTERVAL_S``)."""
    raw = os.environ.get("AGENT_COGNITION_SCHEDULER_INTERVAL_S", str(_DEFAULT_INTERVAL_S))
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid AGENT_COGNITION_SCHEDULER_INTERVAL_S=%r; using %d", raw, _DEFAULT_INTERVAL_S
        )
        return _DEFAULT_INTERVAL_S
    return max(_MIN_INTERVAL_S, value)


def _agent_retention_days(agent_id: str) -> int:
    """Raw-event retention (days) for an agent, from its manifest or the default."""
    from agent_cognition.manifest_scope import retention_days  # noqa: PLC0415

    return retention_days(agent_id)


async def run_cognition_scheduler(*, interval_s: int | None = None) -> None:
    """Periodic rollup → reflect → prune loop. Cancel via the task handle to stop.

    Postconditions:
        * Returns immediately (no loop) when Postgres is disabled.
        * Otherwise every ``interval`` seconds runs the pipeline for each agent
          with memory. A per-agent failure is logged and the pass continues to the
          next agent; storage outages retry next cycle; unexpected pass-level
          errors are logged and the loop continues; ``CancelledError`` propagates.
        * Never activates a rule — only ``reflect`` runs, which writes ``pending``
          proposals.
    """
    if not is_postgres_enabled():
        logger.info("Agent Cognition scheduler disabled (POSTGRES_HOST unset); not starting")
        return

    interval = interval_s if interval_s is not None else scheduler_interval_seconds()
    logger.info("Agent Cognition scheduler started (interval=%ds)", interval)

    while True:
        try:
            await asyncio.sleep(interval)
            await _run_once()
        except asyncio.CancelledError:
            raise
        except AgentCognitionStorageUnavailable:
            logger.debug("cognition scheduler: storage unavailable; will retry next cycle")
        except Exception:
            logger.exception("cognition scheduler iteration failed; continuing")


async def _run_once() -> None:
    """Run the rollup → reflect → prune pipeline for every agent with memory."""
    agent_ids = await asyncio.to_thread(watermark_store.list_agent_ids_with_events)
    for agent_id in agent_ids:
        try:
            await _run_one_agent(agent_id)
        except Exception:
            logger.exception("cognition scheduler: agent %s failed; continuing", agent_id)


async def _run_one_agent(agent_id: str) -> None:
    """Sequence rollups → reflection → pruning for one agent (off the event loop)."""
    now = datetime.now(timezone.utc)
    await asyncio.to_thread(rollup.ensure_rollups_current, agent_id, now)
    await asyncio.to_thread(reflection.reflect, agent_id, now)
    await asyncio.to_thread(store.prune_events, agent_id, _agent_retention_days(agent_id))
