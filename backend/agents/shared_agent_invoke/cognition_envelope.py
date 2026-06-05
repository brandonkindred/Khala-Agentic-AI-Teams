"""Shim-side bridge to the cognition invoke envelope + tool loop.

The sandbox shim must unwrap a cognition-wrapped request, expose the cognition
side channel, and — when the agent returns a :class:`ToolLoopPlan` — **drive the
brokered tool loop itself** so the trusted audit never passes through
agent-reachable state. But ``shared_agent_invoke`` is a thin boundary component
that should *not* hard-depend on ``agent_cognition`` (an image without cognition
still runs agents), so every touchpoint here imports it **lazily** and degrades
to a pass-through when it is absent.

Design by Contract:

* :func:`unwrap_cognition_request` — Postcondition: returns ``(agent_input,
  cognition_or_None)``; an unmarked body (or no cognition package) yields ``(body,
  None)`` unchanged, a well-formed envelope yields its ``input`` + ``cognition``,
  and a marked-but-malformed envelope raises :class:`CognitionEnvelopeError`.
* :func:`open_cognition_runtime` — Postcondition: a context manager exposing
  ``cognition`` to the agent runtime; a no-op when cognition is unavailable.
* :func:`maybe_drive_tool_loop` — Postcondition: returns ``(output, tool_audit)``.
  When the entrypoint returned a :class:`ToolLoopPlan`, the loop is driven here and
  ``tool_audit`` is the trusted per-call record (read off the runner's return value
  in this frame); otherwise the result passes through with an empty audit.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

__all__ = [
    "CognitionEnvelopeError",
    "unwrap_cognition_request",
    "open_cognition_runtime",
    "maybe_drive_tool_loop",
]


class CognitionEnvelopeError(ValueError):
    """A request carried the cognition marker but was not a valid envelope."""


def unwrap_cognition_request(body: Any) -> tuple[Any, dict[str, Any] | None]:
    """Split a (possibly) cognition-wrapped body into ``(input, cognition)``.

    Returns ``(body, None)`` for an unmarked body or when the cognition package is
    not importable. Raises :class:`CognitionEnvelopeError` for a marked-but-
    malformed envelope so the caller can reject it (HTTP 400).
    """
    try:
        from agent_cognition.tools.envelope import EnvelopeError, try_unwrap_request
    except Exception:  # cognition not present in this image — pass through unchanged
        return body, None
    try:
        unwrapped = try_unwrap_request(body)
    except EnvelopeError as exc:
        raise CognitionEnvelopeError(str(exc)) from exc
    if unwrapped is None:
        return body, None
    return unwrapped.input, unwrapped.cognition


@contextmanager
def open_cognition_runtime(cognition: dict[str, Any] | None) -> Iterator[None]:
    """Open the cognition side channel for one invoke.

    A no-op context when the cognition package is unavailable (the agent simply
    runs without a channel).
    """
    try:
        from agent_cognition.tools.channel import runtime_channel
    except Exception:
        yield
        return
    with runtime_channel(cognition):
        yield


def maybe_drive_tool_loop(
    result: Any,
    *,
    agent_id: str,
    source_run_id: str,
    cognition: dict[str, Any] | None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Drive the brokered loop when the entrypoint returned a ``ToolLoopPlan``.

    The shim — not agent code — calls this, so the runner's :class:`ToolAudit`
    lives only in this frame. The active ``forbid_tool`` rules are sourced from
    the platform-injected ``cognition`` block (not the agent's plan), so a
    compromised harness cannot disable its own gate. Synchronous (the loop makes
    blocking LLM calls); the shim runs it off the event loop.

    Returns ``(result, [])`` unchanged when the result is not a plan, or when the
    cognition package is unavailable.
    """
    try:
        from agent_cognition.models import Rule
        from agent_cognition.tools.runner import ToolLoopPlan, execute_plan
    except Exception:
        return result, []
    if not isinstance(result, ToolLoopPlan):
        return result, []

    enforced_rules = []
    for raw in (cognition or {}).get("rules", []) or []:
        try:
            enforced_rules.append(Rule.model_validate(raw))
        except Exception:
            continue  # a malformed rule never blocks the run; the gate just skips it
    final, audit = execute_plan(
        result,
        agent_id=agent_id,
        source_run_id=source_run_id,
        enforced_rules=enforced_rules,
    )
    return final, [tc.model_dump(mode="json") for tc in audit.tool_calls]
