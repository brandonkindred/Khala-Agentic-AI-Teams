"""Shared helpers for phase modules: context-dict coercion and material assembly."""

from __future__ import annotations

from typing import Any, Dict, Optional, Union

from planning_team.models import ClientContext


def as_client_context(value: Union[ClientContext, Dict[str, Any], None]) -> Optional[ClientContext]:
    """Normalize context['client_context'] (ClientContext, a plain dict, or None) to a ClientContext or None."""
    if isinstance(value, dict):
        return ClientContext(**value)
    return value


def assemble_material(
    context: Dict[str, Any],
    *,
    extra_fallback: str = "",
    default: str = "No brief or spec provided.",
) -> str:
    """Join initial_brief + spec_content, mirroring discovery.py/requirements.py's Brief:/Spec: concatenation."""
    brief = context.get("initial_brief") or ""
    spec = context.get("spec_content") or ""
    if brief and spec:
        return f"Brief:\n{brief}\n\nSpec:\n{spec}"
    return brief or spec or extra_fallback or default
