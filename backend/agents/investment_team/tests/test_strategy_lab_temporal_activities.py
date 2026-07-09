"""Unit tests for ``strategy_lab.temporal.activities`` — the fine-grained,
per-side-effect Temporal activities that wrap the Strategy Lab's LLM calls,
sandboxed backtest execution, market-data fetches, and persistence writes.

Mirrors the mock-at-the-boundary style used across the codebase's other
Temporal integrations (e.g. ``market_research_team/tests/test_temporal_activity.py``):
each ``@activity.defn``-decorated function is called directly as a plain
Python function (no Temporal test harness), with the underlying agent-class
method monkeypatched so the test asserts (a) the activity reconstructs the
right Pydantic types from its JSON-shaped input, (b) it calls the *real*
class/method rather than duplicating its logic, and (c) failures map to the
correct ``ApplicationError`` / ``non_retryable`` outcome.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from temporalio.exceptions import ApplicationError

from investment_team.strategy_lab.temporal import activities as act

# ---------------------------------------------------------------------------
# Fixture builders — minimal valid JSON-shaped payloads for the models each
# activity reconstructs.
# ---------------------------------------------------------------------------


def _spec_dict(**overrides: Any) -> Dict[str, Any]:
    base = {
        "strategy_id": "strat-1",
        "authored_by": "DesignAgent",
        "asset_class": "stocks",
        "hypothesis": "test hypothesis",
        "signal_definition": "test signal",
        "timeframe": "1d",
    }
    base.update(overrides)
    return base


def _backtest_config_dict(**overrides: Any) -> Dict[str, Any]:
    base = {"start_date": "2023-01-01", "end_date": "2023-12-31"}
    base.update(overrides)
    return base


def _backtest_result_dict(**overrides: Any) -> Dict[str, Any]:
    base = {
        "total_return_pct": 10.0,
        "annualized_return_pct": 10.0,
        "volatility_pct": 5.0,
        "sharpe_ratio": 1.2,
        "max_drawdown_pct": -5.0,
        "win_rate_pct": 55.0,
        "profit_factor": 1.5,
        "sortino_ratio": 1.5,
        "calmar_ratio": 2.0,
        "deflated_sharpe": 0.9,
    }
    base.update(overrides)
    return base


def _trade_record_dict(**overrides: Any) -> Dict[str, Any]:
    base = {
        "trade_num": 1,
        "entry_date": "2023-01-05",
        "exit_date": "2023-01-10",
        "symbol": "AAPL",
        "side": "long",
        "entry_price": 100.0,
        "exit_price": 105.0,
        "shares": 10.0,
        "position_value": 1000.0,
        "gross_pnl": 50.0,
        "net_pnl": 48.0,
        "return_pct": 5.0,
        "hold_days": 5,
        "outcome": "win",
        "cumulative_pnl": 48.0,
    }
    base.update(overrides)
    return base


def _diagnostics_dict(**overrides: Any) -> Dict[str, Any]:
    base = {"zero_trade_category": "NO_ORDERS_EMITTED", "summary": "no orders"}
    base.update(overrides)
    return base


def _bar_dict(date: str = "2023-01-01", **overrides: Any) -> Dict[str, Any]:
    base = {
        "date": date,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1000.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _map_exception_to_application_error
# ---------------------------------------------------------------------------


def test_map_exception_fatal_llm_error_is_non_retryable():
    from investment_team.strategy_lab.exceptions import StrategyLabLLMError

    exc = StrategyLabLLMError("bad request", outcome="fatal")
    mapped = act._map_exception_to_application_error(exc)
    assert isinstance(mapped, ApplicationError)
    assert mapped.non_retryable is True
    assert mapped.type == "fatal"


@pytest.mark.parametrize("outcome", ["exhausted", "budget_exhausted"])
def test_map_exception_non_fatal_llm_error_is_retryable(outcome):
    from investment_team.strategy_lab.exceptions import StrategyLabLLMError

    exc = StrategyLabLLMError("timed out", outcome=outcome)
    mapped = act._map_exception_to_application_error(exc)
    assert mapped.non_retryable is False
    assert mapped.type == outcome


def test_map_exception_generic_exception_is_non_retryable():
    mapped = act._map_exception_to_application_error(ValueError("bad json"))
    assert mapped.non_retryable is True
    assert mapped.type == "ValueError"


# ---------------------------------------------------------------------------
# Design phase
# ---------------------------------------------------------------------------


def test_design_generate_activity_reuses_design_agent(monkeypatch):
    from investment_team.strategy_lab.agents.design import DesignAgent

    captured = {}

    def _fake_run(
        self,
        prior_records,
        signal_brief=None,
        convergence_directives=None,
        exclude_asset_classes=None,
        regime_summary=None,
    ):
        captured["prior_records"] = prior_records
        captured["signal_brief"] = signal_brief
        return ({"asset_class": "stocks", "hypothesis": "h"}, "rationale text")

    monkeypatch.setattr(DesignAgent, "run", _fake_run)

    result = act.design_generate_activity(prior_records=[], signal_brief=None)

    assert result == {
        "strategy_dict": {"asset_class": "stocks", "hypothesis": "h"},
        "rationale": "rationale text",
    }
    assert captured["prior_records"] == []


def test_design_generate_activity_maps_llm_failure(monkeypatch):
    from investment_team.strategy_lab.agents.design import DesignAgent
    from investment_team.strategy_lab.exceptions import StrategyLabLLMError

    def _raise(self, *a, **kw):
        raise StrategyLabLLMError("down", outcome="fatal")

    monkeypatch.setattr(DesignAgent, "run", _raise)

    with pytest.raises(ApplicationError) as exc_info:
        act.design_generate_activity(prior_records=[])
    assert exc_info.value.non_retryable is True


def test_design_revise_activity_reuses_design_agent(monkeypatch):
    from investment_team.strategy_lab.agents.design import DesignAgent

    def _fake_revise(self, prior_spec, critique, prior_critiques=None, regression_notice=""):
        assert prior_spec.strategy_id == "strat-1"
        assert critique.ready is False
        return ({"asset_class": "stocks"}, "revised rationale")

    monkeypatch.setattr(DesignAgent, "revise", _fake_revise)

    result = act.design_revise_activity(
        prior_spec=_spec_dict(),
        critique={"ready": False, "issues": []},
    )
    assert result["rationale"] == "revised rationale"


def test_design_review_activity_reuses_design_review_agent(monkeypatch):
    from investment_team.strategy_lab.agents.design_review import DesignReviewAgent, SpecCritique

    def _fake_run(self, spec, readiness_results=None, prior_critiques=None):
        assert spec.strategy_id == "strat-1"
        return SpecCritique(ready=True, rationale="looks good")

    monkeypatch.setattr(DesignReviewAgent, "run", _fake_run)

    result = act.design_review_activity(spec=_spec_dict())
    assert result["ready"] is True
    assert result["rationale"] == "looks good"


# ---------------------------------------------------------------------------
# Code synthesis / refinement
# ---------------------------------------------------------------------------


def test_code_synthesis_activity_reuses_code_synthesis_agent(monkeypatch):
    from investment_team.strategy_lab.agents.code_synthesis import CodeSynthesisAgent

    monkeypatch.setattr(CodeSynthesisAgent, "run", lambda self, spec: "def on_bar(): pass")

    result = act.code_synthesis_activity(spec=_spec_dict())
    assert result == {"code": "def on_bar(): pass"}


def test_code_synthesis_activity_maps_code_synthesis_error(monkeypatch):
    from investment_team.strategy_lab.agents.code_synthesis import (
        CodeSynthesisAgent,
        CodeSynthesisError,
    )

    def _raise(self, spec):
        raise CodeSynthesisError("empty response")

    monkeypatch.setattr(CodeSynthesisAgent, "run", _raise)

    with pytest.raises(ApplicationError) as exc_info:
        act.code_synthesis_activity(spec=_spec_dict())
    assert exc_info.value.non_retryable is True


def test_refinement_activity_reuses_refinement_agent(monkeypatch):
    from investment_team.strategy_lab.agents.refinement import RefinementAgent

    def _fake_run(
        self, spec, code, failure_phase, failure_details, metrics=None, prior_attempts=None
    ):
        assert failure_phase == "execution"
        assert metrics.sharpe_ratio == 1.2
        return ({"changes_made": "fixed it"}, "new code")

    monkeypatch.setattr(RefinementAgent, "run", _fake_run)

    result = act.refinement_activity(
        spec=_spec_dict(),
        code="old code",
        failure_phase="execution",
        failure_details="crashed",
        metrics=_backtest_result_dict(),
    )
    assert result == {"updated_fields": {"changes_made": "fixed it"}, "updated_code": "new code"}


# ---------------------------------------------------------------------------
# Trade alignment
# ---------------------------------------------------------------------------


def test_alignment_near_miss_activity_reuses_alignment_agent(monkeypatch):
    from investment_team.strategy_lab.agents.alignment import TradeAlignmentAgent
    from investment_team.strategy_lab.alignment_findings import NearMissVerdict

    def _fake_adjudicate(
        self, *, rule_id, predicate_repr, computed_value, threshold, symbol, entry_date
    ):
        assert rule_id == "entry[0]"
        return NearMissVerdict(legitimate=True, rationale="within tolerance")

    monkeypatch.setattr(TradeAlignmentAgent, "adjudicate_near_miss", _fake_adjudicate)

    result = act.alignment_near_miss_activity(
        rule_id="entry[0]",
        predicate_repr="close > sma(20)",
        computed_value=100.4,
        threshold=100.5,
        symbol="AAPL",
        entry_date="2023-01-05",
    )
    assert result == {"legitimate": True, "rationale": "within tolerance"}


def test_alignment_propose_fix_activity_reuses_alignment_agent(monkeypatch):
    from investment_team.strategy_lab.agents.alignment import (
        TradeAlignmentAgent,
        TradeAlignmentReport,
    )

    def _fake_propose(self, *, spec, code, findings, prior_attempts=None):
        assert spec.strategy_id == "strat-1"
        assert len(findings) == 1
        return TradeAlignmentReport(aligned=False, proposed_code="fixed code")

    monkeypatch.setattr(TradeAlignmentAgent, "propose_code_fix", _fake_propose)

    finding = {
        "trade_num": 1,
        "check_name": "entry_signal",
        "passed": False,
        "severity": "critical",
        "details": "entry predicate never fired",
    }
    result = act.alignment_propose_fix_activity(spec=_spec_dict(), code="code", findings=[finding])
    assert result["aligned"] is False
    assert result["proposed_code"] == "fixed code"


# ---------------------------------------------------------------------------
# Analysis / zero-trade repair
# ---------------------------------------------------------------------------


def test_analysis_activity_reuses_analysis_agent(monkeypatch):
    from investment_team.strategy_lab.agents.analysis import AnalysisAgent

    def _fake_run(
        self,
        spec,
        metrics,
        trades,
        rationale,
        on_sub_phase=None,
        is_winning=None,
        alignment_report=None,
        robustness_caveats=None,
    ):
        assert len(trades) == 1
        assert is_winning is True
        return "polished narrative"

    monkeypatch.setattr(AnalysisAgent, "run", _fake_run)

    result = act.analysis_activity(
        spec=_spec_dict(),
        metrics=_backtest_result_dict(),
        trades=[_trade_record_dict()],
        rationale="why",
        is_winning=True,
    )
    assert result == {"narrative": "polished narrative"}


def test_zero_trade_repair_activity_reuses_repair_agent(monkeypatch):
    from investment_team.strategy_lab.agents.zero_trade_repair import (
        ZeroTradeRepairAgent,
        ZeroTradeRepairReport,
    )

    def _fake_run(self, spec, code, diagnostics, prior_attempts=None, *, coverage_report=None):
        assert diagnostics.zero_trade_category == "NO_ORDERS_EMITTED"
        return ZeroTradeRepairReport(root_cause_category="NO_ORDERS_EMITTED", proposed_code="fixed")

    monkeypatch.setattr(ZeroTradeRepairAgent, "run", _fake_run)

    result = act.zero_trade_repair_activity(
        spec=_spec_dict(), code="code", diagnostics=_diagnostics_dict()
    )
    assert result["proposed_code"] == "fixed"


# ---------------------------------------------------------------------------
# Sandboxed backtest execution
# ---------------------------------------------------------------------------


def test_run_strategy_code_activity_reuses_run_strategy_code(monkeypatch):
    from investment_team.trading_service.modes import sandbox_compat

    captured = {}

    def _fake_run_strategy_code(
        strategy_code, market_data, config, *, strategy=None, coverage_probe_mode=False
    ):
        captured["market_data_keys"] = list(market_data.keys())
        return sandbox_compat.StrategyRunResult(
            success=True, stdout="ok", execution_time_seconds=1.0
        )

    monkeypatch.setattr(sandbox_compat, "run_strategy_code", _fake_run_strategy_code)

    result = act.run_strategy_code_activity(
        strategy_code="def on_bar(): pass",
        market_data={"AAPL": [_bar_dict()]},
        config=_backtest_config_dict(),
    )
    assert result["success"] is True
    assert result["stdout"] == "ok"
    assert captured["market_data_keys"] == ["AAPL"]


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


def test_resolve_symbols_activity_reuses_market_data_service(monkeypatch):
    from investment_team.market_data_service import MarketDataService

    monkeypatch.setattr(
        MarketDataService, "resolve_strategy_symbols", lambda self, spec: ["AAPL", "MSFT"]
    )

    result = act.resolve_symbols_activity(spec=_spec_dict())
    assert result == ["AAPL", "MSFT"]


def test_resolve_readiness_prices_activity_returns_last_close_per_symbol(monkeypatch):
    from investment_team.market_data_service import MarketDataService, OHLCVBar

    def _fake_fetch_ohlcv(self, symbol, asset_class, days=365):
        if symbol == "AAPL":
            return [OHLCVBar(**_bar_dict(close=150.0))]
        return []

    monkeypatch.setattr(MarketDataService, "fetch_ohlcv", _fake_fetch_ohlcv)

    result = act.resolve_readiness_prices_activity(["AAPL", "MSFT"], "stocks")
    assert result == {"AAPL": 150.0}


def test_resolve_readiness_prices_activity_skips_failing_symbols(monkeypatch):
    from investment_team.market_data_service import MarketDataService

    def _fake_fetch_ohlcv(self, symbol, asset_class, days=365):
        raise RuntimeError("provider down")

    monkeypatch.setattr(MarketDataService, "fetch_ohlcv", _fake_fetch_ohlcv)

    result = act.resolve_readiness_prices_activity(["AAPL"], "stocks")
    assert result == {}


def test_fetch_market_data_activity_uses_fresh_service_instance(monkeypatch):
    from investment_team.market_data_service import MarketDataService

    def _fake_fetch(
        self,
        symbols,
        asset_class,
        start_date,
        end_date,
        *,
        intraday_mode=False,
        as_of=None,
        frequency="1d",
    ):
        self.provider_used["AAPL"] = "yahoo"
        from investment_team.market_data_service import OHLCVBar

        return {"AAPL": [OHLCVBar(**_bar_dict())]}

    monkeypatch.setattr(MarketDataService, "fetch_multi_symbol_range", _fake_fetch)

    result = act.fetch_market_data_activity(
        symbols=["AAPL"], asset_class="stocks", start_date="2023-01-01", end_date="2023-12-31"
    )
    assert result["provider_used"] == {"AAPL": "yahoo"}
    assert len(result["data"]["AAPL"]) == 1


def test_compute_regime_summary_activity_reuses_compute_regime_summary(monkeypatch):
    from investment_team.strategy_lab import market_regime
    from investment_team.strategy_lab.temporal import activities as act_mod

    def _fake_compute(fetch_ohlcv, *, computed_at, benchmarks=None, days=400):
        return market_regime.RegimeSummary(
            computed_at=computed_at, degraded=True, degraded_reason="no data"
        )

    monkeypatch.setattr(market_regime, "compute_regime_summary", _fake_compute)

    result = act_mod.compute_regime_summary_activity()
    assert result["degraded"] is True


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_persist_run_state_activity_delegates_to_api_main(monkeypatch):
    from investment_team.api import main as api_main

    captured = {}
    monkeypatch.setattr(
        api_main,
        "_persist_run_state",
        lambda run_id, state, *, create=False: captured.update(
            run_id=run_id, state=state, create=create
        ),
    )

    act.persist_run_state_activity("run-1", {"status": "running"}, create=True)
    assert captured == {"run_id": "run-1", "state": {"status": "running"}, "create": True}


def test_snapshot_prior_records_activity_delegates_to_api_main(monkeypatch):
    from investment_team.api import main as api_main
    from investment_team.models import StrategyLabRecord

    record = StrategyLabRecord(
        lab_record_id="rec-1",
        strategy=_spec_dict(),
        backtest={
            "backtest_id": "bt-1",
            "strategy_id": "strat-1",
            "strategy": _spec_dict(),
            "config": _backtest_config_dict(),
            "submitted_by": "test",
            "submitted_at": "2023-01-01T00:00:00Z",
            "completed_at": "2023-01-01T01:00:00Z",
            "result": _backtest_result_dict(),
        },
        is_winning=True,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2023-01-01T00:00:00Z",
    )
    monkeypatch.setattr(api_main, "_snapshot_prior_records", lambda *, reverse=False: [record])

    result = act.snapshot_prior_records_activity()
    assert result[0]["lab_record_id"] == "rec-1"


def test_persist_record_activity_delegates_to_api_main(monkeypatch):
    from investment_team.api import main as api_main

    captured = {}
    monkeypatch.setattr(
        api_main,
        "_persist_strategy_lab_record",
        lambda record: captured.update(record_id=record.lab_record_id),
    )

    act.persist_record_activity(
        {
            "lab_record_id": "rec-2",
            "strategy": _spec_dict(),
            "backtest": {
                "backtest_id": "bt-2",
                "strategy_id": "strat-1",
                "strategy": _spec_dict(),
                "config": _backtest_config_dict(),
                "submitted_by": "test",
                "submitted_at": "2023-01-01T00:00:00Z",
                "completed_at": "2023-01-01T01:00:00Z",
                "result": _backtest_result_dict(),
            },
            "is_winning": True,
            "strategy_rationale": "r",
            "analysis_narrative": "n",
            "created_at": "2023-01-01T00:00:00Z",
        }
    )
    assert captured == {"record_id": "rec-2"}


# ---------------------------------------------------------------------------
# Composite activities (wrap a whole orchestrator sub-pipeline verbatim)
# ---------------------------------------------------------------------------


def test_run_alignment_audit_activity_reuses_orchestrator_method(monkeypatch):
    from investment_team.strategy_lab.agents.alignment import TradeAlignmentReport
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    captured = {}

    def _fake_audit(self, spec, code, trades, metrics, prior_attempts, *, market_data, config):
        captured["spec_id"] = spec.strategy_id
        captured["market_data_keys"] = list(market_data.keys())
        return TradeAlignmentReport(aligned=True), []

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_alignment_audit", _fake_audit)

    result = act.run_alignment_audit_activity(
        spec=_spec_dict(),
        code="code",
        trades=[_trade_record_dict()],
        metrics=_backtest_result_dict(),
        prior_attempts=[],
        market_data={"AAPL": [_bar_dict()]},
        config=_backtest_config_dict(),
    )
    assert result["report"]["aligned"] is True
    assert result["gate_results"] == []
    assert captured["spec_id"] == "strat-1"
    assert captured["market_data_keys"] == ["AAPL"]


def test_run_verification_and_analysis_activity_round_trips_convergence_tracker(monkeypatch):
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    captured = {}

    def _fake_orchestrate(self, **kwargs):
        captured["trial_count_in"] = self.convergence_tracker.trial_count
        self.convergence_tracker.increment_trials(1)
        return kwargs["metrics"], True, "narrative text"

    monkeypatch.setattr(
        StrategyLabOrchestrator, "_orchestrate_verification_and_analysis", _fake_orchestrate
    )

    result = act.run_verification_and_analysis_activity(
        spec=_spec_dict(),
        trades=[_trade_record_dict()],
        metrics=_backtest_result_dict(),
        market_data={"AAPL": [_bar_dict()]},
        config=_backtest_config_dict(),
        execution_succeeded=True,
        trades_aligned=True,
        alignment_reports=[],
        all_gate_results=[],
        runtime_lookahead_violation=False,
        open_position_entry_reasons=[],
        refinement_attempts=[],
        rationale="why",
        convergence_tracker_state={"trial_count": 5},
    )
    assert captured["trial_count_in"] == 5
    assert result["is_winning"] is True
    assert result["narrative"] == "narrative text"
    assert result["convergence_tracker_state"]["trial_count"] == 6


def test_assemble_record_activity_reuses_orchestrator_method(monkeypatch):
    from investment_team.models import StrategyLabRecord
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    def _fake_assemble(self, **kwargs):
        self.convergence_tracker.increment_trials(1)
        return StrategyLabRecord(
            lab_record_id="rec-1",
            strategy=kwargs["spec"],
            backtest={
                "backtest_id": "bt-1",
                "strategy_id": kwargs["spec"].strategy_id,
                "strategy": kwargs["spec"],
                "config": kwargs["config"],
                "submitted_by": "test",
                "submitted_at": "2023-01-01T00:00:00Z",
                "completed_at": "2023-01-01T01:00:00Z",
                "result": kwargs["metrics"],
            },
            is_winning=kwargs["is_winning"],
            strategy_rationale=kwargs["rationale"],
            analysis_narrative=kwargs["narrative"],
            created_at="2023-01-01T00:00:00Z",
        )

    monkeypatch.setattr(StrategyLabOrchestrator, "_assemble_record", _fake_assemble)

    result = act.assemble_record_activity(
        {
            "spec": _spec_dict(),
            "code": "code",
            "config": _backtest_config_dict(),
            "metrics": _backtest_result_dict(),
            "trades": [_trade_record_dict()],
            "narrative": "narrative",
            "original_spec": _spec_dict(),
            "original_code": "code",
            "rationale": "why",
            "requested_symbols": ["AAPL"],
            "fetched_symbols": ["AAPL"],
            "provider_used": {"AAPL": "yahoo"},
            "max_rounds_exhausted": False,
            "execution_succeeded": True,
            "is_winning": True,
            "trades_aligned": True,
            "refinement_rounds": 0,
            "alignment_rounds": 0,
            "all_gate_results": [],
            "design_context": {"rounds": 1, "critiques": [], "stop_reason": "ready"},
            "drift_collector": {"spec_history": [], "code_history": [], "gate_timeline": []},
            "convergence_tracker_state": {},
        }
    )
    assert result["record"]["lab_record_id"] == "rec-1"
    assert result["convergence_tracker_state"]["trial_count"] == 1


def test_build_short_circuit_record_activity_reuses_orchestrator_method(monkeypatch):
    from investment_team.models import StrategyLabRecord
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    def _fake_build(self, **kwargs):
        self.convergence_tracker.increment_trials(1)
        return StrategyLabRecord(
            lab_record_id="rec-sc-1",
            strategy=kwargs["spec"],
            backtest={
                "backtest_id": "bt-sc-1",
                "strategy_id": kwargs["spec"].strategy_id,
                "strategy": kwargs["spec"],
                "config": kwargs["config"],
                "submitted_by": "test",
                "submitted_at": "2023-01-01T00:00:00Z",
                "completed_at": "2023-01-01T01:00:00Z",
                "result": _backtest_result_dict(),
                "status": kwargs["short_circuit_status"],
            },
            is_winning=False,
            strategy_rationale=kwargs["rationale"],
            analysis_narrative=kwargs["short_circuit_reason"],
            created_at="2023-01-01T00:00:00Z",
        )

    monkeypatch.setattr(StrategyLabOrchestrator, "_build_short_circuit_record", _fake_build)

    result = act.build_short_circuit_record_activity(
        {
            "spec": _spec_dict(),
            "config": _backtest_config_dict(),
            "code": "",
            "original_spec": _spec_dict(),
            "original_code": "",
            "rationale": "why",
            "all_gate_results": [],
            "refinement_attempts": [],
            "short_circuit_status": "failed: design_not_ready",
            "short_circuit_reason": "not ready",
            "convergence_tracker_state": {},
        }
    )
    assert result["record"]["lab_record_id"] == "rec-sc-1"
    assert result["record"]["is_winning"] is False
    assert result["convergence_tracker_state"]["trial_count"] == 1


def test_activities_list_contains_every_activity():
    assert len(act.ACTIVITIES) == 21
    assert act.design_generate_activity in act.ACTIVITIES
    assert act.persist_record_activity in act.ACTIVITIES
    assert act.resolve_readiness_prices_activity in act.ACTIVITIES
    assert act.run_alignment_audit_activity in act.ACTIVITIES
    assert act.run_verification_and_analysis_activity in act.ACTIVITIES
    assert act.assemble_record_activity in act.ACTIVITIES
    assert act.build_short_circuit_record_activity in act.ACTIVITIES
