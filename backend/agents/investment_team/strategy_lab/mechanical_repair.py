"""Deterministic, semantics-preserving repair of mechanical readiness failures.

The design ↔ design-review loop runs :class:`SpecReadinessGate` at the top of
every round. When the gate emits a *critical* finding the loop otherwise spends
a full LLM ``DesignAgent.revise`` round to fix it — even when the violation is
purely mechanical and fully determined by the spec. This module performs those
fixes deterministically *before* the LLM revise path so the costly loop is
reserved for genuinely substantive defects.

Scope is deliberately minimal — only the two least-debatable, fully-determined
mechanical repairs, exposed via :func:`repair_spec`:

* **Timeframe data availability** (readiness Rule 7): coerce an intraday
  ``timeframe`` to ``"1d"`` for asset classes with no intraday data (anything
  not in :data:`spec_readiness._FULL_TIMEFRAME_ASSET_CLASSES`).
* **Position-cap bound** (readiness Rule 8): clamp
  ``risk_limits.max_position_pct`` down to
  :data:`spec_readiness.MAX_POSITION_PCT_CEILING`.

The custom-code decision is a separate, *readiness-gated* concern handled by
:func:`select_code_path`: it trial-compiles a spec and, on :class:`CompilerError`,
reports that ``requires_custom_code`` should be set so the custom-code path is
selected during design rather than discovered later in synthesis (mirroring the
orchestrator's existing synthesis-phase fallback). It is kept out of
:func:`repair_spec` because the compiler assumes structurally valid DSL — a
readiness-defective spec can make ``compile_strategy`` raise a *non*-``CompilerError``
— so callers must only invoke it once the readiness gate reports no criticals.

Each mechanical repair is guarded by exactly the condition its readiness rule
checks, recomputed from the spec via the gate's own shared constants — never
parsed from gate-message text — so the rule and its repair cannot drift apart.
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
) -> RepairOutcome:
    """Apply the in-scope deterministic *mechanical* repairs to ``spec``.

    This function only ever applies fully-determined readiness repairs
    (timeframe data availability, position-cap bound). It deliberately does
    **not** trial-compile: a spec may still carry a readiness-detectable
    structural defect that would make :func:`compile_strategy` raise a
    *non*-``CompilerError``, so the trial compile lives in
    :func:`select_code_path`, which callers invoke only once the spec is
    readiness-clean.

    Preconditions:
      - ``spec`` is a constructed :class:`StrategySpec`.
      - ``config`` is a :class:`BacktestConfig` or ``None`` (accepted for a
        uniform call site; the current repairs do not consult it).

    Postconditions:
      - Returns a :class:`RepairOutcome`. When no repair applies, the input
        ``spec`` is returned unchanged and ``actions`` is empty.
      - Never raises on the spec's compilability — only structural readiness
        fields are touched, so this is safe to call on a readiness-critical spec.
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

    if not actions:
        return RepairOutcome(spec=spec, actions=[])
    return RepairOutcome(spec=spec.model_copy(update=updates, deep=True), actions=actions)


def select_code_path(spec: StrategySpec) -> Optional[RepairAction]:
    """Trial-compile ``spec`` to decide whether it needs the custom-code path.

    Preconditions:
      - ``spec`` is structurally valid (readiness-clean). The deterministic
        compiler assumes valid DSL; a spec with a readiness-detectable defect
        (e.g. an ``sma`` ref whose required ``period`` was removed) can make
        :func:`compile_strategy` raise a *non*-``CompilerError`` such as
        ``TypeError``. Callers running inside the design loop must therefore only
        invoke this once the readiness gate reports no criticals — a residual
        critical is left to the readiness-critique / revise path.

    Postconditions:
      - Returns a ``compiler_fallback`` :class:`RepairAction` (the caller should
        set ``requires_custom_code=True``) when the spec is outside the
        deterministic-compiler envelope; returns ``None`` when the spec compiles
        or already has ``requires_custom_code`` set.
    """
    assert isinstance(spec, StrategySpec), "spec must be a StrategySpec"
    if spec.requires_custom_code:
        return None
    try:
        compile_strategy(spec)
    except CompilerError as exc:
        return RepairAction(
            rule="compiler_fallback",
            field="requires_custom_code",
            before=False,
            after=True,
            reason=(
                "spec falls outside the deterministic compiler envelope "
                f"({exc}); routing to the custom-code synthesis path."
            ),
        )
    return None
