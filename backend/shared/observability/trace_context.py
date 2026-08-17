"""Cross-phase trace-id propagation via :mod:`contextvars`.

Carries a single ``trace_id`` string through the call stack — including
across worker threads fanned out via :func:`shared.concurrency.parallel_map`
— so log lines and telemetry emitted anywhere in a job's execution can be
correlated back to the job that originated them, without threading the id
through every function signature by hand.

``shared.concurrency.parallel_map`` needs no change to support this: with its
default ``propagate_context=True`` it copies the caller's *entire* active
``contextvars.Context`` into each worker (``contextvars.copy_context().run``),
so a ``trace_id`` bound before a ``parallel_map`` call is automatically visible
inside every worker — the same mechanism that already carries
``llm_service``'s attribution / request-id contextvars.

The pattern mirrors ``llm_service/attribution.py``'s ``request_id`` contextvar
(itself documented as mirroring ``agent_team_studio/agent_provisioning_team/shared/logging_context.py``).

Usage::

    from shared.observability import bind_trace_id, current_trace_id, new_trace_id

    with bind_trace_id(new_trace_id()):
        ...  # current_trace_id() == the bound value everywhere in this block,
             # including inside parallel_map workers started from here
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def current_trace_id() -> str:
    """Return the trace id bound for the in-flight job, or ``""`` when none.

    Postconditions: non-empty only while a :func:`bind_trace_id` block is
        active on this thread/task.
    """
    return _trace_id.get()


def new_trace_id() -> str:
    """Return a short, unique, log-friendly trace id.

    Postconditions: returns a 12-char lowercase hex string; successive calls
        return distinct values with overwhelming probability (uuid4-derived).
    """
    return uuid.uuid4().hex[:12]


@contextmanager
def bind_trace_id(trace_id: str) -> Iterator[str]:
    """Bind ``trace_id`` for the duration of the ``with`` block.

    Preconditions: ``trace_id`` is a non-empty string.
    Postconditions: inside the block, :func:`current_trace_id` returns
        ``trace_id``; on exit (including via exception) the previous value is
        restored.
    """
    if not trace_id:
        # Explicit validation rather than ``assert``: the precondition must hold
        # even under ``python -O`` (which strips asserts), or callers would
        # silently bind an empty id and break trace correlation.
        raise ValueError("trace_id must be a non-empty string")
    prev = _trace_id.get()
    token = _trace_id.set(trace_id)
    try:
        yield trace_id
    finally:
        try:
            _trace_id.reset(token)
        except (LookupError, ValueError):  # pragma: no cover - context torn down out of order
            _trace_id.set(prev)


__all__ = ["bind_trace_id", "current_trace_id", "new_trace_id"]
