"""Shared system-prompt assembly for Strands Agent list-form system prompts.

Strands' ``Agent`` accepts ``system_prompt`` as either a plain ``str`` or a
``list[SystemContentBlock]`` — never both. When extra system-content segments
(e.g. a ``CacheBreakpoint``-marked spec excerpt) need to accompany the persona,
this helper normalizes both the persona and the segments into the list form
Strands expects. When no segments are present, it returns the persona as a
plain string so callers that never use extra segments get byte-identical
behavior.

This is the single implementation of the normalize-and-prepend logic.
Consumers include:

- ``shared.persona_agent_base.run_structured_persona``
- ``code_review_agent.via_reasoning.run_agent_via_reasoning``

Both previously had their own copy of this same function.
"""

from __future__ import annotations

from typing import Any, List

__all__ = ["build_system_prompt_with_content"]


def build_system_prompt_with_content(
    system_prompt: str, system_prompt_content: "List[Any] | None"
) -> "str | List[Any]":
    """Combine persona text with extra system-content segments.

    When ``system_prompt_content`` is provided, returns a list suitable for
    Strands ``Agent(system_prompt=...)`` — the persona wrapped as a native
    ``{"text": ...}`` block, followed by each segment (bare strings normalized
    to ``{"text": str}``; ``CacheBreakpoint`` and dict blocks passed through
    as-is). When absent, returns the plain string unchanged.

    Only **trusted** metadata (spec excerpts, architecture overviews) should
    be placed in ``system_prompt_content``. Untrusted content (code under
    review, repository-controlled text) must remain in the user message.

    Preconditions:
        ``system_prompt`` is non-empty. ``system_prompt_content`` is ``None``,
        ``[]``, or a non-empty list of system-content segments.
    Postconditions:
        Returns ``system_prompt`` unchanged when ``system_prompt_content`` is
        falsy; otherwise returns a list ``[{"text": system_prompt}, *normalized]``.
    """
    if not system_prompt_content:
        return system_prompt
    normalized = [
        {"text": seg} if isinstance(seg, str) else seg for seg in system_prompt_content
    ]
    return [{"text": system_prompt}, *normalized]
