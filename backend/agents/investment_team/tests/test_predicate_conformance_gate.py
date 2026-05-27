"""Unit tests for :class:`PredicateConformanceGate`.

Tests exercise the shadow harness by feeding hand-crafted strategy code
(faithful and drifting) against synthetic fixtures and verifying the
gate's pass/fail verdicts.
"""

from __future__ import annotations

import textwrap

from investment_team.models import StrategySpec
from investment_team.strategy_lab.quality_gates.predicate_conformance import (
    PredicateConformanceGate,
    _code_conformance_retries,
    _exec_strategy,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    Predicate,
    SignalExitRule,
    StopLossRule,
)


def _spec(
    *,
    entry_rules=None,
    exit_rules=None,
    requires_custom_code: bool = True,
) -> StrategySpec:
    return StrategySpec(
        strategy_id="pred-conf-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        entry_rules=entry_rules or [],
        exit_rules=exit_rules or [],
        target_symbols=["TEST"],
        requires_custom_code=requires_custom_code,
    )


_FAITHFUL_CLOSE_GT_50 = textwrap.dedent("""\
    class MyStrategy:
        UNIVERSE = frozenset({"TEST"})
        def on_bar(self, ctx, bar):
            if ctx.is_warmup:
                return
            if bar.symbol not in self.UNIVERSE:
                return
            position = ctx.position(bar.symbol)
            if position is None and bar.close > 50:
                ctx.submit_order(symbol=bar.symbol, side="buy", qty=1.0)
            elif position is not None and bar.close <= 50:
                ctx.submit_order(symbol=bar.symbol, side="sell", qty=1.0)
""")

_DRIFTED_CLOSE_GT_50 = textwrap.dedent("""\
    class MyStrategy:
        UNIVERSE = frozenset({"TEST"})
        def on_bar(self, ctx, bar):
            if ctx.is_warmup:
                return
            if bar.symbol not in self.UNIVERSE:
                return
            position = ctx.position(bar.symbol)
            # DRIFT: uses < instead of >
            if position is None and bar.close < 50:
                ctx.submit_order(symbol=bar.symbol, side="buy", qty=1.0)
""")

_NEVER_ORDERS = textwrap.dedent("""\
    class MyStrategy:
        UNIVERSE = frozenset({"TEST"})
        def on_bar(self, ctx, bar):
            pass
""")


def _pred_close_gt_50() -> Predicate:
    return Predicate(lhs="bar.close", op=">", rhs=50.0)


