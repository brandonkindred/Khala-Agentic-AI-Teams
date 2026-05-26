"""End-to-end tests for :class:`RuleProbesGate` with a stubbed sandbox runner.

These tests do **not** spawn subprocesses — they inject a fake runner
that inspects the ``market_data`` it receives and returns a hand-crafted
``StrategyRunResult``. That keeps tests fast (<100ms total) and isolates
the gate's own behaviour from the broader trading-service plumbing.

A separate sandbox-driven test exists for end-to-end confidence (see
``test_rule_probes_orchestrator.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from investment_team.models import StrategySpec, TradeRecord
from investment_team.strategy_lab.quality_gates.rule_probes import RuleProbesGate
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
)


@dataclass
class _FakeResult:
    """Minimal stand-in for :class:`StrategyRunResult`."""

    success: bool = True
    trades: List[TradeRecord] = field(default_factory=list)
    error_type: Optional[str] = None
    stderr: str = ""


def _trade(
    *,
    side: str = "long",
    entry_date: str = "9999-01-01",
    exit_date: str = "9999-12-31",
    exit_reason: Optional[str] = None,
    shares: float = 10.0,
    symbol: str = "PROBE",
) -> TradeRecord:
    """Build a fake TradeRecord. Default dates are deliberately well after
    any plausible probe trigger so the asserter's trigger-timing checks
    accept the trade — tests that need to exercise the early-trade
    rejection paths pass explicit early dates."""
    return TradeRecord(
        trade_num=1,
        entry_date=entry_date,
        exit_date=exit_date,
        symbol=symbol,
        side=side,
        entry_price=100.0,
        exit_price=110.0,
        shares=shares,
        position_value=1000.0,
        gross_pnl=100.0,
        net_pnl=100.0,
        return_pct=0.10,
        hold_days=10,
        outcome="win",
        cumulative_pnl=100.0,
        exit_reason=exit_reason,
    )


def _spec(*, entry_rules=None, exit_rules=None, code: str = "x = 1\n") -> StrategySpec:
    return StrategySpec(
        strategy_id="probe-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        entry_rules=entry_rules or [],
        exit_rules=exit_rules or [],
        target_symbols=["PROBE"],
        strategy_code=code,
    )


def _entry_rsi_lt(side: str = "long") -> EntryRule:
    return EntryRule(
        side=side,
        when=Predicate(
            lhs=IndicatorRef(name="rsi", params={"period": 14}),
            op="<",
            rhs=30.0,
        ),
    )


# ---------------------------------------------------------------------------
# Acceptance scenarios named in the issue
# ---------------------------------------------------------------------------


def test_happy_path_rsi_entry_probe_passes():
    """Stub runner pretends the strategy opened a long position at the trigger
    bar. The probe should pass with severity=info, passed=True."""

    def runner(code, market_data, config, *, strategy=None):
        return _FakeResult(success=True, trades=[_trade(side="long")])

    gate = RuleProbesGate(runner=runner)
    spec = _spec(entry_rules=[_entry_rsi_lt()])
    results = gate.check(spec.strategy_code, spec)
    assert len(results) == 1
    r = results[0]
    assert r.passed is True
    assert r.severity == "info"
    assert r.rule_id == "entry[0]"


def test_swapped_comparator_fails_rsi_entry_probe():
    """Acceptance: a strategy implementing ``rsi > 30`` against a spec asking
    for ``rsi < 30`` will not produce any long trade on a falling-RSI series.
    The stub mimics that by returning no trades."""

    def runner(code, market_data, config, *, strategy=None):
        return _FakeResult(success=True, trades=[])

    gate = RuleProbesGate(runner=runner)
    spec = _spec(entry_rules=[_entry_rsi_lt()])
    results = gate.check(spec.strategy_code, spec)
    [r] = results
    assert r.passed is False
    assert r.severity == "critical"
    assert r.rule_id == "entry[0]"
    assert "long" in r.details


def test_missing_stop_loss_branch_fails_stop_loss_probe():
    """Acceptance: a strategy that forgets the stop-loss branch produces no
    closing trade with ``exit_reason`` matching ``stop_loss``."""

    # The stub opens a long position on the entry trigger but never emits a
    # stop-loss close. Mirrors a strategy whose engine_exit handling is
    # missing the stop_loss case.
    def runner(code, market_data, config, *, strategy=None):
        return _FakeResult(
            success=True,
            trades=[_trade(side="long", exit_reason="manual_close")],
        )

    gate = RuleProbesGate(runner=runner)
    spec = _spec(
        entry_rules=[_entry_rsi_lt()],
        exit_rules=[StopLossRule(pct=0.03)],
    )
    results = gate.check(spec.strategy_code, spec)
    entry_result, exit_result = results
    assert exit_result.passed is False
    assert exit_result.severity == "critical"
    assert exit_result.rule_id == "exit[0]:stop_loss"
    assert "stop_loss" in exit_result.details


# ---------------------------------------------------------------------------
# Per-exit-kind happy and negative paths
# ---------------------------------------------------------------------------


def test_take_profit_probe_passes_when_engine_emits_take_profit_reason():
    def runner(code, market_data, config, *, strategy=None):
        return _FakeResult(
            success=True,
            trades=[
                _trade(side="long", exit_reason="engine_exit:take_profit"),
            ],
        )

    gate = RuleProbesGate(runner=runner)
    spec = _spec(
        entry_rules=[_entry_rsi_lt()],
        exit_rules=[TakeProfitRule(pct=0.05)],
    )
    results = gate.check(spec.strategy_code, spec)
    [_entry, tp] = results
    assert tp.passed is True
    assert tp.rule_id == "exit[0]:take_profit"


def test_signal_exit_probe_passes_when_compiled_signal_exit_reason_present():
    def runner(code, market_data, config, *, strategy=None):
        return _FakeResult(
            success=True,
            trades=[
                _trade(side="long", exit_reason="compiled_signal_exit"),
            ],
        )

    gate = RuleProbesGate(runner=runner)
    signal = SignalExitRule(
        when=Predicate(lhs="bar.close", op="<", rhs=10.0),
    )
    spec = _spec(entry_rules=[_entry_rsi_lt()], exit_rules=[signal])
    results = gate.check(spec.strategy_code, spec)
    [_entry, sig] = results
    assert sig.passed is True
    assert sig.rule_id == "exit[0]:signal_exit"


def test_sandbox_failure_is_critical():
    def runner(code, market_data, config, *, strategy=None):
        return _FakeResult(success=False, error_type="runtime_error", stderr="boom")

    gate = RuleProbesGate(runner=runner)
    spec = _spec(entry_rules=[_entry_rsi_lt()])
    [r] = gate.check(spec.strategy_code, spec)
    assert r.passed is False
    assert r.severity == "critical"
    assert "runtime_error" in r.details
    assert "boom" in r.details


# ---------------------------------------------------------------------------
# Unprobeable predicates surface as warnings
# ---------------------------------------------------------------------------


def test_unprobeable_rule_emits_warning():
    """An exit-only spec (no entry rule to open a position) is unprobeable.
    The gate emits a warning rather than a critical."""

    def runner(code, market_data, config, *, strategy=None):
        # The gate should never invoke the runner for unprobeable rules.
        raise AssertionError("runner must not be called for unprobeable rules")

    gate = RuleProbesGate(runner=runner)
    spec = _spec(entry_rules=[], exit_rules=[StopLossRule(pct=0.05)])
    [r] = gate.check(spec.strategy_code, spec)
    assert r.severity == "warning"
    assert r.rule_id == "exit[0]:stop_loss"
    assert "unprobeable" in r.details.lower()


# ---------------------------------------------------------------------------
# Misc: empty-rule spec, every-result-carries-rule_id invariant
# ---------------------------------------------------------------------------


def test_spec_with_no_rules_returns_info():
    def runner(code, market_data, config, *, strategy=None):
        raise AssertionError("runner must not be called when there are no rules")

    gate = RuleProbesGate(runner=runner)
    spec = _spec(entry_rules=[], exit_rules=[])
    [r] = gate.check(spec.strategy_code, spec)
    assert r.severity == "info"
    assert r.passed is True


def test_empty_strategy_code_returns_critical_without_invoking_runner():
    def runner(code, market_data, config, *, strategy=None):
        raise AssertionError("runner must not be called when code is empty")

    gate = RuleProbesGate(runner=runner)
    spec = _spec(entry_rules=[_entry_rsi_lt()], code="")
    [r] = gate.check("", spec)
    assert r.severity == "critical"


def test_every_result_carries_a_rule_id_or_no_rule():
    """Invariant: every result from RuleProbesGate either carries a rule_id
    (a per-rule probe outcome) or is a meta-result with no rule_id."""

    def runner(code, market_data, config, *, strategy=None):
        return _FakeResult(success=True, trades=[_trade(side="long")])

    gate = RuleProbesGate(runner=runner)
    spec = _spec(
        entry_rules=[_entry_rsi_lt()],
        exit_rules=[StopLossRule(pct=0.05), TakeProfitRule(pct=0.05)],
    )
    results = gate.check(spec.strategy_code, spec)
    # 1 entry + 2 exits = 3 probe results, all with rule_id set.
    assert len(results) == 3
    for r in results:
        assert r.rule_id is not None and r.rule_id.startswith(("entry[", "exit["))


def test_runner_is_called_with_tiny_initial_capital_and_zero_costs():
    seen_configs = []

    def runner(code, market_data, config, *, strategy=None):
        seen_configs.append(config)
        return _FakeResult(success=True, trades=[_trade(side="long")])

    gate = RuleProbesGate(runner=runner)
    spec = _spec(entry_rules=[_entry_rsi_lt()])
    gate.check(spec.strategy_code, spec)
    assert seen_configs, "runner was never invoked"
    cfg = seen_configs[0]
    assert cfg.initial_capital == 1_000.0
    assert cfg.transaction_cost_bps == 0.0
    assert cfg.slippage_bps == 0.0


def test_runner_receives_synthesised_market_data_keyed_by_target_symbol():
    seen_md = []

    def runner(code, market_data, config, *, strategy=None):
        seen_md.append(market_data)
        return _FakeResult(success=True, trades=[_trade(side="long")])

    gate = RuleProbesGate(runner=runner)
    spec = _spec(entry_rules=[_entry_rsi_lt()])  # target_symbols=["PROBE"]
    gate.check(spec.strategy_code, spec)
    assert seen_md and "PROBE" in seen_md[0]
    assert len(seen_md[0]["PROBE"]) > 0
