"""Runtime channels that bridge the invoke boundary and the tools runner.

Two ``ContextVar``-backed channels, both scoped to a single in-flight invoke:

* **Cognition side channel** — the unwrapped ``cognition`` block (advisory rules
  + memory digest) the proxy injects. The agent *runtime* reads it via
  :func:`get_cognition_context` to render advisory rules into its system prompt
  (a generator responsibility — see DESIGN §12 Step 14). It is deliberately a
  side channel, never merged into the agent's ``input``.

* **Trusted tool-audit sink** — the out-of-band channel the tools runner's broker
  writes every tool call to. Because the broker — not the model's self-reported
  output — populates it, the platform records what was *actually* brokered even
  when the model's writeback is dropped (e.g. a blocked run). The shim opens a
  sink with :func:`runtime_channel` and reads it off the context after the
  entrypoint returns.

**Write authorization (trust boundary).** The audit-write path is intentionally
**not** part of the public API: there is no exported ``record_tool_audit`` an
agent could import to forge entries. Writes go through the module-private
:func:`_record_brokered`, which only appends while a :func:`_recording_window`
is active — a window the *runner's broker* opens around each recorded call and
nothing else does. This keeps the audit trusted against the **model** (which
cannot execute Python at all) and against ordinary agent code (which has no
sanctioned writer). It does **not** defend against a deliberately compromised
in-sandbox harness that re-implements the runner's private protocol — such code
already runs arbitrary Python in the sandbox, which is exactly why secret- or
side-effect-critical tools use the ``platform_bound`` path where the broker runs
platform-side and the audit never originates in the sandbox.

Pure stdlib (``contextvars``); no Postgres, no FastAPI. ``shared_agent_invoke``
imports :func:`runtime_channel` lazily so it has no hard dependency on this
package — an agent with no cognition simply runs without a channel.

Design by Contract:

* :func:`get_cognition_context` is a safe ``None`` when no channel is open, so a
  tools runner used in isolation (unit tests, in-process callers) never requires
  an active context.
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
]

_cognition_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "khala_cognition_context", default=None
)
_audit_sink: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "khala_tool_audit_sink", default=None
)
# Armed only while the runner's broker is recording a brokered call. Writes
# outside this window are refused, so an importable writer can't forge the audit.
_recording: ContextVar[bool] = ContextVar("khala_tool_audit_recording", default=False)


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
          ``cognition`` and the runner's broker (and only it) can append to
          ``sink`` via the private recording path. On exit — including on
          exception — both context vars are reset to their previous values, so
          nothing leaks into the next invoke.
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
    accumulates every brokered call recorded within the block. Reading the audit
    is open to any caller; *writing* is still confined to the broker path.
    """
    sink: list[dict[str, Any]] = []
    with runtime_channel(None, sink):
        yield sink


# ---------------------------------------------------------------------------
# Broker-only write path (module-private — never exported, never public).
# ---------------------------------------------------------------------------
@contextmanager
def _recording_window() -> Iterator[None]:
    """Arm the audit sink for the duration of one brokered record.

    The runner's broker wraps each :func:`_record_brokered` call in this window;
    no other code opens it, so a write attempted outside a brokered dispatch is a
    no-op.
    """
    token = _recording.set(True)
    try:
        yield
    finally:
        _recording.reset(token)


def _record_brokered(entry: dict[str, Any]) -> bool:
    """Append one trusted tool-call audit entry — broker path only.

    Preconditions: ``entry`` is a JSON-serializable dict (a ``ToolCall`` dump).
    Postconditions: appends to the sink opened by the enclosing
    :func:`runtime_channel` and returns ``True`` **only** when called inside a
    :func:`_recording_window` (i.e. by the runner's broker) with a sink open;
    otherwise returns ``False`` (a no-op) — so neither an absent sink nor a
    write from outside the broker path can fail or forge the audit.
    """
    if not _recording.get():
        return False
    sink = _audit_sink.get()
    if sink is None:
        return False
    sink.append(entry)
    return True
