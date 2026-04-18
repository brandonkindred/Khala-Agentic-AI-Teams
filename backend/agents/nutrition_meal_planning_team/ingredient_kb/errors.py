"""Error types for the ingredient knowledge base."""

from __future__ import annotations


class KBError(Exception):
    """Base for ingredient_kb failures."""


class UnknownUnitError(KBError):
    """Raised by ``units`` when an input unit isn't in the registry."""


class AmbiguousIngredientError(KBError):
    """Raised by the parser when a string has multiple plausible resolutions
    of equal score and no tie-breaker applies.

    Consumers (SPEC-006 / SPEC-007) prefer receiving a ``None``
    canonical_id with ``reasons=['ambiguous']`` on ``ParsedIngredient``
    rather than exceptions. This error type is reserved for the lint
    path and CLI tools.
    """
