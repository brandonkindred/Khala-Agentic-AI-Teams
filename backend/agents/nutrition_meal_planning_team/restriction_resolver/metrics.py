"""OTel counters for the SPEC-006 restriction resolver.

Two instruments are emitted from the resolver:

- ``nutrition.restriction.resolve`` — one point per input with
  ``outcome`` ∈ {resolved, ambiguous, unresolved} and ``rule`` ∈
  {exact_alias, shorthand, category, negation, fuzzy, unresolved}.
- ``nutrition.restriction.shorthand_used`` — one point per shorthand
  expansion, labelled with the shorthand ``name``.

Both are lazily initialized so the resolver remains a pure function
for call sites that haven't set up OTel (tests, direct imports).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_initialized = False
_counter_resolve: Optional[Any] = None
_counter_shorthand: Optional[Any] = None


def _ensure_instruments() -> None:
    global _initialized, _counter_resolve, _counter_shorthand
    if _initialized:
        return
    _initialized = True
    try:
        from shared_observability import get_meter

        meter = get_meter("khala.nutrition.restriction")
        _counter_resolve = meter.create_counter(
            "nutrition.restriction.resolve",
            description="Count of restriction resolutions by outcome and rule",
        )
        _counter_shorthand = meter.create_counter(
            "nutrition.restriction.shorthand_used",
            description="Count of shorthand expansions by canonical name",
        )
    except Exception:
        logger.debug("restriction resolver metrics init failed", exc_info=True)
        _counter_resolve = None
        _counter_shorthand = None


def record_outcome(outcome: str, rule: str = "") -> None:
    """Record one resolver outcome. No-op if OTel is unavailable."""
    _ensure_instruments()
    if _counter_resolve is None:
        return
    try:
        _counter_resolve.add(1, {"outcome": outcome, "rule": rule})
    except Exception:
        logger.debug("restriction resolve counter add failed", exc_info=True)


def record_shorthand(name: str) -> None:
    """Record one shorthand expansion. No-op if OTel is unavailable."""
    _ensure_instruments()
    if _counter_shorthand is None:
        return
    try:
        _counter_shorthand.add(1, {"name": name})
    except Exception:
        logger.debug("restriction shorthand counter add failed", exc_info=True)
