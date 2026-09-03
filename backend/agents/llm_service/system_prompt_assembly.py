"""Shared system-prompt assembly for Strands Agent list-form system prompts.

Strands' ``Agent`` accepts ``system_prompt`` as either a plain ``str`` or a
``list[SystemContentBlock]`` — never both. When extra system-content segments
(e.g. a ``CacheBreakpoint``-marked spec excerpt) need to accompany the persona,
this helper normalizes both the persona and the segments into the list form
Strands expects. When no segments are present, it returns the persona as a
plain string so callers that never use extra segments get byte-identical
behavior.

This is the single implementation of the normalize-and-prepend logic, hoisted
here (rather than owned by any one agent team) because it is generic Strands
plumbing with no team-specific behavior, and every team already depends on
``llm_service`` for ``CacheBreakpoint`` and ``LLMClient``. Team packages that
need it re-export this implementation rather than defining their own copy:

- ``software_engineering_team.shared.system_prompt_assembly``
- ``blogging.shared.system_prompt_assembly``
"""

from __future__ import annotations

from typing import Any

from .cache_breakpoint import CacheBreakpoint

__all__ = ["build_system_prompt_with_content", "SystemContentSegment"]

SystemContentSegment = str | dict[str, Any] | CacheBreakpoint


def build_system_prompt_with_content(
    system_prompt: str, system_prompt_content: list[SystemContentSegment] | None
) -> str | list[Any]:
    """Combine persona text with extra system-content segments.

    When ``system_prompt_content`` contains at least one segment, returns a
    list suitable for Strands ``Agent(system_prompt=...)`` — the persona
    wrapped as a native ``{"text": ...}`` block, followed by each segment
    (bare strings normalized to ``{"text": str}``; ``CacheBreakpoint`` and
    dict blocks passed through as-is). When ``system_prompt_content`` is
    ``None`` or empty, returns the plain string unchanged.

    Only **trusted** metadata (spec excerpts, architecture overviews) should
    be placed in ``system_prompt_content``. Untrusted content (code under
    review, repository-controlled text) must remain in the user message.

    Preconditions:
        ``system_prompt`` is non-empty (enforced — raises ``ValueError``
        otherwise). ``system_prompt_content`` is ``None``, ``[]``, or a
        non-empty list of system-content segments.
    Postconditions:
        Returns ``system_prompt`` unchanged when ``system_prompt_content`` is
        falsy; otherwise returns a list ``[{"text": system_prompt}, *normalized]``.
    """
    if not system_prompt:
        raise ValueError("system_prompt must be non-empty")
    if not system_prompt_content:
        return system_prompt
    normalized = [{"text": seg} if isinstance(seg, str) else seg for seg in system_prompt_content]
    return [{"text": system_prompt}, *normalized]
