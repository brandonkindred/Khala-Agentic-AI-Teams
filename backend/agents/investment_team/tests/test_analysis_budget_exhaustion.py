"""Regression: ``AnalysisAgent`` must not swallow ``DesignBudgetExhausted``.

Analysis draft/review call sites use ``charge=False`` today, so they do not
trip the per-cycle budget themselves. They still pass
``guard_design_budget=True`` so that if a budget trip reaches the single-shot
driver (or charging is later enabled), the exception propagates bare instead
of being handed to ``on_failure`` and turned into ``_fallback_narrative``.
"""

from __future__ import annotations

from typing import Any

import pytest

from investment_team.models import BacktestResult, StrategySpec
from investment_team.strategy_lab.agents import _agent_runner as agent_runner_module
from investment_team.strategy_lab.agents._llm_budget import DesignBudgetExhausted
from investment_team.strategy_lab.agents.analysis import AnalysisAgent
from investment_team.strategy_lab.spec_dsl import EntryRule, Predicate, StopLossRule


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-budget",
        authored_by="test-suite",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(lhs="bar.close", op=">", rhs=0),
            )
        ],
        exit_rules=[StopLossRule(pct=0.03)],
        risk_limits={},
        speculative=False,
    )


def _metrics() -> BacktestResult:
    return BacktestResult(
        total_return_pct=18.0,
        annualized_return_pct=15.0,
        volatility_pct=8.0,
        sharpe_ratio=1.4,
        max_drawdown_pct=4.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )


def _run_kwargs() -> dict[str, Any]:
    return {
        "spec": _spec(),
        "metrics": _metrics(),
        "trades": [],
        "rationale": "rationale",
        "is_winning": True,
    }


def test_draft_budget_exhaustion_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``DesignBudgetExhausted`` raised during the draft call must escape
    ``AnalysisAgent.run`` — not become a fallback narrative string."""
    trip = DesignBudgetExhausted(limit=3, calls_made=3)

    def _raise_budget(*_a: Any, **_k: Any) -> Any:
        raise trip

    monkeypatch.setattr(agent_runner_module, "run_structured_agent", _raise_budget)
    monkeypatch.setattr(agent_runner_module, "Agent", lambda **_k: object())
    monkeypatch.setattr(agent_runner_module, "get_strands_model", lambda *_a, **_k: None)

    with pytest.raises(DesignBudgetExhausted) as exc_info:
        AnalysisAgent().run(**_run_kwargs())

    assert exc_info.value is trip


def test_review_budget_exhaustion_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``DesignBudgetExhausted`` raised during the self-review call must
    likewise propagate — the review ``on_failure`` path must not swallow it
    into a silent "use draft" fallback."""
    trip = DesignBudgetExhausted(limit=5, calls_made=5)
    call_count = {"n": 0}

    def _draft_ok_then_budget(*_a: Any, **_k: Any) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"draft_narrative": "draft body"}
        raise trip

    monkeypatch.setattr(agent_runner_module, "run_structured_agent", _draft_ok_then_budget)
    monkeypatch.setattr(agent_runner_module, "Agent", lambda **_k: object())
    monkeypatch.setattr(agent_runner_module, "get_strands_model", lambda *_a, **_k: None)

    with pytest.raises(DesignBudgetExhausted) as exc_info:
        AnalysisAgent().run(**_run_kwargs())

    assert exc_info.value is trip
    assert call_count["n"] == 2


def test_non_budget_draft_failure_still_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary draft failures still use ``_fallback_narrative``; only budget
    trips are special-cased by ``guard_design_budget``."""

    class _StubAgent:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def __call__(self, _prompt: str) -> str:
            raise RuntimeError("transport blip")

    monkeypatch.setattr(agent_runner_module, "Agent", _StubAgent)
    monkeypatch.setattr(agent_runner_module, "get_strands_model", lambda *_a, **_k: None)

    narrative = AnalysisAgent().run(**_run_kwargs())

    assert isinstance(narrative, str)
    assert narrative  # non-empty fallback
    assert "transport blip" not in narrative
