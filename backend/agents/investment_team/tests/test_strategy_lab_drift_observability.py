"""Tests for spec/code drift observability: revision histories on StrategyLabRecord.

Covers the ``_DriftCollector`` accumulator, ``SpecRevision`` / ``CodeRevision``
models, gate-timeline conversion, rule-implementation map, and the full
end-to-end flow through ``run_cycle``.
"""

from __future__ import annotations

from typing import Any, Dict

from investment_team.models import (
    BacktestConfig,
    CodeRevision,
    GateEvent,
    SpecRevision,
    StrategyLabRecord,
    StrategySpec,
)
from investment_team.strategy_lab._orchestrator_helpers import (
    _build_rule_implementation_map,
    _DriftCollector,
)
from investment_team.strategy_lab.alignment_findings import AlignmentFinding
from investment_team.strategy_lab.phases import hash_spec
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_spec(**overrides: Any) -> StrategySpec:
    defaults: Dict[str, Any] = {
        "strategy_id": "strat-test",
        "authored_by": "test",
        "asset_class": "stocks",
        "hypothesis": "test hypothesis",
        "signal_definition": "RSI(14)",
        "timeframe": "1d",
        "entry_rules": [
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op="<",
                    rhs=30,
                ),
            ),
        ],
        "exit_rules": [
            StopLossRule(pct=0.02),
            SignalExitRule(
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op=">",
                    rhs=70,
                ),
            ),
        ],
        "target_symbols": ["AAPL"],
        "requires_custom_code": False,
    }
    defaults.update(overrides)
    return StrategySpec(**defaults)


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )


# ---------------------------------------------------------------------------
# _DriftCollector unit tests
# ---------------------------------------------------------------------------


class TestDriftCollector:
    def test_record_spec_change_appends_revision(self):
        collector = _DriftCollector()
        before = _minimal_spec(hypothesis="before")
        after = _minimal_spec(hypothesis="after")
        collector.record_spec_change(
            phase="design_review",
            agent="DesignAgent",
            before_spec=before,
            after_spec=after,
            reason="revised hypothesis",
        )
        assert len(collector.spec_history) == 1
        rev = collector.spec_history[0]
        assert isinstance(rev, SpecRevision)
        assert rev.phase == "design_review"
        assert rev.agent == "DesignAgent"
        assert rev.before_hash != rev.after_hash
        assert "before" in rev.diff or "after" in rev.diff
        assert rev.reason == "revised hypothesis"

    def test_record_spec_change_noop_skipped(self):
        collector = _DriftCollector()
        spec = _minimal_spec()
        collector.record_spec_change(
            phase="synthesis",
            agent="RefinementAgent",
            before_spec=spec,
            after_spec=spec,
            reason="should not appear",
        )
        assert len(collector.spec_history) == 0

    def test_record_code_change_appends_revision(self):
        collector = _DriftCollector()
        collector.record_code_change(
            phase="synthesis",
            agent="RefinementAgent",
            before_code="def run(): pass",
            after_code="def run(): return 42",
            reason="fixed return",
        )
        assert len(collector.code_history) == 1
        rev = collector.code_history[0]
        assert isinstance(rev, CodeRevision)
        assert rev.before_hash != rev.after_hash
        assert "42" in rev.diff

    def test_record_code_change_noop_skipped(self):
        collector = _DriftCollector()
        collector.record_code_change(
            phase="synthesis",
            agent="RefinementAgent",
            before_code="x = 1",
            after_code="x = 1",
            reason="should not appear",
        )
        assert len(collector.code_history) == 0

    def test_record_gate_appends_event(self):
        collector = _DriftCollector()
        collector.record_gate(
            phase="verification",
            gate_name="acceptance",
            passed=True,
            severity="info",
            details="accepted",
        )
        assert len(collector.gate_timeline) == 1
        ev = collector.gate_timeline[0]
        assert isinstance(ev, GateEvent)
        assert ev.passed is True

    def test_gate_failures_carried_on_revision(self):
        collector = _DriftCollector()
        before = _minimal_spec(hypothesis="before")
        after = _minimal_spec(hypothesis="after")
        collector.record_spec_change(
            phase="synthesis",
            agent="RefinementAgent",
            before_spec=before,
            after_spec=after,
            reason="tightened limits",
            gate_failures=["code_conformance: failed"],
        )
        assert collector.spec_history[0].gate_failures == ["code_conformance: failed"]

    def test_hash_stability(self):
        spec = _minimal_spec()
        h1 = hash_spec(spec)
        h2 = hash_spec(spec)
        assert h1 == h2
        assert len(h1) == 64


