"""
Shared system-prompt content assembly for the blogging agent suite.

Multiple blogging agent constructors independently wrap brand-spec and
writing-guideline text in a ``CacheBreakpoint``-marked system-content segment
so Claude can cache that stable prefix across calls instead of re-billing it
on every turn. This module gives the "what goes into the single cache-eligible
segment" decision one documented home, plus the generic combine-with-persona
step every caller needs before handing the result to Strands
``Agent(system_prompt=...)``.

The ``--- BRAND SPEC ---`` / ``--- WRITING STYLE GUIDE ---`` heading text
itself stays caller-owned: pass already-headed text in for a heading to
appear in the cached segment.
"""

from __future__ import annotations

from typing import List, Optional, Union

from llm_service import CacheBreakpoint

__all__ = ["build_blogging_system_prompt_content", "build_system_prompt_with_content"]


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
    parts = [t for t in (brand_spec_text, writing_guideline_text) if t.strip()]
    if not parts:
        return None
    return [CacheBreakpoint("\n\n".join(parts))]


def build_system_prompt_with_content(
    system_prompt: str, system_prompt_content: Optional[list]
) -> Union[str, List[object]]:
    """
    Combine a persona string with extra system-content segments for Strands
    ``Agent(system_prompt=...)``, which accepts only one field (``str`` or a
    content-block ``list``) rather than a separate ``system_prompt_content``.

    Preconditions:
        - ``system_prompt`` is non-empty.
        - ``system_prompt_content`` is ``None``, ``[]``, or a non-empty list
          of system-content segments (e.g. from
          ``build_blogging_system_prompt_content``).
    Postconditions:
        - Returns ``system_prompt`` unchanged when ``system_prompt_content``
          is falsy.
        - Otherwise returns ``[{"text": system_prompt}, *normalized]``, where
          each segment of ``system_prompt_content`` is normalized to
          ``{"text": segment}`` if it is a bare string, and passed through
          unchanged otherwise (e.g. a ``CacheBreakpoint`` or dict block).
        - Pure: no LLM client, no file I/O, no agent import, no module-level
          state; neither argument is mutated.
    """
    if not system_prompt_content:
        return system_prompt
    normalized = [{"text": seg} if isinstance(seg, str) else seg for seg in system_prompt_content]
    return [{"text": system_prompt}, *normalized]
