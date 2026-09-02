"""
Shared system-prompt content assembly for the blogging agent suite.

Multiple blogging agent constructors independently wrap brand-spec and
writing-guideline text in a ``CacheBreakpoint``-marked system-content segment
so Claude can cache that stable prefix across calls instead of re-billing it
on every turn. This module gives the "what goes into the single cache-eligible
segment" decision one documented home.

The ``--- BRAND SPEC ---`` / ``--- WRITING STYLE GUIDE ---`` heading and join
logic currently duplicated in agent constructors, and wiring this function
into those constructors, are out of scope here — this module only decides,
from the two raw texts, what (if anything) becomes a ``CacheBreakpoint``.
"""

from __future__ import annotations

from typing import Optional

from llm_service import CacheBreakpoint

__all__ = ["build_blogging_system_prompt_content"]


def build_blogging_system_prompt_content(
    brand_spec_text: str, writing_guideline_text: str
) -> Optional[list]:
    """
    Wrap non-blank brand-spec / writing-guideline text in a single
    ``CacheBreakpoint``-marked system-content segment.

    Preconditions:
        - ``brand_spec_text`` and ``writing_guideline_text`` are ``str``
          (either may be empty or whitespace-only).
    Postconditions:
        - Returns ``None`` when both arguments are empty or whitespace-only.
        - Otherwise returns a one-element list ``[CacheBreakpoint(text)]``,
          where ``text`` is the non-blank argument(s) joined with ``"\\n\\n"``
          in ``(brand_spec_text, writing_guideline_text)`` order; a blank
          argument is omitted rather than contributing an empty segment.
        - Never returns more than one ``CacheBreakpoint`` (so the wire
          payload can never carry two ``cache_control`` markers).
        - Pure: no LLM client, no file I/O, no agent import, no module-level
          state; neither argument is mutated.
    """
    parts = [t for t in (brand_spec_text, writing_guideline_text) if t and t.strip()]
    if not parts:
        return None
    return [CacheBreakpoint("\n\n".join(parts))]