# ---------------------------------------------------------------------------
# Gate timeline conversion
# ---------------------------------------------------------------------------


class TestGateTimeline:
    def test_gate_results_to_timeline(self):
        gates = [
            QualityGateResult(
                gate_name="spec_readiness",
                passed=True,
                details="ok",
                severity="info",
                phase="design",
                evaluated_at="2024-01-01T00:00:01+00:00",
            ),
            QualityGateResult(
                gate_name="code_conformance",
                passed=False,
                details="missing import",
                severity="critical",
                phase="synthesis",
                evaluated_at="2024-01-01T00:00:05+00:00",
            ),
        ]
        timeline = [
            GateEvent(
                phase=g.phase,
                gate_name=g.gate_name,
                passed=g.passed,
                severity=g.severity,
                details=g.details,
                timestamp=g.evaluated_at,
            )
            for g in gates
        ]
        assert len(timeline) == 2
        assert timeline[0].passed is True
        assert timeline[1].passed is False
        assert timeline[1].gate_name == "code_conformance"

    def test_per_gate_timestamps_preserved(self):
        """Each gate event carries its own evaluation timestamp."""
        gates = [
            QualityGateResult(
                gate_name="spec_readiness",
                passed=True,
                details="ok",
                severity="info",
                phase="design",
                evaluated_at="2024-01-01T00:00:01+00:00",
            ),
            QualityGateResult(
                gate_name="alignment",
                passed=False,
                details="misaligned",
                severity="critical",
                phase="verification",
                evaluated_at="2024-01-01T00:05:00+00:00",
            ),
        ]
        timeline = [
            GateEvent(
                phase=g.phase,
                gate_name=g.gate_name,
                passed=g.passed,
                severity=g.severity,
                details=g.details,
                timestamp=g.evaluated_at,
            )
            for g in gates
        ]
        assert timeline[0].timestamp == "2024-01-01T00:00:01+00:00"
        assert timeline[1].timestamp == "2024-01-01T00:05:00+00:00"
        assert timeline[0].timestamp != timeline[1].timestamp


# ---------------------------------------------------------------------------
# Rule implementation map
# ---------------------------------------------------------------------------


