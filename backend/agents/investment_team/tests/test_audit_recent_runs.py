"""Tests for the audit_recent_runs CLI script.

Covers all 10 acceptance-criteria checks (PASS / FAIL / SKIP per check),
argument parsing, record loading, and end-to-end ``main()`` integration.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

# ---------------------------------------------------------------------------
# Fake job-service infrastructure (same pattern as test_scripts.py)
# ---------------------------------------------------------------------------


class _FakeJobClient:
    def __init__(self, team: str = "test") -> None:
        self.team = team
        self.jobs: List[Dict[str, Any]] = []

    def list_jobs(self) -> List[Dict[str, Any]]:
        return list(self.jobs)


@pytest.fixture
def patched_job_service_client(monkeypatch: pytest.MonkeyPatch):
    instances: Dict[str, _FakeJobClient] = {}

    def _factory(team: str = "test") -> _FakeJobClient:
        existing = instances.get(team)
        if existing is None:
            existing = _FakeJobClient(team=team)
            instances[team] = existing
        return existing

    import job_service_client as jsc_mod

    monkeypatch.setattr(jsc_mod, "JobServiceClient", _factory)
    return instances


# ---------------------------------------------------------------------------
# Synthetic record builder
# ---------------------------------------------------------------------------


def _synthetic_record(**overrides: Any) -> Dict[str, Any]:
    """Minimal record that passes all 10 checks."""
    record: Dict[str, Any] = {
        "lab_record_id": overrides.pop("lab_record_id", "test-001"),
        "created_at": overrides.pop("created_at", "2099-01-15T00:00:00+00:00"),
        "strategy": {
            "entry_rules": [
                {
                    "kind": "entry",
                    "side": "long",
                    "when": {
                        "lhs": {"name": "rsi", "params": {"period": 14}, "source": "close"},
                        "op": "<",
                        "rhs": 30,
                    },
                    "note": "RSI oversold",
                },
            ],
            "exit_rules": [
                {"kind": "stop_loss", "pct": 0.05, "basis": "entry_price", "note": "5% stop"},
                {"kind": "take_profit", "pct": 0.10, "note": "10% take profit"},
            ],
            "target_symbols": ["AAPL", "MSFT"],
            "timeframe": "1d",
            "requires_custom_code": False,
            "risk_limits": {},
        },
        "backtest": {
            "trades": [
                {
                    "symbol": "AAPL",
                    "position_value": 1000,
                    "hold_days": 5,
                    "entry_reason": "compiled_entry:entry[0]",
                    "exit_reason": "engine_exit:stop_loss",
                },
                {
                    "symbol": "MSFT",
                    "position_value": 1200,
                    "hold_days": 8,
                    "entry_reason": "compiled_entry:entry[0]",
                    "exit_reason": "engine_exit:take_profit",
                },
                {
                    "symbol": "AAPL",
                    "position_value": 1100,
                    "hold_days": 3,
                    "entry_reason": "compiled_entry:entry[0]",
                    "exit_reason": "engine_exit:stop_loss",
                },
            ],
            "result": {
                "cost_stress_results": [
                    {"multiplier": 1.0, "sharpe_ratio": 1.5, "annualized_return_pct": 12.0},
                    {"multiplier": 2.0, "sharpe_ratio": 0.8, "annualized_return_pct": 5.0},
                ],
                "regime_results": [
                    {"regime": "bull", "n_obs": 50, "strategy_cumret": 0.15},
                    {"regime": "bear", "n_obs": 30, "strategy_cumret": 0.02},
                ],
                "deflated_sharpe": 0.5,
            },
            "config": {
                "start_date": "2024-01-01",
                "end_date": "2024-01-16",
            },
            "alignment_findings": [],
        },
        "analysis_narrative": "The RSI-based strategy enters when RSI drops below 30.",
        "spec_history": [],
        "rule_implementation_map": [
            {"rule_id": "entry[0]", "code_line_refs": [[10, 20]], "traded_count": 3},
            {"rule_id": "exit:stop_loss[0]", "code_line_refs": [[25, 30]], "traded_count": 2},
            {"rule_id": "exit:take_profit[0]", "code_line_refs": [[35, 40]], "traded_count": 1},
            {"rule_id": "sizing", "code_line_refs": [[45, 50]], "traded_count": 3},
        ],
        "quality_gate_results": [
            {
                "gate_name": "exit_rule_conformance",
                "passed": True,
                "severity": "info",
                "details": "OK",
            },
            {"gate_name": "liquidity_realism", "passed": True, "severity": "info", "details": "OK"},
        ],
    }

    for key, val in overrides.items():
        if "." in key:
            parts = key.split(".")
            target = record
            for p in parts[:-1]:
                target = target.setdefault(p, {})
            target[parts[-1]] = val
        else:
            record[key] = val

    return record


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestParseSince:
    def test_duration(self) -> None:
        from investment_team.scripts.audit_recent_runs import _parse_since

        result = _parse_since("30d")
        assert isinstance(result, datetime)
        assert result.tzinfo is not None

    def test_iso_date(self) -> None:
        from investment_team.scripts.audit_recent_runs import _parse_since

        result = _parse_since("2024-06-01")
        assert result.year == 2024
        assert result.month == 6
        assert result.day == 1

    def test_invalid(self) -> None:
        from investment_team.scripts.audit_recent_runs import _parse_since

        with pytest.raises(argparse.ArgumentTypeError):
            _parse_since("not-a-date")


class TestPositiveInt:
    def test_valid(self) -> None:
        from investment_team.scripts.audit_recent_runs import _positive_int

        assert _positive_int("5") == 5

    def test_zero_rejected(self) -> None:
        from investment_team.scripts.audit_recent_runs import _positive_int

        with pytest.raises(argparse.ArgumentTypeError):
            _positive_int("0")

    def test_negative_rejected(self) -> None:
        from investment_team.scripts.audit_recent_runs import _positive_int

        with pytest.raises(argparse.ArgumentTypeError):
            _positive_int("-1")

    def test_non_integer_rejected(self) -> None:
        from investment_team.scripts.audit_recent_runs import _positive_int

        with pytest.raises(argparse.ArgumentTypeError):
            _positive_int("abc")


class TestParseRate:
    def test_valid(self) -> None:
        from investment_team.scripts.audit_recent_runs import _parse_rate

        assert _parse_rate("0.8") == 0.8

    def test_out_of_range(self) -> None:
        from investment_team.scripts.audit_recent_runs import _parse_rate

        with pytest.raises(argparse.ArgumentTypeError):
            _parse_rate("1.5")

    def test_invalid(self) -> None:
        from investment_team.scripts.audit_recent_runs import _parse_rate

        with pytest.raises(argparse.ArgumentTypeError):
            _parse_rate("abc")


# ---------------------------------------------------------------------------
# Check 1: Spec stability
# ---------------------------------------------------------------------------


class TestCheckSpecStability:
    def test_pass_empty_history(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_spec_stability

        r = check_spec_stability(_synthetic_record())
        assert r.status == "PASS"

    def test_pass_design_phase_only(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_spec_stability

        rec = _synthetic_record(
            spec_history=[
                {"phase": "design", "diff": "+entry_rules: ...", "reason": "init"},
            ]
        )
        assert check_spec_stability(rec).status == "PASS"

    def test_fail_post_design_non_risk(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_spec_stability

        rec = _synthetic_record(
            spec_history=[
                {
                    "phase": "synthesis",
                    "diff": '- "entry_rules": []\n+ "entry_rules": [{"kind": "entry"}]',
                    "reason": "fix",
                },
            ]
        )
        result = check_spec_stability(rec)
        assert result.status == "FAIL"
        assert "entry_rules" in result.details

    def test_pass_risk_limits_tightening(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_spec_stability

        rec = _synthetic_record(
            spec_history=[
                {
                    "phase": "synthesis",
                    "diff": '- "max_drawdown_pct": 0.20\n+ "max_drawdown_pct": 0.15',
                    "reason": "tighten risk",
                },
            ]
        )
        assert check_spec_stability(rec).status == "PASS"

    def test_fail_loosened_risk_limit(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_spec_stability

        rec = _synthetic_record(
            spec_history=[
                {
                    "phase": "synthesis",
                    "diff": '- "max_drawdown_pct": 0.15\n+ "max_drawdown_pct": 0.25',
                    "reason": "relax risk",
                },
            ]
        )
        result = check_spec_stability(rec)
        assert result.status == "FAIL"
        assert "loosened" in result.details
        assert "max_drawdown_pct" in result.details

    def test_fail_immutable_risk_limit(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_spec_stability

        rec = _synthetic_record(
            spec_history=[
                {
                    "phase": "verification",
                    "diff": '- "vol_lookback_days": 20\n+ "vol_lookback_days": 30',
                    "reason": "adjust",
                },
            ]
        )
        result = check_spec_stability(rec)
        assert result.status == "FAIL"
        assert "immutable" in result.details

    def test_fail_null_to_value_mutation(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_spec_stability

        rec = _synthetic_record(
            spec_history=[
                {
                    "phase": "synthesis",
                    "diff": '- "target_annual_vol": null\n+ "target_annual_vol": 0.15',
                    "reason": "set vol target",
                },
            ]
        )
        result = check_spec_stability(rec)
        assert result.status == "FAIL"
        assert "structurally changed" in result.details

    def test_skip_missing(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_spec_stability

        rec = _synthetic_record()
        del rec["spec_history"]
        assert check_spec_stability(rec).status == "SKIP"


# ---------------------------------------------------------------------------
# Check 2: Rule implementation
# ---------------------------------------------------------------------------


class TestCheckRuleImplementation:
    def test_pass(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_rule_implementation

        assert check_rule_implementation(_synthetic_record()).status == "PASS"

    def test_pass_empty_code_line_refs(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_rule_implementation

        rec = _synthetic_record(
            rule_implementation_map=[
                {"rule_id": "entry[0]", "code_line_refs": [], "traded_count": 3},
                {"rule_id": "exit:stop_loss[0]", "code_line_refs": [[25, 30]], "traded_count": 2},
                {"rule_id": "exit:take_profit[0]", "code_line_refs": [[35, 40]], "traded_count": 1},
                {"rule_id": "sizing", "code_line_refs": [[45, 50]], "traded_count": 3},
            ]
        )
        assert check_rule_implementation(rec).status == "PASS"

    def test_fail_missing_rule_in_map(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_rule_implementation

        rec = _synthetic_record(
            rule_implementation_map=[
                {"rule_id": "exit:stop_loss[0]", "code_line_refs": [[25, 30]], "traded_count": 2},
                {"rule_id": "exit:take_profit[0]", "code_line_refs": [[35, 40]], "traded_count": 1},
                {"rule_id": "sizing", "code_line_refs": [[45, 50]], "traded_count": 3},
            ]
        )
        result = check_rule_implementation(rec)
        assert result.status == "FAIL"
        assert "entry[0]" in result.details

    def test_skip_custom_code(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_rule_implementation

        rec = _synthetic_record()
        rec["strategy"]["requires_custom_code"] = True
        assert check_rule_implementation(rec).status == "SKIP"

    def test_fail_empty_rim_with_rules(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_rule_implementation

        rec = _synthetic_record(rule_implementation_map=[])
        result = check_rule_implementation(rec)
        assert result.status == "FAIL"
        assert "empty but spec has rules" in result.details

    def test_skip_empty_rim_no_rules(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_rule_implementation

        rec = _synthetic_record(rule_implementation_map=[])
        rec["strategy"]["entry_rules"] = []
        rec["strategy"]["exit_rules"] = []
        assert check_rule_implementation(rec).status == "SKIP"


# ---------------------------------------------------------------------------
# Check 3: Universe fidelity
# ---------------------------------------------------------------------------


class TestCheckUniverseFidelity:
    def test_pass(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_universe_fidelity

        assert check_universe_fidelity(_synthetic_record()).status == "PASS"

    def test_fail_off_spec(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_universe_fidelity

        rec = _synthetic_record()
        rec["backtest"]["trades"].append({"symbol": "TSLA", "position_value": 500, "hold_days": 2})
        result = check_universe_fidelity(rec)
        assert result.status == "FAIL"
        assert "TSLA" in result.details

    def test_skip_empty_targets(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_universe_fidelity

        rec = _synthetic_record()
        rec["strategy"]["target_symbols"] = []
        assert check_universe_fidelity(rec).status == "SKIP"

    def test_case_insensitive(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_universe_fidelity

        rec = _synthetic_record()
        rec["strategy"]["target_symbols"] = ["aapl", "msft"]
        assert check_universe_fidelity(rec).status == "PASS"


# ---------------------------------------------------------------------------
# Check 4: Exit-rule alignment
# ---------------------------------------------------------------------------


class TestCheckExitRuleAlignment:
    def test_pass_gate_results(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_exit_rule_alignment

        assert check_exit_rule_alignment(_synthetic_record()).status == "PASS"

    def test_fail_critical_gate(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_exit_rule_alignment

        rec = _synthetic_record(
            quality_gate_results=[
                {
                    "gate_name": "exit_rule_conformance",
                    "passed": False,
                    "severity": "critical",
                    "details": "stop-loss never fired",
                },
            ]
        )
        result = check_exit_rule_alignment(rec)
        assert result.status == "FAIL"

    def test_pass_warning_only(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_exit_rule_alignment

        rec = _synthetic_record(
            quality_gate_results=[
                {
                    "gate_name": "exit_rule_conformance",
                    "passed": False,
                    "severity": "warning",
                    "details": "minor issue",
                },
            ]
        )
        assert check_exit_rule_alignment(rec).status == "PASS"

    def test_fallback_alignment_findings_pass(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_exit_rule_alignment

        rec = _synthetic_record(quality_gate_results=[])
        rec["backtest"]["alignment_findings"] = [
            {"check_name": "stop_loss", "passed": True, "severity": "info", "details": "OK"},
        ]
        assert check_exit_rule_alignment(rec).status == "PASS"

    def test_fallback_alignment_findings_fail(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_exit_rule_alignment

        rec = _synthetic_record(quality_gate_results=[])
        rec["backtest"]["alignment_findings"] = [
            {
                "check_name": "stop_loss",
                "passed": False,
                "severity": "critical",
                "details": "stop-loss price violated",
            },
        ]
        result = check_exit_rule_alignment(rec)
        assert result.status == "FAIL"

    def test_skip_no_data(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_exit_rule_alignment

        rec = _synthetic_record(quality_gate_results=[])
        rec["backtest"]["alignment_findings"] = []
        assert check_exit_rule_alignment(rec).status == "SKIP"


# ---------------------------------------------------------------------------
# Check 5: Cost robustness
# ---------------------------------------------------------------------------


class TestCheckCostRobustness:
    def test_pass(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_cost_robustness

        assert check_cost_robustness(_synthetic_record()).status == "PASS"

    def test_fail_negative_return(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_cost_robustness

        rec = _synthetic_record()
        rec["backtest"]["result"]["cost_stress_results"] = [
            {"multiplier": 2.0, "sharpe_ratio": -0.1, "annualized_return_pct": -3.5},
        ]
        result = check_cost_robustness(rec)
        assert result.status == "FAIL"
        assert "-3.50%" in result.details

    def test_skip_missing_annualized_return(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_cost_robustness

        rec = _synthetic_record()
        rec["backtest"]["result"]["cost_stress_results"] = [
            {"multiplier": 2.0, "sharpe_ratio": 0.8},
        ]
        assert check_cost_robustness(rec).status == "SKIP"

    def test_skips_malformed_multiplier(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_cost_robustness

        rec = _synthetic_record()
        rec["backtest"]["result"]["cost_stress_results"] = [
            {"multiplier": "bad", "sharpe_ratio": 1.0, "annualized_return_pct": 5.0},
            {"multiplier": 2.0, "sharpe_ratio": 0.8, "annualized_return_pct": 3.0},
        ]
        assert check_cost_robustness(rec).status == "PASS"

    def test_skip_no_results(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_cost_robustness

        rec = _synthetic_record()
        rec["backtest"]["result"]["cost_stress_results"] = None
        assert check_cost_robustness(rec).status == "SKIP"

    def test_skip_no_2x_row(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_cost_robustness

        rec = _synthetic_record()
        rec["backtest"]["result"]["cost_stress_results"] = [
            {"multiplier": 1.0, "sharpe_ratio": 1.5, "annualized_return_pct": 12.0},
        ]
        assert check_cost_robustness(rec).status == "SKIP"


# ---------------------------------------------------------------------------
# Check 6: Regime coverage
# ---------------------------------------------------------------------------


class TestCheckRegimeCoverage:
    def test_pass(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_regime_coverage

        assert check_regime_coverage(_synthetic_record()).status == "PASS"

    def test_fail_negative_cumret(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_regime_coverage

        rec = _synthetic_record()
        rec["backtest"]["result"]["regime_results"] = [
            {"regime": "bull", "n_obs": 50, "strategy_cumret": 0.15},
            {"regime": "bear", "n_obs": 30, "strategy_cumret": -0.08},
        ]
        result = check_regime_coverage(rec)
        assert result.status == "FAIL"
        assert "bear" in result.details

    def test_fail_negative_dsr(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_regime_coverage

        rec = _synthetic_record()
        rec["backtest"]["result"]["deflated_sharpe"] = -0.3
        result = check_regime_coverage(rec)
        assert result.status == "FAIL"
        assert "Deflated Sharpe" in result.details

    def test_fail_missing_dsr(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_regime_coverage

        rec = _synthetic_record()
        del rec["backtest"]["result"]["deflated_sharpe"]
        result = check_regime_coverage(rec)
        assert result.status == "FAIL"
        assert "missing" in result.details

    def test_skip_no_regimes(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_regime_coverage

        rec = _synthetic_record()
        rec["backtest"]["result"]["regime_results"] = None
        assert check_regime_coverage(rec).status == "SKIP"

    def test_pass_zero_obs_regime_ignored(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_regime_coverage

        rec = _synthetic_record()
        rec["backtest"]["result"]["regime_results"] = [
            {"regime": "bull", "n_obs": 50, "strategy_cumret": 0.15},
            {"regime": "crisis", "n_obs": 0, "strategy_cumret": -0.50},
        ]
        assert check_regime_coverage(rec).status == "PASS"


# ---------------------------------------------------------------------------
# Check 7: Narrative fidelity
# ---------------------------------------------------------------------------


class TestCheckNarrativeFidelity:
    def test_pass(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_narrative_fidelity

        assert check_narrative_fidelity(_synthetic_record()).status == "PASS"

    def test_fail_phantom_indicator(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_narrative_fidelity

        rec = _synthetic_record(
            analysis_narrative="The MACD crossover and RSI strategy performed well."
        )
        result = check_narrative_fidelity(rec)
        assert result.status == "FAIL"
        assert "macd" in result.details

    def test_skip_empty_narrative(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_narrative_fidelity

        rec = _synthetic_record(analysis_narrative="")
        assert check_narrative_fidelity(rec).status == "SKIP"

    def test_pass_indicator_in_spec(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_narrative_fidelity

        rec = _synthetic_record(analysis_narrative="The RSI indicator dropped below threshold.")
        assert check_narrative_fidelity(rec).status == "PASS"

    def test_case_insensitive_matching(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_narrative_fidelity

        rec = _synthetic_record(analysis_narrative="The EMA crossover was detected.")
        result = check_narrative_fidelity(rec)
        assert result.status == "FAIL"
        assert "ema" in result.details


# ---------------------------------------------------------------------------
# Check 8: Trade adequacy
# ---------------------------------------------------------------------------


class TestCheckTradeAdequacy:
    def test_pass(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_trade_adequacy

        assert check_trade_adequacy(_synthetic_record()).status == "PASS"

    def test_fail_too_few_trades(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_trade_adequacy

        rec = _synthetic_record()
        rec["backtest"]["trades"] = [
            {"symbol": "AAPL", "position_value": 1000, "hold_days": 5},
        ]
        result = check_trade_adequacy(rec)
        assert result.status == "FAIL"
        assert "n_trades=1" in result.details

    def test_skip_missing_dates(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_trade_adequacy

        rec = _synthetic_record()
        rec["backtest"]["config"] = {}
        assert check_trade_adequacy(rec).status == "SKIP"

    def test_skip_no_trades(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_trade_adequacy

        rec = _synthetic_record()
        rec["backtest"]["trades"] = []
        assert check_trade_adequacy(rec).status == "SKIP"

    def test_accepts_iso_datetime_strings(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_trade_adequacy

        rec = _synthetic_record()
        rec["backtest"]["config"] = {
            "start_date": "2024-01-01T00:00:00+00:00",
            "end_date": "2024-01-16T00:00:00+00:00",
        }
        result = check_trade_adequacy(rec)
        assert result.status == "PASS"

    def test_same_day_intraday_window(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_trade_adequacy

        rec = _synthetic_record()
        rec["backtest"]["config"] = {
            "start_date": "2024-01-15",
            "end_date": "2024-01-15",
        }
        result = check_trade_adequacy(rec)
        assert result.status in ("PASS", "FAIL")
        assert result.status != "SKIP"

    def test_fallback_to_default_hold(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_trade_adequacy

        rec = _synthetic_record()
        rec["backtest"]["trades"] = [
            {"symbol": "AAPL", "position_value": 1000, "hold_days": 0} for _ in range(40)
        ]
        result = check_trade_adequacy(rec)
        assert result.status in ("PASS", "FAIL")


# ---------------------------------------------------------------------------
# Check 9: Liquidity realism
# ---------------------------------------------------------------------------


class TestCheckLiquidityRealism:
    def test_pass(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_liquidity_realism

        assert check_liquidity_realism(_synthetic_record()).status == "PASS"

    def test_fail_critical(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_liquidity_realism

        rec = _synthetic_record(
            quality_gate_results=[
                {
                    "gate_name": "liquidity_realism",
                    "passed": False,
                    "severity": "critical",
                    "details": "Position exceeds 1% ADV",
                },
            ]
        )
        result = check_liquidity_realism(rec)
        assert result.status == "FAIL"

    def test_skip_no_gate(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_liquidity_realism

        rec = _synthetic_record(quality_gate_results=[])
        assert check_liquidity_realism(rec).status == "SKIP"


# ---------------------------------------------------------------------------
# Check 10: No dead-code rules
# ---------------------------------------------------------------------------


class TestCheckNoDeadCodeRules:
    def test_pass(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_no_dead_code_rules

        assert check_no_dead_code_rules(_synthetic_record()).status == "PASS"

    def test_fail_zero_trades(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_no_dead_code_rules

        rec = _synthetic_record(
            rule_implementation_map=[
                {"rule_id": "entry[0]", "code_line_refs": [[10, 20]], "traded_count": 3},
                {"rule_id": "exit:stop_loss[0]", "code_line_refs": [[25, 30]], "traded_count": 0},
                {"rule_id": "sizing", "code_line_refs": [[45, 50]], "traded_count": 3},
            ]
        )
        result = check_no_dead_code_rules(rec)
        assert result.status == "FAIL"
        assert "exit:stop_loss[0]" in result.details

    def test_skip_custom_code(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_no_dead_code_rules

        rec = _synthetic_record()
        rec["strategy"]["requires_custom_code"] = True
        assert check_no_dead_code_rules(rec).status == "SKIP"

    def test_fail_empty_rim_with_rules(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_no_dead_code_rules

        rec = _synthetic_record(rule_implementation_map=[])
        result = check_no_dead_code_rules(rec)
        assert result.status == "FAIL"
        assert "empty but spec has rules" in result.details

    def test_skip_empty_rim_no_rules(self) -> None:
        from investment_team.scripts.audit_recent_runs import check_no_dead_code_rules

        rec = _synthetic_record(rule_implementation_map=[])
        rec["strategy"]["entry_rules"] = []
        rec["strategy"]["exit_rules"] = []
        assert check_no_dead_code_rules(rec).status == "SKIP"


# ---------------------------------------------------------------------------
# Record loading
# ---------------------------------------------------------------------------


class TestParseCreatedAt:
    def test_iso_with_offset(self) -> None:
        from investment_team.scripts.audit_recent_runs import _parse_created_at

        dt = _parse_created_at("2026-01-15T00:00:00+00:00")
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.year == 2026

    def test_iso_with_z(self) -> None:
        from investment_team.scripts.audit_recent_runs import _parse_created_at

        dt = _parse_created_at("2026-01-15T00:00:00Z")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_naive_gets_utc(self) -> None:
        from investment_team.scripts.audit_recent_runs import _parse_created_at

        dt = _parse_created_at("2026-01-15T00:00:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_empty(self) -> None:
        from investment_team.scripts.audit_recent_runs import _parse_created_at

        assert _parse_created_at("") is None

    def test_invalid(self) -> None:
        from investment_team.scripts.audit_recent_runs import _parse_created_at

        assert _parse_created_at("not-a-date") is None


class TestLoadRecords:
    def test_filters_by_since(self) -> None:
        from investment_team.scripts.audit_recent_runs import _load_records

        client = _FakeJobClient()
        client.jobs = [
            {"job_id": "old", "data": {"created_at": "2023-01-01T00:00:00+00:00"}},
            {"job_id": "new", "data": {"created_at": "2026-06-01T00:00:00+00:00"}},
        ]
        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        records = _load_records(client, since, None)
        assert len(records) == 1
        assert records[0]["_job_id"] == "new"

    def test_mixed_timestamp_formats(self) -> None:
        from investment_team.scripts.audit_recent_runs import _load_records

        client = _FakeJobClient()
        client.jobs = [
            {"job_id": "z-fmt", "data": {"created_at": "2026-06-01T00:00:00Z"}},
            {"job_id": "offset-fmt", "data": {"created_at": "2026-06-02T00:00:00+00:00"}},
            {"job_id": "old-z", "data": {"created_at": "2023-01-01T00:00:00Z"}},
        ]
        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        records = _load_records(client, since, None)
        assert len(records) == 2
        assert records[0]["_job_id"] == "offset-fmt"
        assert records[1]["_job_id"] == "z-fmt"

    def test_sample_limits(self) -> None:
        from investment_team.scripts.audit_recent_runs import _load_records

        client = _FakeJobClient()
        client.jobs = [
            {"job_id": f"j-{i}", "data": {"created_at": f"2026-01-{i + 1:02d}T00:00:00+00:00"}}
            for i in range(5)
        ]
        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        records = _load_records(client, since, 2)
        assert len(records) == 2

    def test_skips_missing_job_id(self) -> None:
        from investment_team.scripts.audit_recent_runs import _load_records

        client = _FakeJobClient()
        client.jobs = [{"data": {"created_at": "2026-01-01T00:00:00+00:00"}}]
        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        assert _load_records(client, since, None) == []


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------


class TestMain:
    def test_all_pass(self, capsys: pytest.CaptureFixture[str], patched_job_service_client) -> None:
        from investment_team.scripts.audit_recent_runs import main

        fake = patched_job_service_client.setdefault(
            "investment_strategy_lab_records",
            _FakeJobClient(team="investment_strategy_lab_records"),
        )
        fake.jobs = [{"job_id": "r-1", "data": _synthetic_record()}]

        rc = main(["--since", "1d"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "FAIL" not in out
        assert "OK" in out

    def test_with_failure_exits_1(
        self, capsys: pytest.CaptureFixture[str], patched_job_service_client
    ) -> None:
        from investment_team.scripts.audit_recent_runs import main

        fake = patched_job_service_client.setdefault(
            "investment_strategy_lab_records",
            _FakeJobClient(team="investment_strategy_lab_records"),
        )
        rec = _synthetic_record()
        rec["backtest"]["trades"].append({"symbol": "TSLA", "position_value": 500, "hold_days": 2})
        fake.jobs = [{"job_id": "r-1", "data": rec}]

        rc = main(["--since", "1d", "--min-pass-rate", "1.0"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "BELOW THRESHOLD" in out

    def test_empty_records(
        self, capsys: pytest.CaptureFixture[str], patched_job_service_client
    ) -> None:
        from investment_team.scripts.audit_recent_runs import main

        patched_job_service_client.setdefault(
            "investment_strategy_lab_records",
            _FakeJobClient(team="investment_strategy_lab_records"),
        )

        rc = main(["--since", "1d"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No records found" in out

    def test_sample_arg(
        self, capsys: pytest.CaptureFixture[str], patched_job_service_client
    ) -> None:
        from investment_team.scripts.audit_recent_runs import main

        fake = patched_job_service_client.setdefault(
            "investment_strategy_lab_records",
            _FakeJobClient(team="investment_strategy_lab_records"),
        )
        fake.jobs = [
            {"job_id": f"r-{i}", "data": _synthetic_record(lab_record_id=f"r-{i}")}
            for i in range(5)
        ]

        rc = main(["--since", "1d", "--sample", "2"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Records: 2" in out

    def test_all_skip_exits_1(
        self, capsys: pytest.CaptureFixture[str], patched_job_service_client
    ) -> None:
        from investment_team.scripts.audit_recent_runs import main

        fake = patched_job_service_client.setdefault(
            "investment_strategy_lab_records",
            _FakeJobClient(team="investment_strategy_lab_records"),
        )
        rec: Dict[str, Any] = {
            "lab_record_id": "skip-all",
            "created_at": "2099-01-15T00:00:00+00:00",
            "strategy": {
                "requires_custom_code": True,
                "target_symbols": [],
                "entry_rules": [],
                "exit_rules": [],
            },
            "backtest": {"result": {}, "trades": [], "config": {}, "alignment_findings": []},
            "quality_gate_results": [],
            "rule_implementation_map": [],
            "analysis_narrative": "",
            "spec_history": None,
        }
        fake.jobs = [{"job_id": "r-skip", "data": rec}]

        rc = main(["--since", "1d"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "NO DATA" in out

    def test_no_since_defaults_to_all(
        self, capsys: pytest.CaptureFixture[str], patched_job_service_client
    ) -> None:
        from investment_team.scripts.audit_recent_runs import main

        fake = patched_job_service_client.setdefault(
            "investment_strategy_lab_records",
            _FakeJobClient(team="investment_strategy_lab_records"),
        )
        fake.jobs = [{"job_id": "r-1", "data": _synthetic_record()}]

        rc = main([])
        assert rc == 0


class TestRiskLimitWhitelistSync:
    """The audit script replicates the risk-limit tighten-direction map (it
    avoids a runtime import to stay decoupled). This guard keeps the replica
    byte-for-byte in sync with the source of truth so a new risk-limit field
    (e.g. target_annual_vol) can't be accepted by the orchestrator's refinement
    merge while audits still mis-report it as a non-risk field."""

    def test_audit_replica_matches_source_tighten_direction(self) -> None:
        from investment_team.execution.risk_filter import _RISK_LIMIT_TIGHTEN_DIRECTION
        from investment_team.scripts.audit_recent_runs import _RISK_LIMIT_TIGHTEN_DIR

        assert _RISK_LIMIT_TIGHTEN_DIR == _RISK_LIMIT_TIGHTEN_DIRECTION

    def test_audit_keys_include_max_position_pct(self) -> None:
        from investment_team.scripts.audit_recent_runs import _RISK_LIMIT_KEYS

        assert "max_position_pct" in _RISK_LIMIT_KEYS
