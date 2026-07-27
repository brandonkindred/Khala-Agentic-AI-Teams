"""Shared JSON-recovery helper for branding_team LLM call sites.

``orchestrator.py`` (``_parse_model_from_text``) and ``assistant/agent.py``
(``_loads_lenient``) each independently reimplement "strip markdown fences /
recover JSON from prose-wrapped text": whole-string parse first, then an
outermost ``{...}`` slice fallback. This module gives both a single place to
call instead, delegating to the team-agnostic salvage engine in
``shared.llm_recovery`` rather than re-deriving the brace-slicing logic here.

Not yet wired into either call site — that migration is tracked separately.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from shared.llm_recovery import extract_json_object

__all__ = ["recover_json_object"]


def recover_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Recover a JSON object from LLM text, tolerating markdown fences and prose.

    A thin, strict wrapper over ``shared.llm_recovery.extract_json_object``
    (``repair=False``): only strictly-valid JSON is accepted (whole-string
    parse, or the authoritative balanced ``{...}`` recovered from fenced or
    prose-wrapped text) — no fuzzy ``json-repair`` salvage runs, matching the
    strict two-step behavior both existing call sites hand-roll today.

    Preconditions:
        - ``text`` is a ``str`` (may be empty).
    Postconditions:
        - Returns the parsed ``dict`` on success, or ``None`` when no
          strictly-valid JSON object can be recovered. Never raises.
    """
    if not text:
        return None
    return extract_json_object(text, repair=False)
