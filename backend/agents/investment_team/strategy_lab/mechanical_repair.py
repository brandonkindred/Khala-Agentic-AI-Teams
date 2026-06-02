"""Deterministic, semantics-preserving repair of mechanical readiness failures.

The design ↔ design-review loop runs :class:`SpecReadinessGate` at the top of
every round. When the gate emits a *critical* finding the loop otherwise spends
a full LLM ``DesignAgent.revise`` round to fix it — even when the violation is
purely mechanical and fully determined by the spec. This module performs those
fixes deterministically *before* the LLM revise path so the costly loop is
reserved for genuinely substantive defects.

Scope is deliberately minimal — only the two least-debatable, fully-determined
repairs — plus a trial compile:

* **Timeframe data availability** (readiness Rule 7): coerce an intraday
  ``timeframe`` to ``"1d"`` for asset classes with no intraday data (anything
  not in :data:`spec_readiness._FULL_TIMEFRAME_ASSET_CLASSES`).
* **Position-cap bound** (readiness Rule 8): clamp
  ``risk_limits.max_position_pct`` down to
  :data:`spec_readiness.MAX_POSITION_PCT_CEILING`.
* **Trial compile**: attempt :func:`compile_strategy`; on
  :class:`CompilerError` set ``requires_custom_code=True`` so the custom-code
  path is selected during design rather than discovered later in synthesis
  (mirrors the orchestrator's existing synthesis-phase fallback).

Each repair is guarded by exactly the condition its readiness rule checks,
recomputed from the spec via the gate's own shared constants — never parsed
from gate-message text — so the rule and its repair cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from ..models import BacktestConfig, StrategySpec
from ..strategy_lab_context import normalize_asset_class_strict
from .quality_gates.spec_readiness import (
    _FULL_TIMEFRAME_ASSET_CLASSES,
    MAX_POSITION_PCT_CEILING,
)
from .synthesis import CompilerError, compile_strategy


@dataclass(frozen=True)
class RepairAction:
    """One deterministic spec edit, recorded for the audit trail.

    Invariants:
      - ``rule`` names the mechanical class; ``field`` names the spec field
        changed; ``before`` / ``after`` are the field values around the edit.
    """

    rule: str
    field: str
    before: Any
    after: Any
    reason: str


@dataclass(frozen=True)
class RepairOutcome:
    """Result of :func:`repair_spec`.

    Invariants:
      - ``actions == []`` ⇔ ``spec`` is the (unchanged) input instance.
      - ``actions`` non-empty ⇒ ``spec`` is a new ``StrategySpec`` carrying
        every recorded edit.
    """

    spec: StrategySpec
    actions: List[RepairAction] = field(default_factory=list)


def repair_spec(
    spec: StrategySpec,
    *,
    config: Optional[BacktestConfig] = None,
    attempt_compile: bool = True,
) -> RepairOutcome:
    """Apply the in-scope deterministic mechanical repairs to ``spec``.

    Preconditions:
      - ``spec`` is a constructed :class:`StrategySpec`.
      - ``config`` is a :class:`BacktestConfig` or ``None`` (accepted for a
        uniform call site; the current repairs do not consult it).

    Postconditions:
      - Returns a :class:`RepairOutcome`. When no repair applies, the input
        ``spec`` is returned unchanged and ``actions`` is empty.
      - Every applied repair targets exactly the condition its readiness rule
        rejects, so re-running ``repair_spec`` on the result yields
        ``actions == []`` (idempotent) — barring external state changes.
      - The returned spec is never mutated in place; edits are made on a deep
        copy.
    """
    assert isinstance(spec, StrategySpec), "spec must be a StrategySpec"
    assert config is None or isinstance(config, BacktestConfig), (
        "config must be a BacktestConfig or None"
    )

    actions: List[RepairAction] = []
    updates: dict[str, Any] = {}

    # --- Rule 7: intraday timeframe on an asset class with no intraday data.
    if spec.timeframe != "1d":
        try:
            canonical = normalize_asset_class_strict(spec.asset_class)
        except ValueError:
            # Unknown asset_class — Rule 5 owns that critical; not a timeframe
            # repair. Leave the timeframe untouched.
            canonical = None
        if canonical is not None and canonical not in _FULL_TIMEFRAME_ASSET_CLASSES:
            actions.append(
                RepairAction(
                    rule="timeframe_data_availability",
                    field="timeframe",
                    before=spec.timeframe,
                    after="1d",
                    reason=(
                        f"asset_class '{spec.asset_class}' has no reliable intraday "
                        f"data for timeframe '{spec.timeframe}'; coerced to '1d'."
                    ),
                )
            )
            updates["timeframe"] = "1d"

    # --- Rule 8: single-position risk budget over the ceiling.
    if spec.risk_limits.max_position_pct > MAX_POSITION_PCT_CEILING:
        actions.append(
            RepairAction(
                rule="max_position_pct_cap",
                field="risk_limits.max_position_pct",
                before=spec.risk_limits.max_position_pct,
                after=MAX_POSITION_PCT_CEILING,
                reason=(
                    f"max_position_pct={spec.risk_limits.max_position_pct:g}% exceeds the "
                    f"{MAX_POSITION_PCT_CEILING:g}% cap; clamped to the ceiling."
                ),
            )
        )
        updates["risk_limits"] = spec.risk_limits.model_copy(
            update={"max_position_pct": MAX_POSITION_PCT_CEILING}
        )

    repaired = spec.model_copy(update=updates, deep=True) if updates else spec

    # --- Trial compile: select the custom-code path now rather than in synthesis.
    if attempt_compile and not repaired.requires_custom_code:
        try:
            compile_strategy(repaired)
        except CompilerError as exc:
            actions.append(
                RepairAction(
                    rule="compiler_fallback",
                    field="requires_custom_code",
                    before=False,
                    after=True,
                    reason=(
                        "spec falls outside the deterministic compiler envelope "
                        f"({exc}); routing to the custom-code synthesis path."
                    ),
                )
            )
            repaired = repaired.model_copy(update={"requires_custom_code": True})

    if not actions:
        return RepairOutcome(spec=spec, actions=[])
    return RepairOutcome(spec=repaired, actions=actions)
