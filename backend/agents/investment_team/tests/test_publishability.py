"""Tests for Strategy Lab ``is_publishable`` decision + skip-reason helper.

Preconditions: investment_team package importable under pytest.
Postconditions: asserts the publishability contract — joined veto-order
skip codes, model default ``False``, orchestrator wiring, and paper-trade
gating.
"""

from __future__ import annotations

from investment_team.models import (
    BacktestConfig,
    BacktestRecord,
    BacktestResult,
    StrategyLabRecord,
    StrategySpec,
)
from investment_team.strategy_lab.spec_dsl import EntryRule, Predicate, StopLossRule


def _make_record(*, is_winning: bool = True, **kwargs) -> StrategyLabRecord:
    strategy = StrategySpec(
        strategy_id="strat-pub-test",
        authored_by="test",
        asset_class="equities",
        hypothesis="test",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=1))],
        exit_rules=[StopLossRule(pct=0.03)],
        risk_limits={},
        speculative=False,
    )
    config = BacktestConfig(start_date="2021-01-01", end_date="2024-12-31")
    metrics = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        win_rate_pct=55.0,
        trade_count=20,
        profit_factor=1.5,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
        volatility_pct=10.0,
    )
    backtest = BacktestRecord(
        backtest_id="bt-pub-test",
        strategy_id=strategy.strategy_id,
        strategy=strategy,
        config=config,
        submitted_by="test",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T00:01:00Z",
        result=metrics,
        notes=[],
        trades=[],
    )
    fields = dict(
        lab_record_id="lab-pub-test",
        strategy=strategy,
        backtest=backtest,
        is_winning=is_winning,
        strategy_rationale="rationale",
        analysis_narrative="narrative",
        created_at="2024-01-01T00:01:00Z",
        strategy_code="def strategy(): pass\n",
    )
    fields.update(kwargs)
    return StrategyLabRecord(**fields)


def test_publishability_skip_reason_none_when_all_pass():
    from investment_team.strategy_lab._orchestrator_helpers import publishability_skip_reason

    assert (
        publishability_skip_reason(
            exit_rule_conformance_passed=True,
            realism_passed=True,
            trades_aligned=True,
            runtime_lookahead_violation=False,
        )
        is None
    )


def test_publishability_skip_reason_joins_in_veto_order():
    from investment_team.strategy_lab._orchestrator_helpers import publishability_skip_reason

    assert publishability_skip_reason(
        exit_rule_conformance_passed=False,
        realism_passed=False,
        trades_aligned=False,
        runtime_lookahead_violation=True,
    ) == (
        "exit_rule_conformance_failed,realism_failed,"
        "alignment_unresolved,lookahead_violation"
    )


def test_publishability_skip_reason_single_realism_failed():
    from investment_team.strategy_lab._orchestrator_helpers import publishability_skip_reason

    assert (
        publishability_skip_reason(
            exit_rule_conformance_passed=True,
            realism_passed=False,
            trades_aligned=True,
            runtime_lookahead_violation=False,
        )
        == "realism_failed"
    )


def test_strategy_lab_record_is_publishable_defaults_false():
    """Omitted publishability fields must default to not publishable."""
    record = _make_record(is_winning=True)
    assert record.is_publishable is False
    assert record.publishability_skip_reason is None


def test_strategy_lab_record_accepts_explicit_publishable():
    record = _make_record(is_winning=True, is_publishable=True)
    assert record.is_publishable is True


def test_strategy_lab_record_legacy_dict_missing_is_publishable():
    """Persisted rows without the field deserialize as not publishable."""
    record = _make_record(is_winning=True, is_publishable=True)
    raw = record.model_dump()
    del raw["is_publishable"]
    del raw["publishability_skip_reason"]
    restored = StrategyLabRecord.model_validate(raw)
    assert restored.is_publishable is False
    assert restored.publishability_skip_reason is None


def test_verification_phase_misaligned_is_winning_not_publishable(monkeypatch):
    """Misaligned trades keep is_winning when return clears the bar, but block publishability."""
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult
    from investment_team.tests.test_realism_orchestrator_wiring import (
        _config,
        _metrics,
        _orch,
        _spec,
        _trade,
    )

    orch = _orch()
    monkeypatch.setattr(
        orch, "_evaluate_walk_forward", lambda spec, md, cfg, trades, metrics: metrics
    )
    monkeypatch.setattr(
        orch.acceptance_gate,
        "check",
        lambda metrics, config, n_trials: [
            QualityGateResult(
                gate_name="oos_deflated_sharpe",
                passed=True,
                severity="info",
                phase="verification",
                details="ok",
            ),
        ],
    )
    monkeypatch.setattr(orch, "_run_realism_gates", lambda **_kwargs: [])

    outcome = orch._run_verification_phase(
        spec=_spec(target_symbols=["QQQ"]),
        trades=[_trade("QQQ", i + 1) for i in range(20)],
        metrics=_metrics(),
        market_data={"QQQ": []},
        config=_config(),
        execution_succeeded=True,
        trades_aligned=False,
        alignment_reports=[],
        all_gate_results=[],
        emit=lambda *_a, **_k: None,
    )

    assert outcome.is_winning is True
    assert outcome.is_publishable is False
    assert outcome.publishability_skip_reason == "alignment_unresolved"


def test_verification_phase_clean_winner_is_publishable(monkeypatch):
    """Fully clean gates → is_publishable True."""
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult
    from investment_team.tests.test_realism_orchestrator_wiring import (
        _config,
        _metrics,
        _orch,
        _spec,
        _trade,
    )

    orch = _orch()
    monkeypatch.setattr(
        orch, "_evaluate_walk_forward", lambda spec, md, cfg, trades, metrics: metrics
    )
    monkeypatch.setattr(
        orch.acceptance_gate,
        "check",
        lambda metrics, config, n_trials: [
            QualityGateResult(
                gate_name="oos_deflated_sharpe",
                passed=True,
                severity="info",
                phase="verification",
                details="ok",
            ),
        ],
    )
    monkeypatch.setattr(orch, "_run_realism_gates", lambda **_kwargs: [])

    outcome = orch._run_verification_phase(
        spec=_spec(target_symbols=["QQQ"]),
        trades=[_trade("QQQ", i + 1) for i in range(20)],
        metrics=_metrics(),
        market_data={"QQQ": []},
        config=_config(),
        execution_succeeded=True,
        trades_aligned=True,
        alignment_reports=[],
        all_gate_results=[],
        emit=lambda *_a, **_k: None,
    )

    assert outcome.is_winning is True
    assert outcome.is_publishable is True
    assert outcome.publishability_skip_reason is None
