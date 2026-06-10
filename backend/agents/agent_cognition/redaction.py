"""Secret redaction + size bounding for values about to touch episodic memory.

A single source of truth for "make this value safe to persist": redact
secret-like keys to ``"***"`` and bound depth / item-count / string length so a
hostile or merely large value can neither leak a credential nor blow up a row.

Pure stdlib — no Postgres, no LLM, no tool machinery — so it is cheap to import
on the hot invoke path and can be shared by both the in-sandbox tool broker
(:mod:`agent_cognition.tools.runner`, which sanitizes args/results as they are
recorded) and the platform-side invoke facade
(:mod:`agent_cognition.context`, which re-sanitizes an agent's returned
writeback at the trust boundary). Keeping the logic in one module means a new
secret-key hint can never be added to one copy and forgotten in the other.

Design by Contract:

* :func:`sanitize_for_memory` — Postconditions: returns a value built only from
  JSON-friendly scalars / dicts / lists; every mapping key whose name matches a
  :data:`SECRET_KEY_HINTS` substring maps to ``"***"``; nesting deeper than
  :data:`MAX_DEPTH` collapses to a marker; mappings/sequences are truncated to
  :data:`MAX_ITEMS` entries and strings to :data:`MAX_STR` characters; an
  unknown object is stringified rather than allowed to crash the caller. Pure —
  never mutates its argument.
* :func:`is_secret_key` — Postcondition: ``True`` iff the (case-insensitive) key
  contains any :data:`SECRET_KEY_HINTS` substring.
"""

from __future__ import annotations

from collections.abc import Mapping
from itertools import islice
from typing import Any

__all__ = [
    "SECRET_KEY_HINTS",
    "MAX_STR",
    "MAX_DEPTH",
    "MAX_ITEMS",
    "is_secret_key",
    "sanitize_for_memory",
]

# Substring denylist for sanitizing values before they touch memory. Tool
# secrets ride env / secure stores, never the episodic log (DESIGN §11 Secrets).
SECRET_KEY_HINTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "credential",
    "private_key",
)
MAX_STR = 512
MAX_DEPTH = 4
MAX_ITEMS = 50


def sanitize_for_memory(value: Any, *, _depth: int = 0) -> Any:
    """Redact secret-like keys and bound size before a value touches memory."""
    if _depth >= MAX_DEPTH:
        return "<truncated:depth>"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        # islice bounds the traversal to MAX_ITEMS without first materialising
        # every item of a large mapping (which would defeat the cap).
        for key, item in islice(value.items(), MAX_ITEMS):
            key_s = str(key)
            if is_secret_key(key_s):
                out[key_s] = "***"
            else:
                out[key_s] = sanitize_for_memory(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize_for_memory(item, _depth=_depth + 1) for item in islice(value, MAX_ITEMS)]
    if isinstance(value, str) and len(value) > MAX_STR:
        return value[:MAX_STR] + "…<truncated>"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Unknown object — stringify defensively (never let logging crash the loop).
    text = repr(value)
    return text[:MAX_STR] + "…<truncated>" if len(text) > MAX_STR else text


def is_secret_key(key: str) -> bool:
    low = key.lower()
    return any(hint in low for hint in SECRET_KEY_HINTS)