class TestRuleImplementationMap:
    def test_builds_from_findings(self):
        spec = _minimal_spec()
        findings = [
            AlignmentFinding(
                trade_num=1, rule_id="entry[0]", check_name="entry_signal",
                passed=True, computed_value=25.0, expected_value=30.0,
            ),
            AlignmentFinding(
                trade_num=2, rule_id="entry[0]", check_name="entry_signal",
                passed=True, computed_value=22.0, expected_value=30.0,
            ),
            AlignmentFinding(trade_num=1, rule_id="exit:stop_loss[0]", check_name="stop_loss", passed=False),
            AlignmentFinding(
                trade_num=1, rule_id="sizing", check_name="sizing",
                passed=True, computed_value=100.0, expected_value=100.0,
            ),
        ]
        result = _build_rule_implementation_map(spec, findings, "def run(): pass")
        assert isinstance(result, list)
        rule_ids = {r.rule_id for r in result}
        assert "entry[0]" in rule_ids
        assert "exit:stop_loss[0]" in rule_ids
        assert "sizing" in rule_ids

        entry_map = next(r for r in result if r.rule_id == "entry[0]")
        assert entry_map.traded_count == 2

        sizing_map = next(r for r in result if r.rule_id == "sizing")
        assert sizing_map.traded_count == 1

    def test_empty_findings(self):
        spec = _minimal_spec()
        result = _build_rule_implementation_map(spec, [], "")
        assert isinstance(result, list)
        assert all(r.traded_count == 0 for r in result)

    def test_code_line_refs_best_effort(self):
        code = """
def check_entry0(data):
    return data['rsi'] < 30

def check_exit_stop_loss(data):
    return data['loss'] > 2.0
"""
        spec = _minimal_spec()
        result = _build_rule_implementation_map(spec, [], code)
        entry_map = next((r for r in result if r.rule_id == "entry[0]"), None)
        assert entry_map is not None
        assert isinstance(entry_map.code_line_refs, list)

    def test_unparseable_code(self):
        result = _build_rule_implementation_map(_minimal_spec(), [], "def broken(")
        assert isinstance(result, list)
        assert all(r.code_line_refs == [] for r in result)

    def test_indexed_signal_exit_ids_match_directly(self):
        """Indexed IDs like exit:signal_exit[0] match the per-instance canonical key."""
        spec = _minimal_spec()
        findings = [
            AlignmentFinding(
                trade_num=1, rule_id="exit:signal_exit[0]", check_name="signal_exit",
                passed=True, severity="info", computed_value=75.0, expected_value=70.0,
            ),
            AlignmentFinding(
                trade_num=2, rule_id="exit:signal_exit[0]", check_name="signal_exit",
                passed=True, severity="info", computed_value=80.0, expected_value=70.0,
            ),
        ]
        result = _build_rule_implementation_map(spec, findings, "def run(): pass")
        sig_map = next(r for r in result if r.rule_id == "exit:signal_exit[0]")
        assert sig_map.traded_count == 2

    def test_suffixed_stop_loss_ids_normalised(self):
        """Suffixed IDs like exit:stop_loss:trailing normalise to exit:stop_loss[0]."""
        spec = _minimal_spec()
        findings = [
            AlignmentFinding(
                trade_num=1, rule_id="exit:stop_loss:trailing", check_name="stop_loss",
                passed=True, computed_value=-1.5, expected_value=-2.0,
            ),
        ]
        result = _build_rule_implementation_map(spec, findings, "def run(): pass")
        sl_map = next(r for r in result if r.rule_id == "exit:stop_loss[0]")
        assert sl_map.traded_count == 1

    def test_traded_count_is_distinct_trades(self):
        """Same trade with multiple suffixed findings counts once."""
        spec = _minimal_spec()
        findings = [
            AlignmentFinding(
                trade_num=1, rule_id="exit:stop_loss:trailing_high", check_name="stop_loss",
                passed=True, computed_value=-1.0, expected_value=-2.0,
            ),
            AlignmentFinding(
                trade_num=1, rule_id="exit:stop_loss:trailing_low", check_name="stop_loss",
                passed=True, computed_value=-0.5, expected_value=-2.0,
            ),
            AlignmentFinding(
                trade_num=2, rule_id="exit:stop_loss:entry_price", check_name="stop_loss",
                passed=True, computed_value=-1.8, expected_value=-2.0,
            ),
        ]
        result = _build_rule_implementation_map(spec, findings, "def run(): pass")
        sl_map = next(r for r in result if r.rule_id == "exit:stop_loss[0]")
        assert sl_map.traded_count == 2

    def test_non_spec_finding_ids_excluded(self):
        """Diagnostic finding IDs not in the spec are excluded from the map."""
        spec = _minimal_spec()
        findings = [
            AlignmentFinding(trade_num=1, rule_id="universe", check_name="universe_check", passed=True),
            AlignmentFinding(trade_num=1, rule_id="exit:time_stop", check_name="time_stop", passed=True),
        ]
        result = _build_rule_implementation_map(spec, findings, "def run(): pass")
        rule_ids = {r.rule_id for r in result}
        assert "universe" not in rule_ids
        assert "exit:time_stop" not in rule_ids

    def test_info_skip_findings_not_counted(self):
        """N/A skip findings (passed=True but no computed_value) don't inflate traded_count."""
        spec = _minimal_spec()
        findings = [
            AlignmentFinding(
                trade_num=1, rule_id="exit:signal_exit", check_name="signal_exit",
                passed=True, severity="info",
                details="Trade #1 closed via engine attribution; signal-exit check N/A.",
            ),
        ]
        result = _build_rule_implementation_map(spec, findings, "def run(): pass")
        sig_map = next(r for r in result if r.rule_id == "exit:signal_exit[0]")
        assert sig_map.traded_count == 0

    def test_per_instance_exit_rule_ids(self):
        """Multiple exit rules of the same kind get separate map entries."""
        spec = _minimal_spec(
            exit_rules=[
                SignalExitRule(
                    when=Predicate(
                        lhs=IndicatorRef(name="rsi", params={"period": 14}),
                        op=">",
                        rhs=70,
                    ),
                ),
                SignalExitRule(
                    when=Predicate(
                        lhs=IndicatorRef(name="rsi", params={"period": 14}),
                        op="<",
                        rhs=20,
                    ),
                ),
            ],
        )
        findings = [
            AlignmentFinding(
                trade_num=1, rule_id="exit:signal_exit[0]", check_name="signal_exit",
                passed=True, severity="info", computed_value=75.0, expected_value=70.0,
            ),
        ]
        result = _build_rule_implementation_map(spec, findings, "def run(): pass")
        rule_ids = [r.rule_id for r in result]
        assert "exit:signal_exit[0]" in rule_ids
        assert "exit:signal_exit[1]" in rule_ids
        m0 = next(r for r in result if r.rule_id == "exit:signal_exit[0]")
        m1 = next(r for r in result if r.rule_id == "exit:signal_exit[1]")
        assert m0.traded_count == 1
        assert m1.traded_count == 0


