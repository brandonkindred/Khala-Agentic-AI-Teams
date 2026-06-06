"""Cognition side channel — the read-only context the proxy injects on invoke.

A single ``ContextVar`` scoped to one in-flight invoke: the unwrapped
``cognition`` block (advisory rules + memory digest). The agent *runtime* reads
it via :func:`get_cognition_context` to render advisory rules into its system
prompt (a generator responsibility — see DESIGN §12 Step 14). It is deliberately
a side channel, never merged into the agent's ``input``.

**There is no audit channel here.** The trusted tool-call audit is *not* carried
through any ambient/importable state, because in-sandbox agent code shares the
process and a leading underscore is not an access boundary — any writer reachable
to the runner is reachable to a compromised harness. Instead the **shim drives the
tool loop itself** (it executes the :class:`~agent_cognition.tools.runner.ToolLoopPlan`
an agent returns) and reads the audit straight off
:func:`~agent_cognition.tools.runner.run_tool_loop`'s return value, which lives only
in the shim's frame. Agent code never holds the audit object and has nothing to
forge into. Integrity-critical tools additionally use the ``platform_bound`` path,
where the broker runs platform-side.

Pure stdlib (``contextvars``); no Postgres, no FastAPI. ``shared_agent_invoke``
imports :func:`runtime_channel` lazily so it has no hard dependency on this
package — an agent with no cognition simply runs without a channel.

Design by Contract:

* :func:`get_cognition_context` is a safe ``None`` when no channel is open.
* :func:`runtime_channel` always restores the previous context var on exit, even
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
]

_cognition_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "khala_cognition_context", default=None
)


@contextmanager
def runtime_channel(cognition: Mapping[str, Any] | None) -> Iterator[None]:
    """Open the cognition side channel for one invoke.

    Preconditions:
        * ``cognition`` is a mapping or ``None``.
    Postconditions:
        * Within the ``with`` block, :func:`get_cognition_context` returns
          ``cognition``. On exit — including on exception — the context var is
          reset to its previous value, so nothing leaks into the next invoke.
    """
    token = _cognition_context.set(dict(cognition) if cognition is not None else None)
    try:
        yield
    finally:
        _cognition_context.reset(token)


def get_cognition_context() -> dict[str, Any] | None:
    """Return the cognition block for the in-flight invoke, or ``None``.

    Postconditions: the dict set by the enclosing :func:`runtime_channel`, or
    ``None`` when no channel is open. The runtime treats ``None`` as "no advisory
    steering this call".
    """
    return _cognition_context.get()
