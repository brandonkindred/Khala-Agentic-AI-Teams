"""The marker-wrapped invoke envelope — the wire contract between the proxy and
the agent's invoke shim.

Cognition must **not** be added as a sibling key to the agent's own input: the
sandbox shim (``shared_agent_invoke.dispatch.invoke_entrypoint``) passes the POST
body straight to the manifest entrypoint, so an extra field would break agents
with strict request models or non-object inputs. Instead the proxy wraps the body
with an explicit, namespaced marker::

    { "__khala_cognition_envelope__": 1,
      "input": { ...the agent's original, unchanged request body... },
      "cognition": { "rules": [], "memory_digest": "" } }

The shim unwraps **only** when the marker is present *and* the envelope is
well-formed (marker + ``input`` + an object ``cognition``), then invokes the
entrypoint with ``input`` exactly as declared. Any body lacking the marker —
including one that happens to contain its own top-level ``input`` key — is passed
through unchanged, so no existing agent contract can be misread.

This module is the single source of truth for the marker and the wrap/unwrap
primitive (the ``CognitiveContext`` facade and the invoke proxy build on it). It
is intentionally pure: no Postgres, no LLM, no FastAPI — only dict shaping.

Design by Contract:

* :func:`wrap_request` — Precondition: ``cognition`` is a mapping. Postcondition:
  returns a fresh dict carrying the marker, the verbatim ``input``, and a shallow
  copy of ``cognition``; never mutates its arguments.
* :func:`try_unwrap_request` — Postcondition: returns ``None`` for any body
  without the marker (pass-through), an :class:`UnwrappedRequest` for a
  well-formed envelope, and raises :class:`EnvelopeError` for a body that carries
  the marker but is malformed (so a forged/garbled envelope is rejected, never
  silently fed to the entrypoint).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ENVELOPE_MARKER",
    "EnvelopeError",
    "UnwrappedRequest",
    "wrap_request",
    "is_envelope",
    "try_unwrap_request",
]

# Namespaced so a collision with real user data is vanishingly unlikely; the
# proxy rejects a caller body that already contains it (see DESIGN §10).
ENVELOPE_MARKER = "__khala_cognition_envelope__"

_ALLOWED_KEYS = frozenset({ENVELOPE_MARKER, "input", "cognition"})


class EnvelopeError(ValueError):
    """A body carries the cognition marker but is not a well-formed envelope."""


@dataclass(frozen=True)
class UnwrappedRequest:
    """The two halves of a validated cognition envelope.

    ``input`` is handed to the entrypoint exactly as the caller supplied it;
    ``cognition`` rides a separate side channel (never merged into ``input``).
    """

    input: Any
    cognition: dict[str, Any]


def wrap_request(input_body: Any, cognition: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap ``input_body`` and ``cognition`` into a marked envelope dict.

    Preconditions:
        * ``cognition`` is a mapping (e.g. a ``CognitionContext.model_dump()``).
    Postconditions:
        * Returns a new dict ``{marker: 1, "input": input_body, "cognition":
          {...}}``; ``input_body`` is referenced verbatim and ``cognition`` is
          shallow-copied. Neither argument is mutated.
    """
    if not isinstance(cognition, Mapping):
        raise EnvelopeError(f"cognition must be a mapping, got {type(cognition).__name__}")
    return {ENVELOPE_MARKER: 1, "input": input_body, "cognition": dict(cognition)}


def is_envelope(body: Any) -> bool:
    """Return whether ``body`` carries the cognition envelope marker (cheap check).

    This is presence-only — :func:`try_unwrap_request` still validates the shape.
    """
    return isinstance(body, Mapping) and ENVELOPE_MARKER in body


def try_unwrap_request(body: Any) -> UnwrappedRequest | None:
    """Unwrap ``body`` iff it is a well-formed cognition envelope.

    Postconditions:
        * ``None`` when ``body`` is not a mapping or lacks the marker — the caller
          passes the body through to the entrypoint unchanged.
        * An :class:`UnwrappedRequest` when the marker is present and the envelope
          is well-formed (``input`` and ``cognition`` both present, ``cognition``
          an object, no stray keys).
        * Raises :class:`EnvelopeError` when the marker is present but the
          envelope is malformed — a forged or corrupt envelope is rejected, never
          forwarded to the entrypoint as if it were user input.
    """
    if not isinstance(body, Mapping) or ENVELOPE_MARKER not in body:
        return None
    if "input" not in body:
        raise EnvelopeError("cognition envelope is missing 'input'")
    # The contract is marker + input + cognition; a marked body that omits
    # cognition is malformed (corrupt/forged), not a request to unwrap with an
    # empty side channel — reject it so the shim returns 400 rather than invoking
    # the entrypoint with the nested input and no cognition.
    if "cognition" not in body:
        raise EnvelopeError("cognition envelope is missing 'cognition'")
    cognition = body["cognition"]
    if not isinstance(cognition, Mapping):
        raise EnvelopeError(
            f"cognition envelope 'cognition' must be an object, got {type(cognition).__name__}"
        )
    extra = set(body) - _ALLOWED_KEYS
    if extra:
        raise EnvelopeError(f"unexpected cognition envelope keys: {sorted(extra)}")
    return UnwrappedRequest(input=body["input"], cognition=dict(cognition))
