"""Orchestrator-level composition of acceptance-gate results into the
``is_winning`` verdict.

Unlike ``test_acceptance_gate.py`` (unit-level ``AcceptanceGate.check()``
coverage), this file exercises how those gate results, and the anomaly
detector's severity upgrades on the walk-forward fallback path, drive the
orchestrator's acceptance decision end-to-end. Alignment-loop caveats and
other ``run_cycle`` acceptance-reason resolution paths are covered in
``test_run_cycle_caveat_resolution.py``; the walk-forward evaluation
mechanics themselves are covered in ``test_walk_forward_evaluation.py``.
"""

from __future__ import annotations

from investment_team.models import BacktestResult
from investment_team.strategy_lab.quality_gates.acceptance_gate import AcceptanceGate

from ._walk_forward_test_helpers import StubMarketDataService as _StubMarketDataService
from ._walk_forward_test_helpers import config as _config
from ._walk_forward_test_helpers import orchestrator as _orchestrator
from ._walk_forward_test_helpers import stub_bars as _stub_bars
from ._walk_forward_test_helpers import trades_across_year as _trades_across_year
from .conftest import stub_design_loop


def test_acceptance_gate_passes_winning_walk_forward_result():
    """A walk-forward result that clears all four sub-criteria produces an
    all-passing gate result; the orchestrator's ``is_winning`` rule treats
    that as a win."""
    cfg = _config(
        walk_forward_enabled=True,
        dsr_threshold=0.5,
        max_is_oos_degradation_pct=40.0,
        min_oos_trades=10,
    )
    res = BacktestResult(
        total_return_pct=12.0,
        annualized_return_pct=14.0,
        volatility_pct=9.0,
        sharpe_ratio=1.4,
        max_drawdown_pct=5.0,
        win_rate_pct=58.0,
        profit_factor=1.6,
        deflated_sharpe=0.8,
        oos_sharpe=1.2,
        is_sharpe=1.3,
        is_oos_degradation_pct=10.0,
        oos_trade_count=40,
        regime_results=[
            {"regime": "vix_q1", "beat_benchmark": True},
            {"regime": "vix_q2", "beat_benchmark": True},
            {"regime": "vix_q3", "beat_benchmark": False},
            {"regime": "vix_q4", "beat_benchmark": False},
        ],
        calmar_ratio=0.0,
        sortino_ratio=0.0,
    )
    results = AcceptanceGate().check(res, cfg, n_trials=10)
    assert all(r.passed for r in results)
    assert (True and all(r.passed for r in results)) is True  # the orchestrator's rule


def test_acceptance_gate_rejects_overfit_pattern():
    """High IS Sharpe + collapsed OOS Sharpe + insufficient regime breadth
    must trip multiple sub-criteria, so the orchestrator marks the cycle
    as not-winning."""
    cfg = _config(
        walk_forward_enabled=True,
        dsr_threshold=0.9,
        max_is_oos_degradation_pct=30.0,
        min_oos_trades=30,
    )
    res = BacktestResult(
        total_return_pct=18.0,
        annualized_return_pct=22.0,
        volatility_pct=11.0,
        sharpe_ratio=2.5,  # IS-only sharpe (the headline single-window number)
        max_drawdown_pct=6.0,
        win_rate_pct=63.0,
        profit_factor=1.9,
        deflated_sharpe=0.3,  # low — overfit suspicion
        oos_sharpe=0.4,
        is_sharpe=2.5,
        is_oos_degradation_pct=84.0,  # huge IS→OOS gap
        oos_trade_count=12,  # below min_oos_trades
        regime_results=[
            {"regime": "vix_q1", "beat_benchmark": False},
            {"regime": "vix_q2", "beat_benchmark": False},
            {"regime": "vix_q3", "beat_benchmark": False},
            {"regime": "vix_q4", "beat_benchmark": False},
        ],
        calmar_ratio=0.0,
        sortino_ratio=0.0,
    )
    results = AcceptanceGate().check(res, cfg, n_trials=50)
    assert not all(r.passed for r in results)
    failed_reasons = [r.details for r in results if not r.passed]
    # Each of the four sub-criteria fails on this fixture.
    assert len(failed_reasons) == 4