class TestSkipCases:
    def test_skip_when_engine_managed(self):
        gate = PredicateConformanceGate()
        spec = _spec(
            entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")],
            requires_custom_code=False,
        )
        results = gate.check(_FAITHFUL_CLOSE_GT_50, spec)
        assert len(results) == 1
        assert results[0].passed
        assert "engine-managed" in results[0].details.lower()

    def test_skip_when_no_predicate_rules(self):
        gate = PredicateConformanceGate()
        spec = _spec(
            exit_rules=[StopLossRule(pct=0.05)],
        )
        results = gate.check(_FAITHFUL_CLOSE_GT_50, spec)
        assert len(results) == 1
        assert results[0].passed

    def test_empty_code_critical(self):
        gate = PredicateConformanceGate()
        spec = _spec(entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")])
        results = gate.check("", spec)
        assert len(results) == 1
        assert not results[0].passed
        assert results[0].severity == "critical"


class TestFaithfulStrategy:
    def test_faithful_entry_passes(self):
        gate = PredicateConformanceGate()
        spec = _spec(
            entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")],
        )
        results = gate.check(_FAITHFUL_CLOSE_GT_50, spec)
        passed = [r for r in results if r.passed]
        assert len(passed) >= 1


class TestDriftDetection:
    def test_inverted_predicate_caught(self):
        gate = PredicateConformanceGate()
        spec = _spec(
            entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")],
        )
        results = gate.check(_DRIFTED_CLOSE_GT_50, spec)
        criticals = [r for r in results if not r.passed and r.severity == "critical"]
        assert len(criticals) >= 1
        assert "false" in criticals[0].details.lower()

    def test_never_ordering_caught(self):
        gate = PredicateConformanceGate()
        spec = _spec(
            entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")],
        )
        results = gate.check(_NEVER_ORDERS, spec)
        criticals = [r for r in results if not r.passed and r.severity == "critical"]
        assert len(criticals) >= 1
        assert "false negative" in criticals[0].details.lower()


class TestPositionGuard:
    def test_no_false_negative_when_position_open(self):
        """Strategy correctly skips predicate-true bars when in a position."""
        code = textwrap.dedent("""\
            class MyStrategy:
                UNIVERSE = frozenset({"TEST"})
                def on_bar(self, ctx, bar):
                    if ctx.is_warmup:
                        return
                    if bar.symbol not in self.UNIVERSE:
                        return
                    position = ctx.position(bar.symbol)
                    if position is None and bar.close > 50:
                        ctx.submit_order(symbol=bar.symbol, side="buy", qty=1.0)
        """)
        gate = PredicateConformanceGate()
        spec = _spec(
            entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")],
        )
        results = gate.check(code, spec)
        criticals = [r for r in results if not r.passed and r.severity == "critical"]
        assert len(criticals) == 0, (
            "Should not penalise strategy for not re-entering when already in position"
        )


class TestRetryDemotion:
    def test_criticals_demoted_after_max_retries(self):
        gate = PredicateConformanceGate()
        spec = _spec(
            entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")],
        )
        results = gate.check(_DRIFTED_CLOSE_GT_50, spec, attempt=99)
        warnings = [r for r in results if not r.passed and r.severity == "warning"]
        criticals = [r for r in results if not r.passed and r.severity == "critical"]
        assert len(criticals) == 0
        assert len(warnings) >= 1

    def test_criticals_not_demoted_before_max(self):
        gate = PredicateConformanceGate()
        spec = _spec(
            entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")],
        )
        results = gate.check(_DRIFTED_CLOSE_GT_50, spec, attempt=0)
        criticals = [r for r in results if not r.passed and r.severity == "critical"]
        assert len(criticals) >= 1


class TestEnvVar:
    def test_garbage_defaults_to_2(self, monkeypatch):
        monkeypatch.setenv("STRATEGY_LAB_CODE_CONFORMANCE_RETRIES", "not_a_number")
        assert _code_conformance_retries() == 2

    def test_valid_value_respected(self, monkeypatch):
        monkeypatch.setenv("STRATEGY_LAB_CODE_CONFORMANCE_RETRIES", "5")
        assert _code_conformance_retries() == 5

    def test_negative_clamped_to_zero(self, monkeypatch):
        monkeypatch.setenv("STRATEGY_LAB_CODE_CONFORMANCE_RETRIES", "-1")
        assert _code_conformance_retries() == 0


class TestExecStrategy:
    def test_extracts_strategy_class(self):
        code = textwrap.dedent("""\
            class MyStrategy:
                def on_bar(self, ctx, bar):
                    pass
        """)
        cls = _exec_strategy(code)
        assert cls is not None
        assert cls.__name__ == "MyStrategy"

    def test_returns_none_on_syntax_error(self):
        cls = _exec_strategy("def broken(:")
        assert cls is None

    def test_returns_none_when_no_on_bar(self):
        code = textwrap.dedent("""\
            class NotAStrategy:
                pass
        """)
        cls = _exec_strategy(code)
        assert cls is None


class TestSignalExitConformance:
    def test_faithful_signal_exit_passes(self):
        code = textwrap.dedent("""\
            class MyStrategy:
                UNIVERSE = frozenset({"TEST"})
                _has_position = False
                def on_bar(self, ctx, bar):
                    if ctx.is_warmup:
                        return
                    if bar.symbol not in self.UNIVERSE:
                        return
                    position = ctx.position(bar.symbol)
                    if position is None and not self._has_position:
                        ctx.submit_order(symbol=bar.symbol, side="buy", qty=1.0)
                        self._has_position = True
                    elif position is not None and bar.close > 110:
                        ctx.submit_order(symbol=bar.symbol, side="sell", qty=1.0, reason="signal_exit")
                        self._has_position = False
        """)
        gate = PredicateConformanceGate()
        spec = _spec(
            exit_rules=[SignalExitRule(when=Predicate(lhs="bar.close", op=">", rhs=110.0))],
        )
        results = gate.check(code, spec)
        assert any(r.passed for r in results)


class TestContractImport:
    def test_strategy_with_contract_import(self):
        """Real LLM-generated code uses ``from contract import Strategy, ...``."""
        code = textwrap.dedent("""\
            from contract import OrderSide, OrderType, Strategy, TimeInForce

            class MyStrategy(Strategy):
                UNIVERSE = frozenset({"TEST"})
                def on_bar(self, ctx, bar):
                    if ctx.is_warmup:
                        return
                    if bar.symbol not in self.UNIVERSE:
                        return
                    position = ctx.position(bar.symbol)
                    if position is None and bar.close > 50:
                        ctx.submit_order(symbol=bar.symbol, side="buy", qty=1.0)
                    elif position is not None and bar.close <= 50:
                        ctx.submit_order(symbol=bar.symbol, side="sell", qty=1.0)
        """)
        gate = PredicateConformanceGate()
        spec = _spec(
            entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")],
        )
        results = gate.check(code, spec)
        passed = [r for r in results if r.passed]
        assert len(passed) >= 1, "Strategy with contract import should pass conformance"

    def test_non_contract_import_fails_gracefully(self):
        """Importing an unknown module should not crash the gate."""
        code = textwrap.dedent("""\
            import numpy as np

            class MyStrategy:
                def on_bar(self, ctx, bar):
                    pass
        """)
        gate = PredicateConformanceGate()
        spec = _spec(
            entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")],
        )
        results = gate.check(code, spec)
        criticals = [r for r in results if not r.passed and r.severity == "critical"]
        assert len(criticals) >= 1
        assert "subclass" in criticals[0].details.lower()


class TestSignalExitDrift:
    def test_missing_signal_exit_detected(self):
        """Strategy that opens a position but never exits on the predicate."""
        code = textwrap.dedent("""\
            class MyStrategy:
                UNIVERSE = frozenset({"TEST"})
                _entered = False
                def on_bar(self, ctx, bar):
                    if ctx.is_warmup:
                        return
                    if bar.symbol not in self.UNIVERSE:
                        return
                    position = ctx.position(bar.symbol)
                    if position is None and not self._entered:
                        ctx.submit_order(symbol=bar.symbol, side="buy", qty=1.0)
                        self._entered = True
                    # DRIFT: never exits on signal — ignores exit predicate
        """)
        gate = PredicateConformanceGate()
        spec = _spec(
            exit_rules=[SignalExitRule(when=Predicate(lhs="bar.close", op=">", rhs=110.0))],
        )
        results = gate.check(code, spec)
        criticals = [r for r in results if not r.passed and r.severity == "critical"]
        assert len(criticals) >= 1, "Should detect missing signal exit"
        assert "false negative" in criticals[0].details.lower()


class TestRuleIdOnResults:
    def test_rule_id_set_on_every_result(self):
        gate = PredicateConformanceGate()
        spec = _spec(
            entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")],
        )
        results = gate.check(_FAITHFUL_CLOSE_GT_50, spec)
        for r in results:
            assert r.rule_id is not None
