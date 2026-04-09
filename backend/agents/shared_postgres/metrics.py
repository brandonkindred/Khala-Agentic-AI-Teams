"""Lightweight query-timing decorator for store methods.

Each migrated store wraps its methods with ``@timed_query(store="...")``
so ops can grep for ``store=branding op=save_client duration_ms=12`` in
the logs. Nothing here talks to Prometheus or OpenTelemetry — we only
emit structured log lines so the migration doesn't block on wiring up a
metrics pipeline.

Slow queries (``duration_ms > SLOW_QUERY_THRESHOLD_MS``, default 100)
are logged at INFO so they surface in normal log scraping; everything
else stays at DEBUG.
"""

from __future__ import annotations

import logging
import os
import time
from functools import wraps
from typing import Any, Callable, TypeVar

logger = logging.getLogger("shared_postgres.metrics")

F = TypeVar("F", bound=Callable[..., Any])


def _slow_threshold_ms() -> float:
    try:
        return float(os.environ.get("POSTGRES_SLOW_QUERY_MS", "100"))
    except ValueError:
        return 100.0


def timed_query(store: str, op: str | None = None) -> Callable[[F], F]:
    """Decorate a store method with before/after timing logs.

    Args:
        store: Team/store slug used in log lines, e.g. ``"branding"``
            or ``"unified_api_credentials"``.
        op: Operation name. Defaults to the wrapped function's
            ``__name__`` — pass explicitly only when you need something
            other than the method name.

    The wrapped function's signature and return value are preserved.
    Exceptions re-raise unchanged after logging the failure at WARNING.
    """

    def decorator(func: F) -> F:
        op_name = op or func.__name__

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
            except Exception as e:
                duration_ms = (time.perf_counter() - start) * 1000.0
                logger.warning(
                    "store=%s op=%s duration_ms=%.1f status=error error=%s",
                    store,
                    op_name,
                    duration_ms,
                    type(e).__name__,
                )
                raise
            duration_ms = (time.perf_counter() - start) * 1000.0
            if duration_ms > _slow_threshold_ms():
                logger.info(
                    "store=%s op=%s duration_ms=%.1f status=ok slow=true",
                    store,
                    op_name,
                    duration_ms,
                )
            else:
                logger.debug(
                    "store=%s op=%s duration_ms=%.1f status=ok",
                    store,
                    op_name,
                    duration_ms,
                )
            return result

        return wrapper  # type: ignore[return-value]

    return decorator