def test_walk_forward_fallback_records_overfit_anomaly_as_caveat(monkeypatch):
    """When walk-forward evaluation raises and we fall back, the orchestrator
    re-runs anomaly checks with ``dsr_aware=False`` so a downgraded
    ``Sharpe > 5`` flag becomes critical again and is recorded on the gate
    timeline + ``acceptance_reason``. Under the deterministic verdict this
    overfit-suspect 60% run is still WINNING (60% >= the 8% benchmark) — the
    anomaly is a caveat that rides into the narrative, not a rejection."""

    from investment_team.models import StrategyLabRecord

    orch = _orchestrator(_StubMarketDataService())

    # Force walk-forward to raise so we exercise the fallback path.
    def _raise(*args, **kwargs):
        raise RuntimeError("walk-forward fold construction failed (synthetic)")

    monkeypatch.setattr(orch, "_evaluate_walk_forward", _raise)

    # Stub the agents that would otherwise call the LLM.
    from investment_team.strategy_lab.spec_dsl import EntryRule, Predicate, StopLossRule

    spec_dict = {
        "asset_class": "stocks",
        "hypothesis": "h",
        "signal_definition": "s",
        "entry_rules": [
            EntryRule(
                side="long",
                when=Predicate(lhs="bar.close", op=">", rhs=0),
            ).model_dump()
        ],
        "exit_rules": [StopLossRule(pct=0.20).model_dump()],
        "risk_limits": {},
        "speculative": False,
    }
    overfit_code = (
        "from contract import Strategy\n\nclass S(Strategy):\n"
        "    def on_bar(self, ctx, bar):\n"
        "        ctx.submit_order(symbol='X', qty=1, side='LONG')\n"
        "        ctx.submit_order(symbol='X', qty=1, side='FLAT')\n"
    )

    stub_design_loop(monkeypatch, orch, spec_dict, overfit_code)
    monkeypatch.setattr(
        orch.refinement_agent, "run", lambda **kw: ({"changes_made": "x"}, overfit_code)
    )
    # The minimal stub strategy code used here (qty=1, no entry conditional)
    # would otherwise be rejected by code conformance. Neutralise it the
    # same way the other gates are neutralised — synthesis-phase code
    # conformance is not the subject under test on the fallback path.
    monkeypatch.setattr(orch.code_conformance_gate, "check", lambda *a, **kw: [])
    # The deterministic alignment gate decides ``aligned``. Stub the gate's
    # ``check`` to script the same verdict the LLM path used to produce.
    # ``propose_code_fix`` is never consulted for an aligned gate verdict.
    from investment_team.strategy_lab.quality_gates.alignment_checks import (
        AlignmentCheckResult,
    )

    monkeypatch.setattr(
        orch.deterministic_alignment_checker,
        "check",
        lambda **kw: AlignmentCheckResult(aligned=True, rationale="ok"),
    )
    monkeypatch.setattr(orch.analysis_agent, "run", lambda *a, **k: "narrative")
    # ``_fetch_market_data`` returns a ``_MarketDataFetch`` envelope.
    from investment_team.strategy_lab.orchestrator import _MarketDataFetch

    monkeypatch.setattr(
        orch,
        "_fetch_market_data",
        lambda spec, config: _MarketDataFetch(
            data={"AAPL": _stub_bars("AAPL")},
            requested_symbols=["AAPL"],
            fetched_symbols=["AAPL"],
        ),
    )

    # Synthesize an "overfit" backtest: high Sharpe (>5) and high
    # annualized return (>WINNING_THRESHOLD). With dsr_aware=False the
    # Sharpe>5 critical is what we expect to upgrade severity on
    # fallback.
    overfit_result = BacktestResult(
        total_return_pct=80.0,
        annualized_return_pct=60.0,
        volatility_pct=8.0,
        sharpe_ratio=6.5,
        max_drawdown_pct=4.0,
        win_rate_pct=60.0,
        profit_factor=2.4,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    overfit_trades = _trades_across_year(n_per_month=4, base_pnl=80.0)

    class _StubExecResult:
        def __init__(self):
            self.success = True
            self.trades = overfit_trades
            self.execution_time_seconds = 0.01
            self.error_type = None
            self.stderr = ""
            self.execution_diagnostics = None

    monkeypatch.setattr(
        "investment_team.strategy_lab.orchestrator.run_strategy_code",
        lambda *a, **k: _StubExecResult(),
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.orchestrator.compute_metrics",
        lambda *a, **k: overfit_result,
    )

    config = _config(walk_forward_enabled=True)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    # Caveats-only: the 60% return clears the 8% benchmark, so the run is
    # WINNING; the upgraded Sharpe>5 critical is recorded as a caveat rather
    # than flipping the verdict.
    assert record.is_winning is True
    # The persisted gate-result history reflects the upgraded severity so
    # downstream consumers can audit the caveat.
    fallback_gates = [
        g for g in record.quality_gate_results if g.get("gate_name", "").startswith("fallback_")
    ]
    assert any(
        g.get("severity") == "critical" and "Sharpe ratio" in g.get("details", "")
        for g in fallback_gates
    )
