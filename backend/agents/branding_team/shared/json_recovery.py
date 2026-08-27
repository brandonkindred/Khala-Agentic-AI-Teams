"""Shared JSON-recovery helper for branding_team LLM call sites.

``orchestrator.py`` (``_parse_model_from_text``) needs to "strip markdown
fences / recover JSON from prose-wrapped text": whole-string parse first,
then an outermost ``{...}`` slice fallback. This module gives it a single
place to call instead of re-deriving that logic inline, delegating to the
team-agnostic salvage engine in ``shared.llm_recovery`` rather than
re-deriving the brace-slicing logic here.

Wired into ``orchestrator.py`` (``_parse_model_from_text``).
"""

from __future__ import annotations

from typing import Any, Collection, Dict, Optional

from shared.llm_recovery import extract_json_object

__all__ = ["recover_json_object"]


def recover_json_object(
    text: str, required_keys: Optional[Collection[str]] = None
) -> Optional[Dict[str, Any]]:
    """Recover a JSON object from LLM text, tolerating markdown fences and prose.

    A thin, strict wrapper over ``shared.llm_recovery.extract_json_object``
    (``repair=False``): only strictly-valid JSON is accepted (whole-string
    parse, or the authoritative balanced ``{...}`` recovered from fenced or
    prose-wrapped text) — no fuzzy ``json-repair`` salvage runs.

    Preconditions:
        - ``text`` is a ``str`` (may be empty).
        - ``required_keys``, when given, is the caller's expected schema keys.
          When a reply contains more than one JSON object (e.g. the real
          payload followed by a usage/metadata echo), passing the target
          schema's field names anchors selection on the object that actually
          carries them instead of silently accepting an unrelated trailing
          object — critical for schemas where every field defaults, since an
          unanchored recovery would otherwise validate successfully against
          the wrong object.
    Postconditions:
        - Returns the parsed ``dict`` on success, or ``None`` when no
          strictly-valid JSON object can be recovered, or (with
          ``required_keys`` set) none of the candidate objects carry any of
          them. Never raises.
    """
    if not text:
        return None
    return extract_json_object(text, required_keys, repair=False)
