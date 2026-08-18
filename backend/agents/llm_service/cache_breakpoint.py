"""Cache-breakpoint marking primitive for stable prompt prefixes.

Introduces :class:`CacheBreakpoint`, a small immutable marker a prompt
builder wraps around a prefix segment (a spec excerpt, an architecture
overview, a system prompt, ...) to declare: "this exact text is stable and
safe to send as a provider-side cached prefix on repeated calls."

This module is deliberately self-contained and inert: constructing a
``CacheBreakpoint`` performs no provider call, no I/O, and no
global/contextvar mutation — it is pure data. A later pipeline stage (the
Strands model wrapper, tracked separately) is responsible for walking a
prompt/messages structure, recognizing ``CacheBreakpoint`` instances, and
translating them into the provider's wire-level cache-control block (e.g.
Anthropic's ``cache_control: {"type": "ephemeral"}``). That translation, and
the cache-token telemetry it enables, are out of scope here — this module
only defines the marker and its contract.

Usage (illustrative; call sites are adopted in a later step)::

    from llm_service import CacheBreakpoint

    spec_prefix = CacheBreakpoint(spec_excerpt_text)
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["CacheBreakpoint"]


@dataclass(frozen=True)
class CacheBreakpoint:
    """Marks a stable prompt-prefix segment as a provider cache breakpoint.

    Invariants:
        - Immutable once constructed (frozen dataclass): ``text`` never
          changes after ``__post_init__`` validates it.
        - ``text`` is always a non-empty ``str`` — enforced in
          ``__post_init__``, never coerced.
        - Constructing an instance has no observable side effect: no
          provider call, no I/O, no contextvar/global mutation. Two
          instances with equal ``text`` compare equal and hash equal
          (inherited dataclass behavior), so callers may deduplicate or use
          a ``CacheBreakpoint`` as a dict/set key.

    ``text`` must be the *exact* prefix content, byte-for-byte, that the
    caller intends the provider to cache — this module does not normalize,
    strip, or otherwise transform it, since any transformation could
    silently change what gets cached and break the byte-stability a
    provider cache breakpoint depends on.
    """

    text: str

    def __post_init__(self) -> None:
        """Validate the marked prefix text.

        Preconditions:
            - ``text`` must be a ``str``.
        Postconditions:
            - No return value; raises ``ValueError`` if ``text`` is not a
              non-empty ``str``, otherwise leaves the instance unchanged.
        """
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("CacheBreakpoint.text must be a non-empty string")
