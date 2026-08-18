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
        - Duck-types as a single-key ``{"text": ...}`` mapping (``__contains__``/
          ``__getitem__``, below) so a ``CacheBreakpoint`` placed directly in a
          Strands ``Agent``'s ``system_prompt=`` list — a plain
          ``list[SystemContentBlock]`` of dict-shaped blocks — survives
          Strands' own ``split_system_prompt`` (called both at ``Agent``
          construction and again by the event loop on every turn), which
          does ``block["text"] for block in system_prompt if "text" in
          block`` and returns the list unchanged as the second element of its
          result. Without this, a bare dataclass instance in that list raises
          ``TypeError`` (``"text" in block`` fails for a non-``Mapping``).
          This is additive only: every existing consumer of
          ``CacheBreakpoint`` (``strands_adapter._system_prompt_content_segments``,
          ``clients.claude._render_cache_aware_parts``) checks
          ``isinstance(x, CacheBreakpoint)`` before falling back to dict-like
          access, so this protocol never changes their behavior — it only
          makes the marker itself survive Strands' own internal list
          processing before those consumers ever see it.

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

    def __contains__(self, key: object) -> bool:
        """``"text" in breakpoint`` — the only key this marker exposes.

        Preconditions: none.
        Postconditions: returns ``key == "text"``; never raises.
        """
        return key == "text"

    def __getitem__(self, key: str) -> str:
        """``breakpoint["text"]`` — the only key this marker exposes.

        Preconditions: ``key`` should be ``"text"`` (see ``__contains__``).
        Postconditions: returns ``self.text`` for ``key == "text"``; raises
            ``KeyError`` for any other key.
        """
        if key == "text":
            return self.text
        raise KeyError(key)
