"""Nutritionist agent: narrator on top of the nutrition_calc calculator.

Schemas are imported eagerly (pure Pydantic, no LLM dependency). The
``NutritionistAgent`` class pulls in ``llm_service`` (via
``get_client`` / ``complete_validated``) which transitively requires
``strands``; we lazy-load the class so unit tests that only need the
schemas or payload types do not pay that import cost.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .schemas import GuidanceOnlyPayload, NarrativePayload

if TYPE_CHECKING:  # pragma: no cover - type-checker hint only
    from .agent import NutritionistAgent  # noqa: F401

__all__ = [
    "GuidanceOnlyPayload",
    "NarrativePayload",
    "NutritionistAgent",
]


def __getattr__(name: str) -> Any:
    if name == "NutritionistAgent":
        from .agent import NutritionistAgent as _NutritionistAgent

        globals()[name] = _NutritionistAgent
        return _NutritionistAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
