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
    _build_conformance_detail,
    _code_conformance_retries,
    _enriched_trace_lines,
    _exec_strategy,
    _format_scalar,
    _predicate_for_rule_id,
)
from investment_team.strategy_lab.quality_gates.predicate_conformance_fixtures import (
    ConformanceFixture,
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

    def test_engine_owned_signal_exit_skipped_when_covered(self):
        """Entries-only custom code is conformant when ``spec.exit_rules``
        cover the entered side: the ``SignalExitRule`` is engine-owned, so
        its fixture is skipped and the absent manual close is not a false
        negative.

        Without the engine-coverage skip this same drift (enter, never exit)
        would raise a false-negative critical — see
        ``test_missing_signal_exit_detected``, which uses an entry-less spec
        so coverage does not apply.
        """
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
                    # No manual exit: the engine owns the SignalExitRule.
        """)
        gate = PredicateConformanceGate()
        spec = _spec(
            entry_rules=[EntryRule(when=Predicate(lhs="bar.close", op=">", rhs=50.0), side="long")],
            exit_rules=[SignalExitRule(when=Predicate(lhs="bar.close", op=">", rhs=110.0))],
        )
        results = gate.check(code, spec)
        criticals = [r for r in results if not r.passed and r.severity == "critical"]
        assert criticals == [], criticals
        # The signal-exit fixture was skipped (engine-owned); only the entry
        # fixture ran.
        assert not any(r.rule_id and "signal_exit" in r.rule_id for r in results)
        assert any(r.rule_id and r.rule_id.startswith("entry") for r in results)

    def test_signal_exit_skipped_even_with_side_specific_stop(self):
        """A SignalExitRule is engine-owned for BOTH sides, so its fixture is
        skipped whenever the strategy has an entered side — even alongside a
        side-specific stop that covers only one side. Confirms the all-sides
        coverage gate cannot wrongly retain a signal-exit fixture, because
        the signal rule itself covers every side."""
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
                    # No manual exit: the engine owns the signal exit.
        """)
        gate = PredicateConformanceGate()
        spec = _spec(
            entry_rules=[EntryRule(when=Predicate(lhs="bar.close", op=">", rhs=50.0), side="long")],
            exit_rules=[
                SignalExitRule(when=Predicate(lhs="bar.close", op=">", rhs=110.0)),
                StopLossRule(pct=0.05, basis="trailing_high"),
            ],
        )
        results = gate.check(code, spec)
        criticals = [r for r in results if not r.passed and r.severity == "critical"]
        assert criticals == [], criticals
        assert not any(r.rule_id and "signal_exit" in r.rule_id for r in results)


class TestEnumSideHandling:
    def test_strategy_using_orderside_enum(self):
        """Strategies that pass ``OrderSide.LONG`` must be handled correctly."""
        code = textwrap.dedent("""\
            from contract import OrderSide, Strategy

            class MyStrategy(Strategy):
                UNIVERSE = frozenset({"TEST"})
                def on_bar(self, ctx, bar):
                    if ctx.is_warmup:
                        return
                    if bar.symbol not in self.UNIVERSE:
                        return
                    position = ctx.position(bar.symbol)
                    if position is None and bar.close > 50:
                        ctx.submit_order(symbol=bar.symbol, side=OrderSide.LONG, qty=1.0)
                    elif position is not None and bar.close <= 50:
                        ctx.submit_order(symbol=bar.symbol, side=OrderSide.SHORT, qty=1.0)
        """)
        gate = PredicateConformanceGate()
        spec = _spec(
            entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")],
        )
        results = gate.check(code, spec)
        passed = [r for r in results if r.passed]
        assert len(passed) >= 1, "Strategy using OrderSide enum should pass"


