"""Shared helpers for phase modules: context-dict coercion and material assembly."""

from __future__ import annotations

from typing import Any, Dict, Optional, Union

from pydantic import ValidationError

from planning_team.models import ClientContext

DEFAULT_MATERIAL_FALLBACK = "No brief or spec provided."


def as_client_context(value: Union[ClientContext, Dict[str, Any], None]) -> Optional[ClientContext]:
    """Normalize context['client_context'] (ClientContext, a plain dict, or None) to a ClientContext or None.

    Preconditions:
        - if ``value`` is a dict, any key it shares with a ``ClientContext`` field must be
          coercible to that field's type; unknown/extra keys are ignored (mirroring
          ``ClientContext``'s default pydantic ``extra="ignore"`` behavior).
    Raises:
        - ``ValueError`` if ``value`` is a dict with a known field of an incompatible type
          — re-raised from the underlying ``pydantic.ValidationError`` so the failure is
          identified at the coercion point instead of surfacing as a bare traceback deep
          in phase code.
    """
    if isinstance(value, dict):
        try:
            return ClientContext(**value)
        except ValidationError as exc:
            raise ValueError(f"Invalid client_context dict: {exc}") from exc
    return value


def assemble_material(context: Dict[str, Any], *, default: str = DEFAULT_MATERIAL_FALLBACK) -> str:
    """Join initial_brief + spec_content, mirroring discovery.py/requirements.py's Brief:/Spec: concatenation."""
    brief = context.get("initial_brief") or ""
    spec = context.get("spec_content") or ""
    if brief and spec:
        return f"Brief:\n{brief}\n\nSpec:\n{spec}"
    return brief or spec or default
