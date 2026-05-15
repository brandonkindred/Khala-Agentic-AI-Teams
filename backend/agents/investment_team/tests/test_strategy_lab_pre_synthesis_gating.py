"""Pre-synthesis spec gating in the Strategy Lab orchestrator (#547 item 1).

The orchestrator now runs ``StrategySpecValidator`` once, immediately
after ideation and before the refinement loop. Critical failures
short-circuit the cycle: no sandbox call, no market-data fetch, and the
persisted record carries ``status="failed: spec_validation"``.

These tests stub ``IdeationAgent`` and patch
``orchestrator_module.run_strategy_code`` so a failure trips the new
short-circuit and the test fails loudly if the orchestrator ever calls
the sandbox.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

import pytest

from investment_team.models import BacktestConfig
from investment_team.strategy_lab import orchestrator as orchestrator_module
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )


def _ideation_returning(strategy_dict: Dict[str, Any], code: str) -> Any:
    """Build a stub ``IdeationAgent.run`` that returns scripted output."""

    def _run(**_kwargs) -> Tuple[Dict[str, Any], str, str]:
        return strategy_dict, code, "scripted rationale"

    return _run


_VALID_CODE = (
    "from contract import Strategy\n\n"
    "class S(Strategy):\n"
    "    def on_bar(self, ctx, bar):\n"
    "        ctx.submit_order(symbol='X', qty=1, side='LONG')\n"
    "        ctx.submit_order(symbol='X', qty=1, side='FLAT')\n"
)


def test_pre_synthesis_critical_failure_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty entry_rules trips the validator → cycle exits without sandbox call."""
    bad_spec_dict = {
        "asset_class": "stocks",
        "hypothesis": "test",
        "signal_definition": "sig",
        "entry_rules": [],  # critical: no entry rules
        "exit_rules": ["exit"],
        "sizing_rules": ["risk 2%"],
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10},
        "speculative": False,
    }

    orch = StrategyLabOrchestrator()
    monkeypatch.setattr(orch.ideation_agent, "run", _ideation_returning(bad_spec_dict, _VALID_CODE))

    def _sandbox_must_not_run(*_a, **_kw):
        raise AssertionError("run_strategy_code must not be called when pre-synthesis gating fails")

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _sandbox_must_not_run)

    def _market_data_must_not_run(self, *_a, **_kw):
        raise AssertionError(
            "_fetch_market_data must not be called when pre-synthesis gating fails"
        )

    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _market_data_must_not_run)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert record.backtest.status == "failed: spec_validation"
    assert record.is_winning is False
    assert record.refinement_rounds == 0
    # The pre-synthesis gate result must be persisted with refinement_round=-1
    pre_synth_gates = [g for g in record.quality_gate_results if g.get("refinement_round") == -1]
    assert pre_synth_gates, record.quality_gate_results
    assert any(g.get("severity") == "critical" and not g.get("passed") for g in pre_synth_gates), (
        pre_synth_gates
    )


def test_pre_synthesis_validator_persisted_even_on_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid spec records the pre-synthesis pass result and enters the loop."""
    valid_spec_dict = {
        "asset_class": "stocks",
        "hypothesis": "RSI signal strategy",
        "signal_definition": "sig",
        "entry_rules": ["enter when RSI < 30"],
        "exit_rules": ["exit when RSI > 70"],
        "sizing_rules": ["risk 2% per trade"],
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10},
        "speculative": False,
    }

    orch = StrategyLabOrchestrator()
    monkeypatch.setattr(
        orch.ideation_agent, "run", _ideation_returning(valid_spec_dict, _VALID_CODE)
    )

    # The orchestrator will then enter the refinement loop. Short-circuit on
    # the first failed code_safety check so the test does not need a full
    # sandbox stack — record stays in scope to assert on the pre-synth gates.
    monkeypatch.setattr(
        StrategyLabOrchestrator,
        "_fetch_market_data",
        lambda *_a, **_kw: None,
    )

    record = orch.run_cycle(prior_records=[], config=_config())

    pre_synth_gates = [g for g in record.quality_gate_results if g.get("refinement_round") == -1]
    assert pre_synth_gates, record.quality_gate_results
    # All pre-synthesis gates passed (or are info-severity) — none should be critical.
    assert not any(
        g.get("severity") == "critical" and not g.get("passed") for g in pre_synth_gates
    ), pre_synth_gates


def test_missing_strategy_code_does_not_short_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ideation may return an empty ``strategy_code``. That's a
    code-generation failure that the refinement loop's existing code-
    safety + regeneration paths are equipped to repair; the pre-synthesis
    spec gate must NOT short-circuit on it (regressing a previously-
    recoverable case into an outright failure).

    The validator's ``strategy_code is missing — nothing to execute``
    critical is excluded from short-circuit eligibility; the cycle
    proceeds into the refinement loop.
    """
    valid_spec_dict_but_no_code = {
        "asset_class": "stocks",
        "hypothesis": "RSI signal strategy",
        "signal_definition": "sig",
        "entry_rules": ["enter when RSI < 30"],
        "exit_rules": ["exit when RSI > 70"],
        "sizing_rules": ["risk 2% per trade"],
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10},
        "speculative": False,
    }

    orch = StrategyLabOrchestrator()
    # Empty string — triggers the validator's "strategy_code is missing"
    # critical but the spec itself is otherwise valid.
    monkeypatch.setattr(
        orch.ideation_agent,
        "run",
        _ideation_returning(valid_spec_dict_but_no_code, ""),
    )
    # Short-circuit further down by returning no market data so the cycle
    # exits cleanly without needing a full sandbox stack.
    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", lambda *_a, **_kw: None)

    record = orch.run_cycle(prior_records=[], config=_config())

    # The cycle did NOT short-circuit with "failed: spec_validation":
    # the validator's strategy_code critical was excluded from pre-synth
    # gating, so we proceeded past it.
    assert record.backtest.status != "failed: spec_validation", record.backtest.status
