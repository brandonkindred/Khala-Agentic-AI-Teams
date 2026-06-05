"""Runtime channels that bridge the invoke boundary and the tools runner.

Two ``ContextVar``-backed channels, both scoped to a single in-flight invoke:

* **Cognition side channel** — the unwrapped ``cognition`` block (advisory rules
  + memory digest) the proxy injects. The agent *runtime* reads it via
  :func:`get_cognition_context` to render advisory rules into its system prompt
  (a generator responsibility — see DESIGN §12 Step 14). It is deliberately a
  side channel, never merged into the agent's ``input``.

* **Trusted tool-audit sink** — the out-of-band channel the tools runner's broker
  writes every tool call to (:func:`record_tool_audit`). Because the broker — not
  the model's self-reported output — populates it, the platform records what was
  *actually* brokered even when the model's writeback is dropped (e.g. a blocked
  run). The shim opens a sink with :func:`runtime_channel` and reads it off the
  context after the entrypoint returns.

Pure stdlib (``contextvars``); no Postgres, no FastAPI. ``shared_agent_invoke``
imports :func:`runtime_channel` lazily so it has no hard dependency on this
package — an agent with no cognition simply runs without a channel.

Design by Contract:

* :func:`record_tool_audit` and :func:`get_cognition_context` are safe no-ops /
  ``None`` when no channel is open, so a tools runner used in isolation (unit
  tests, in-process callers) never requires an active context.
* :func:`runtime_channel` always restores the previous context vars on exit, even
  on exception — invariant: a channel never leaks into the next invoke.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

__all__ = [
    "runtime_channel",
    "get_cognition_context",
    "collect_tool_audit",
    "record_tool_audit",
]

_cognition_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "khala_cognition_context", default=None
)
_audit_sink: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "khala_tool_audit_sink", default=None
)


@contextmanager
def runtime_channel(
    cognition: Mapping[str, Any] | None,
    sink: list[dict[str, Any]] | None,
) -> Iterator[None]:
    """Open the cognition side channel and trusted audit sink for one invoke.

    Preconditions:
        * ``cognition`` is a mapping or ``None``; ``sink`` is a caller-owned list
          (the shim reads it after the invoke) or ``None``.
    Postconditions:
        * Within the ``with`` block, :func:`get_cognition_context` returns
          ``cognition`` and :func:`record_tool_audit` appends to ``sink``. On exit
          — including on exception — both context vars are reset to their previous
          values, so nothing leaks into the next invoke.
    """
    cog_token = _cognition_context.set(dict(cognition) if cognition is not None else None)
    sink_token = _audit_sink.set(sink)
    try:
        yield
    finally:
        _cognition_context.reset(cog_token)
        _audit_sink.reset(sink_token)


def get_cognition_context() -> dict[str, Any] | None:
    """Return the cognition block for the in-flight invoke, or ``None``.

    Postconditions: the dict set by the enclosing :func:`runtime_channel`, or
    ``None`` when no channel is open. The runtime treats ``None`` as "no advisory
    steering this call".
    """
    return _cognition_context.get()


@contextmanager
def collect_tool_audit() -> Iterator[list[dict[str, Any]]]:
    """Open *only* an audit sink (no cognition block) and yield the backing list.

    Convenience for callers that want the trusted audit without injecting a
    cognition block (e.g. an in-process tool run). Postcondition: the yielded list
    accumulates every :func:`record_tool_audit` entry made within the block.
    """
    sink: list[dict[str, Any]] = []
    with runtime_channel(None, sink):
        yield sink


def record_tool_audit(entry: dict[str, Any]) -> bool:
    """Append one trusted tool-call audit entry to the active sink, if any.

    Preconditions: ``entry`` is a JSON-serializable dict (a ``ToolCall`` dump).
    Postconditions: appends to the sink opened by the enclosing
    :func:`runtime_channel` and returns ``True``; returns ``False`` (a no-op) when
    no sink is open — so the broker never fails merely because it ran outside an
    invoke context.
    """
    sink = _audit_sink.get()
    if sink is None:
        return False
    sink.append(entry)
    return True
