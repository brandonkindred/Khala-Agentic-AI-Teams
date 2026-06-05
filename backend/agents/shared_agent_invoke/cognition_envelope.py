"""Shim-side bridge to the cognition invoke envelope + runtime channels.

The sandbox shim must unwrap a cognition-wrapped request and carry the trusted
tool-audit channel — but ``shared_agent_invoke`` is a thin boundary component that
should *not* hard-depend on the ``agent_cognition`` package (an image without
cognition still runs agents). So every touchpoint here imports
``agent_cognition.tools`` **lazily** and degrades to a pass-through when it is
absent. Only the dependency-free ``envelope`` / ``channel`` submodules are pulled
in, never the heavy git/LLM tool stack.

Design by Contract:

* :func:`unwrap_cognition_request` — Postcondition: returns ``(agent_input,
  cognition_or_None)``; an unmarked body (or no cognition package) yields ``(body,
  None)`` unchanged, a well-formed envelope yields its ``input`` + ``cognition``,
  and a marked-but-malformed envelope raises :class:`CognitionEnvelopeError`.
* :func:`open_cognition_runtime` — Postcondition: a context manager that, within
  its block, exposes ``cognition`` to the agent runtime and routes broker
  tool-audit entries into ``sink``; a no-op context when cognition is unavailable.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

__all__ = [
    "CognitionEnvelopeError",
    "unwrap_cognition_request",
    "open_cognition_runtime",
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
def open_cognition_runtime(
    cognition: dict[str, Any] | None,
    sink: list[dict[str, Any]] | None,
) -> Iterator[None]:
    """Open the cognition side channel + trusted audit sink for one invoke.

    A no-op context when the cognition package is unavailable (the agent simply
    runs without a channel and ``sink`` stays empty).
    """
    try:
        from agent_cognition.tools.channel import runtime_channel
    except Exception:
        yield
        return
    with runtime_channel(cognition, sink):
        yield
