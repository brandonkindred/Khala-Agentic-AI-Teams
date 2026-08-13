"""Background pruner for ``agent_console_runs``.

Started from the unified API lifespan alongside the sandbox idle reaper.
Configurable via env:

* ``AGENT_CONSOLE_RUNS_RETENTION``   — rows kept per agent_id (default 200).
* ``AGENT_CONSOLE_PRUNE_INTERVAL_S`` — seconds between passes (default 3600).
"""

from __future__ import annotations

import asyncio
import logging
import os

from .store import AgentConsoleStorageUnavailable, get_store

logger = logging.getLogger(__name__)


def retention_per_agent() -> int:
    raw = os.environ.get("AGENT_CONSOLE_RUNS_RETENTION", "200")
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid AGENT_CONSOLE_RUNS_RETENTION=%r; falling back to 200", raw)
        return 200
    return max(1, value)


def prune_interval_seconds() -> int:
    raw = os.environ.get("AGENT_CONSOLE_PRUNE_INTERVAL_S", "3600")
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid AGENT_CONSOLE_PRUNE_INTERVAL_S=%r; falling back to 3600", raw)
        return 3600
    return max(60, value)


async def run_pruner(
    *,
    interval_s: int | None = None,
    keep_per_agent: int | None = None,
) -> None:
    """Periodic DELETE loop. Cancel via the task handle to stop."""
    interval = interval_s if interval_s is not None else prune_interval_seconds()
    keep = keep_per_agent if keep_per_agent is not None else retention_per_agent()
    logger.info(
        "Agent Console run pruner started (keep=%d per agent, interval=%ds)",
        keep,
        interval,
    )
    store = get_store()
    while True:
        try:
            await asyncio.sleep(interval)
            deleted = await asyncio.to_thread(store.prune_runs, keep_per_agent=keep)
            if deleted:
                logger.info("Agent Console pruner removed %d stale runs", deleted)
        except asyncio.CancelledError:
            raise
        except AgentConsoleStorageUnavailable:
            logger.debug("Agent Console pruner: storage unavailable; will retry next cycle")
        except Exception:
            logger.exception("Agent Console pruner iteration failed; continuing")