class TestOnStartHook:
    def test_strategy_with_on_start(self):
        """Strategy that initialises state in on_start must not crash the shadow run."""
        code = textwrap.dedent("""\
            class MyStrategy:
                UNIVERSE = frozenset({"TEST"})
                def on_start(self, ctx):
                    self._ready = True
                def on_bar(self, ctx, bar):
                    if ctx.is_warmup:
                        return
                    if bar.symbol not in self.UNIVERSE:
                        return
                    if not self._ready:
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
        assert len(passed) >= 1, "Strategy with on_start should pass"


class TestSandboxImports:
    def test_strategy_importing_indicators(self):
        """Strategies that use ``from indicators import sma`` must not crash."""
        code = textwrap.dedent("""\
            from contract import Strategy
            from indicators import sma

            class MyStrategy(Strategy):
                UNIVERSE = frozenset({"TEST"})
                def on_bar(self, ctx, bar):
                    if ctx.is_warmup:
                        return
                    if bar.symbol not in self.UNIVERSE:
                        return
                    history = ctx.history(bar.symbol, 20)
                    if len(history) < 20:
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
        # Should not fail with "Could not extract Strategy subclass"
        strategy_fail = [r for r in results if "subclass" in r.details.lower()]
        assert len(strategy_fail) == 0

    def test_strategy_importing_datetime(self):
        """Stdlib imports like datetime must be allowed."""
        code = textwrap.dedent("""\
            import datetime

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
        strategy_fail = [r for r in results if "subclass" in r.details.lower()]
        assert len(strategy_fail) == 0


class TestShortEntryStrategy:
    def test_faithful_short_entry_passes(self):
        """A short-entry strategy that correctly fires on predicate-true bars."""
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
                        ctx.submit_order(symbol=bar.symbol, side="short", qty=1.0)
                    elif position is not None and bar.close <= 50:
                        ctx.submit_order(symbol=bar.symbol, side="buy", qty=1.0)
        """)
        gate = PredicateConformanceGate()
        spec = _spec(
            entry_rules=[EntryRule(when=_pred_close_gt_50(), side="short")],
        )
        results = gate.check(code, spec)
        passed = [r for r in results if r.passed]
        assert len(passed) >= 1, "Faithful short-entry strategy should pass"


class TestRuleIdOnResults:
    def test_rule_id_set_on_every_result(self):
        gate = PredicateConformanceGate()
        spec = _spec(
            entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")],
        )
        results = gate.check(_FAITHFUL_CLOSE_GT_50, spec)
        for r in results:
            assert r.rule_id is not None


def _drift_critical(gate, spec, *, attempt=0):
    """Return the first non-passing conformance result for the drifted entry."""
    results = gate.check(_DRIFTED_CLOSE_GT_50, spec, attempt=attempt)
    failing = [r for r in results if not r.passed]
    assert failing, "expected at least one conformance failure"
    return failing[0]


class TestTargetedRepairTrace:
    """Enriched failure detail (predicate + per-bar lhs/rhs/verdict)."""

    def test_details_include_predicate_expression(self):
        gate = PredicateConformanceGate()
        spec = _spec(entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")])
        result = _drift_critical(gate, spec, attempt=0)
        assert result.severity == "critical"
        # Rendered predicate ("bar.close > 50") and at least one enriched bar line.
        assert "Predicate:" in result.details
        # _format_predicate renders the bar-field literal without the "bar." prefix.
        assert "close > 50" in result.details
        assert "lhs=" in result.details
        assert "rhs=" in result.details
        assert "engine=" in result.details

    def test_details_include_lhs_rhs_values(self):
        gate = PredicateConformanceGate()
        spec = _spec(entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")])
        result = _drift_critical(gate, spec, attempt=0)
        # The threshold (rhs) is a literal 50 — it must surface verbatim in a trace row.
        assert "rhs=50" in result.details
        # A directive line tells the refiner which branch to fix.
        assert "Fix the on_bar branch implementing 'entry[0]'" in result.details

    def test_repair_before_demote_attempt_sequence(self, monkeypatch):
        """Two critical (repairable) rounds precede the demotion warning."""
        monkeypatch.delenv("STRATEGY_LAB_CODE_CONFORMANCE_RETRIES", raising=False)
        gate = PredicateConformanceGate()
        spec = _spec(entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")])
        assert _drift_critical(gate, spec, attempt=0).severity == "critical"
        assert _drift_critical(gate, spec, attempt=1).severity == "critical"
        # attempt == max_retries (default 2) -> demoted to warning.
        assert _drift_critical(gate, spec, attempt=2).severity == "warning"

    def test_enriched_details_bounded(self):
        """Enriched per-bar rows are capped and the index list is retained."""
        gate = PredicateConformanceGate()
        spec = _spec(entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")])
        result = _drift_critical(gate, spec, attempt=0)
        bar_lines = [ln for ln in result.details.splitlines() if ln.strip().startswith("bar ")]
        assert 1 <= len(bar_lines) <= 5
        # The bare (≤10) index list is still present alongside the enriched rows.
        assert "False positives" in result.details or "False negatives" in result.details


class TestUnsynthesizableWarning:
    def test_unsynthesizable_detail_prefix(self):
        """The unsynthesizable warning carries a stable prefix the telemetry excludes."""
        gate = PredicateConformanceGate()
        fixture = ConformanceFixture(
            rule_id="entry[0]",
            rule_kind="entry",
            side="long",
            synthesizable=False,
            unsynthesizable_reason="no forcing sequence",
        )
        with gate._using_phase("synthesis"):
            result = gate._check_fixture(object, fixture, spec=None, demote=False)
        assert result.severity == "warning"
        assert result.details.startswith("Fixture unsynthesizable:")


class TestPredicateForRuleId:
    def test_resolves_entry_predicate(self):
        pred = _pred_close_gt_50()
        spec = _spec(entry_rules=[EntryRule(when=pred, side="long")])
        fixture = ConformanceFixture(rule_id="entry[0]", rule_kind="entry", side="long")
        assert _predicate_for_rule_id(spec, fixture) is pred

    def test_resolves_signal_exit_predicate(self):
        pred = Predicate(lhs="bar.close", op="<", rhs=90.0)
        spec = _spec(
            exit_rules=[StopLossRule(pct=0.05), SignalExitRule(when=pred)],
        )
        # exit_rules[1] is the SignalExitRule -> rule_id carries that index.
        fixture = ConformanceFixture(rule_id="exit[1]:signal_exit", rule_kind="signal_exit")
        assert _predicate_for_rule_id(spec, fixture) is pred

    def test_malformed_rule_id_returns_none(self):
        spec = _spec(entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")])
        fixture = ConformanceFixture(rule_id="bogus[x]", rule_kind="entry", side="long")
        assert _predicate_for_rule_id(spec, fixture) is None

    def test_out_of_range_index_returns_none(self):
        spec = _spec(entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")])
        fixture = ConformanceFixture(rule_id="entry[7]", rule_kind="entry", side="long")
        assert _predicate_for_rule_id(spec, fixture) is None

    def test_none_spec_returns_none(self):
        fixture = ConformanceFixture(rule_id="entry[0]", rule_kind="entry", side="long")
        assert _predicate_for_rule_id(None, fixture) is None

    def test_wrong_variant_at_index_returns_none(self):
        # exit[0] points at a StopLossRule, not a SignalExitRule -> no predicate.
        spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
        fixture = ConformanceFixture(rule_id="exit[0]:signal_exit", rule_kind="signal_exit")
        assert _predicate_for_rule_id(spec, fixture) is None


class TestDetailHelpers:
    def test_format_scalar_none_and_float(self):
        assert _format_scalar(None) == "None"
        assert _format_scalar(1.23456) == "1.235"

    def test_enriched_trace_lines_empty_when_no_offending_bars(self):
        fixture = ConformanceFixture(rule_id="entry[0]", rule_kind="entry", side="long")
        assert _enriched_trace_lines(_pred_close_gt_50(), fixture, [], []) == []

    def test_build_conformance_detail_falls_back_without_predicate(self):
        # Unresolvable rule_id -> index-only detail, no "Predicate:" / directive line.
        spec = _spec(entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")])
        fixture = ConformanceFixture(rule_id="bogus[x]", rule_kind="entry", side="long")
        detail = _build_conformance_detail(fixture, spec, [3], [7])
        assert "Predicate:" not in detail
        assert "Fix the on_bar branch" not in detail
        assert "False positives" in detail
        assert "False negatives" in detail
