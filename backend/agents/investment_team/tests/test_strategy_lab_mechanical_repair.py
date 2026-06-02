"""Unit tests for the deterministic mechanical-repair pre-flight.

These pin the fully-determined, semantics-preserving repairs applied before
the LLM ``DesignAgent.revise`` path: intraday-timeframe coercion (Rule 7),
position-cap clamping (Rule 8), and the trial compile that selects the
custom-code path on ``CompilerError``. Each repair is idempotent and only
fires on exactly the condition its readiness rule rejects.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab import mechanical_repair as mr
from investment_team.strategy_lab.mechanical_repair import RepairOutcome, repair_spec
from investment_team.strategy_lab.quality_gates.spec_readiness import (
    MAX_POSITION_PCT_CEILING,
    SpecReadinessGate,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)
from investment_team.strategy_lab.synthesis import CompilerError


def _make_spec(**overrides: Any) -> StrategySpec:
    base: Dict[str, Any] = {
        "strategy_id": "s1",
        "authored_by": "design_agent",
        "asset_class": "stocks",
        "hypothesis": "RSI mean reversion",
        "signal_definition": "RSI(14) crossings",
        "timeframe": "1d",
        "entry_rules": [
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30),
            ).model_dump()
        ],
        "exit_rules": [
            SignalExitRule(
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70)
            ).model_dump()
        ],
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10},
        "target_symbols": ["QQQ"],
        "speculative": False,
    }
    base.update(overrides)
    return StrategySpec(**base)


# ---------------------------------------------------------------------------
# Rule 7 — timeframe data availability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("asset_class", ["forex", "commodities", "futures"])
def test_intraday_timeframe_coerced_to_daily_for_non_intraday_class(asset_class: str) -> None:
    spec = _make_spec(asset_class=asset_class, timeframe="1h", target_symbols=[])
    out = repair_spec(spec, attempt_compile=False)
    rules = {a.rule for a in out.actions}
    assert "timeframe_data_availability" in rules
    assert out.spec.timeframe == "1d"
    # The input spec is never mutated.
    assert spec.timeframe == "1h"


@pytest.mark.parametrize("asset_class", ["stocks", "crypto"])
def test_intraday_timeframe_untouched_for_intraday_class(asset_class: str) -> None:
    spec = _make_spec(asset_class=asset_class, timeframe="1h", target_symbols=[])
    out = repair_spec(spec, attempt_compile=False)
    assert all(a.rule != "timeframe_data_availability" for a in out.actions)


def test_daily_timeframe_never_touched() -> None:
    spec = _make_spec(asset_class="forex", timeframe="1d", target_symbols=[])
    out = repair_spec(spec, attempt_compile=False)
    assert all(a.rule != "timeframe_data_availability" for a in out.actions)


def test_unknown_asset_class_skips_timeframe_repair() -> None:
    # Bypass construction-time validation via model_copy so the gate/repair
    # see an off-vocabulary class (the same path the gate guards against).
    spec = _make_spec(asset_class="stocks", timeframe="1h").model_copy(
        update={"asset_class": "bonds"}
    )
    out = repair_spec(spec, attempt_compile=False)
    assert all(a.rule != "timeframe_data_availability" for a in out.actions)
    assert out.spec.timeframe == "1h"


# ---------------------------------------------------------------------------
# Rule 8 — position-cap bound
# ---------------------------------------------------------------------------


def test_max_position_pct_clamped_to_ceiling() -> None:
    spec = _make_spec(risk_limits={"max_position_pct": 40, "max_drawdown_pct": 10})
    out = repair_spec(spec, attempt_compile=False)
    clamp = [a for a in out.actions if a.rule == "max_position_pct_cap"]
    assert len(clamp) == 1
    assert clamp[0].before == 40.0
    assert clamp[0].after == MAX_POSITION_PCT_CEILING
    assert out.spec.risk_limits.max_position_pct == MAX_POSITION_PCT_CEILING
    assert spec.risk_limits.max_position_pct == 40.0  # input untouched


@pytest.mark.parametrize("pct", [25, 20, 5])
def test_position_pct_at_or_below_ceiling_untouched(pct: int) -> None:
    spec = _make_spec(risk_limits={"max_position_pct": pct})
    out = repair_spec(spec, attempt_compile=False)
    assert all(a.rule != "max_position_pct_cap" for a in out.actions)


# ---------------------------------------------------------------------------
# Trial compile
# ---------------------------------------------------------------------------


def test_compiler_error_flips_requires_custom_code() -> None:
    # volatility_target sizing without a matching atr indicator is outside the
    # deterministic compiler envelope → CompilerError → custom-code fallback.
    spec = _make_spec(sizing={"kind": "volatility_target", "target_annual_vol": 0.15})
    out = repair_spec(spec)
    fallback = [a for a in out.actions if a.rule == "compiler_fallback"]
    assert len(fallback) == 1
    assert out.spec.requires_custom_code is True
    assert spec.requires_custom_code is False  # input untouched


def test_compilable_spec_has_no_compile_action() -> None:
    spec = _make_spec()
    out = repair_spec(spec)
    assert all(a.rule != "compiler_fallback" for a in out.actions)
    assert out.actions == []
    assert out.spec is spec  # unchanged identity when no repair applies


def test_already_custom_code_skips_trial_compile(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    def _boom(_spec: Any) -> str:
        called["n"] += 1
        raise CompilerError("should not be called")

    monkeypatch.setattr(mr, "compile_strategy", _boom)
    spec = _make_spec(requires_custom_code=True)
    out = repair_spec(spec)
    assert called["n"] == 0
    assert all(a.rule != "compiler_fallback" for a in out.actions)


def test_attempt_compile_false_skips_trial_compile(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    def _boom(_spec: Any) -> str:
        called["n"] += 1
        raise CompilerError("should not be called")

    monkeypatch.setattr(mr, "compile_strategy", _boom)
    spec = _make_spec(sizing={"kind": "volatility_target", "target_annual_vol": 0.15})
    repair_spec(spec, attempt_compile=False)
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Combined / idempotency / re-validation contract
# ---------------------------------------------------------------------------


def test_combined_repairs_and_idempotency() -> None:
    spec = _make_spec(
        asset_class="forex",
        timeframe="1h",
        target_symbols=["EURUSD=X"],
        risk_limits={"max_position_pct": 40, "max_drawdown_pct": 10},
    )
    out = repair_spec(spec)
    rules = {a.rule for a in out.actions}
    assert {"timeframe_data_availability", "max_position_pct_cap"} <= rules
    # Re-running on the repaired spec is a no-op.
    assert repair_spec(out.spec).actions == []


def test_clean_spec_is_noop() -> None:
    spec = _make_spec()
    out = repair_spec(spec)
    assert isinstance(out, RepairOutcome)
    assert out.actions == []
    assert out.spec is spec


def test_repaired_spec_clears_readiness_criticals() -> None:
    """After repairing a timeframe+cap-only spec, the readiness gate reports
    no criticals — the repair and the gate agree."""
    spec = _make_spec(
        asset_class="forex",
        timeframe="1h",
        target_symbols=["EURUSD=X"],
        risk_limits={"max_position_pct": 40, "max_drawdown_pct": 10},
    )
    gate = SpecReadinessGate()
    before = gate.validate(spec, phase="design")
    assert any((not r.passed) and r.severity == "critical" for r in before)

    out = repair_spec(spec, attempt_compile=False)
    after = gate.validate(out.spec, phase="design")
    assert not any((not r.passed) and r.severity == "critical" for r in after)
