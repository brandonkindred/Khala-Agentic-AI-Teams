"""Lightweight timing decorator for graph operations.

The Graphiti analogue of ``shared_postgres.metrics.timed_query``: emit a
structured ``store=shared_neo4j op=... duration_ms=...`` log line around graph
calls so slow ingests/searches surface in normal log scraping. Graphiti is async,
so this decorator handles coroutine functions (it also passes through plain sync
functions unchanged). Nothing here talks to Prometheus — it is log-only, matching
the Postgres metrics module.
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import time
from typing import Any, Callable, TypeVar

logger = logging.getLogger("shared_neo4j.metrics")

F = TypeVar("F", bound=Callable[..., Any])


def _slow_threshold_ms() -> float:
    try:
        return float(os.environ.get("NEO4J_SLOW_OP_MS", "1000"))
    except ValueError:
        return 1000.0


def _log(op_name: str, start: float, *, error: str | None = None) -> None:
    duration_ms = (time.perf_counter() - start) * 1000.0
    if error is not None:
        logger.warning(
            "store=shared_neo4j op=%s duration_ms=%.1f status=error error=%s",
            op_name,
            duration_ms,
            error,
        )
    elif duration_ms > _slow_threshold_ms():
        logger.info(
            "store=shared_neo4j op=%s duration_ms=%.1f status=ok slow=true",
            op_name,
            duration_ms,
        )
    else:
        logger.debug("store=shared_neo4j op=%s duration_ms=%.1f status=ok", op_name, duration_ms)


def timed_graph_op(op: str | None = None) -> Callable[[F], F]:
    """Decorate a sync or async graph call with before/after timing logs.

    The wrapped callable's signature and return value are preserved; exceptions
    re-raise unchanged after a WARNING is logged.
    """

    def decorator(func: F) -> F:
        op_name = op or func.__name__

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                try:
                    result = await func(*args, **kwargs)
                except Exception as e:
                    _log(op_name, start, error=type(e).__name__)
                    raise
                _log(op_name, start)
                return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
            except Exception as e:
                _log(op_name, start, error=type(e).__name__)
                raise
            _log(op_name, start)
            return result

        return sync_wrapper  # type: ignore[return-value]

    return decorator