# ---------------------------------------------------------------------------
# StrategyLabRecord with drift fields
# ---------------------------------------------------------------------------


class TestStrategyLabRecordDriftFields:
    @staticmethod
    def _backtest():
        from investment_team.models import BacktestRecord
        from investment_team.trade_simulator import compute_metrics

        cfg = _config()
        result = compute_metrics([], cfg.initial_capital, cfg.start_date, cfg.end_date)
        return BacktestRecord(
            backtest_id="bt-test",
            strategy_id="strat-test",
            strategy=_minimal_spec(),
            config=cfg,
            submitted_by="test",
            submitted_at="2024-01-01",
            completed_at="2024-01-01",
            status="completed",
            result=result,
            trades=[],
        )

    def test_empty_defaults(self):
        record = StrategyLabRecord(
            lab_record_id="lab-test",
            strategy=_minimal_spec(),
            backtest=self._backtest(),
            is_winning=False,
            strategy_rationale="test",
            analysis_narrative="test",
            created_at="2024-01-01",
        )
        assert record.spec_history == []
        assert record.code_history == []
        assert record.gate_timeline == []
        assert record.rule_implementation_map == []

    def test_populated_fields_serialize(self):
        rev = SpecRevision(
            phase="design_review",
            agent="DesignAgent",
            timestamp="2024-01-01T00:00:00+00:00",
            before_hash="a" * 64,
            after_hash="b" * 64,
            diff="--- a\n+++ b\n",
            reason="revised",
        )
        record = StrategyLabRecord(
            lab_record_id="lab-test",
            strategy=_minimal_spec(),
            backtest=self._backtest(),
            is_winning=False,
            strategy_rationale="test",
            analysis_narrative="test",
            created_at="2024-01-01",
            spec_history=[rev],
        )
        dumped = record.model_dump()
        assert len(dumped["spec_history"]) == 1
        assert dumped["spec_history"][0]["phase"] == "design_review"
