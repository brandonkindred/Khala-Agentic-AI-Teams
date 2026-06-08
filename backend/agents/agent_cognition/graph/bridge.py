"""Sync→async bridge for calling the async graph layer from sync code.

The reflection engine is synchronous (it runs inside ``asyncio.to_thread`` from the
scheduler), but the graph retrieval (``build_graph_context``) is async. This bridge
runs an async coroutine to completion from such a synchronous worker-thread context.

It is best-effort by contract: graph grounding must never break rule reflection, so
:func:`run_sync` returns ``default`` (rather than raising) when there is no usable
way to run the coroutine — e.g. it is unexpectedly called from a thread that already
owns a running event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def run_sync(coro: Awaitable[T], *, default: Any = None) -> Any:
    """Run ``coro`` to completion synchronously; return ``default`` on failure.

    Postconditions:
        * Returns the coroutine's result when run from a thread with no running
          event loop (the reflection-in-``to_thread`` case).
        * Returns ``default`` — never raises — when a loop is already running in
          this thread (``asyncio.run`` would error) or the coroutine raises, so a
          caller can ground best-effort without a try/except at every site.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop in this thread — the expected path.
        try:
            return asyncio.run(coro)
        except Exception:
            logger.warning("run_sync: coroutine failed; returning default", exc_info=True)
            return default
    # A loop is already running here; we cannot block on it. Close the coroutine
    # to avoid an un-awaited warning and degrade to the default.
    logger.debug("run_sync: called from a running loop; returning default")
    if asyncio.iscoroutine(coro):
        coro.close()
    return default
