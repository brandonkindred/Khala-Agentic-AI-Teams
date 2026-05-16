"""Regression guard for issue #529 follow-up review (PR #573).

``AnalysisAgent.run`` used to derive its own verdict from
``metrics.annualized_return_pct > 8.0`` to choose between ``analysis_win.md``
and ``analysis_lose.md`` and set ``outcome_label="WINNING"|"LOSING"``. After
#529, the orchestrator can mark a high-return run as ``is_winning=False`` —
the alignment loop, the walk-forward acceptance gate, or the removal of the
legacy ``walk_forward_enabled=False`` publication path can all veto. The
caller now threads the resolved verdict in, and this test pins that the agent
honours it instead of looking at the metric.
"""

from __future__ import annotations

import json
from typing import Any, List

from investment_team.models import BacktestResult, StrategySpec
from investment_team.strategy_lab.agents import analysis as analysis_module
from investment_team.strategy_lab.agents.analysis import AnalysisAgent
from investment_team.strategy_lab.spec_dsl import (
    ConstRef,
    EntryRule,
    Predicate,
    PriceRef,
    TimeStopRule,
)


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-test",
        authored_by="test-suite",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(lhs=PriceRef(), op="gt", rhs=ConstRef(value=0)),
            )
        ],
        exit_rules=[TimeStopRule(n_bars=5)],
        risk_limits={},
        speculative=False,
    )


def _high_return_metrics() -> BacktestResult:
    # Above the legacy WINNING_THRESHOLD (8.0) — under the old code path
    # this would force ``is_winning=True`` inside AnalysisAgent.
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


class _RecordingAgent:
    """Stand-in for ``strands.Agent``. Records every prompt it is called with
    and returns a JSON-shaped string the production code can parse."""

    instances: List["_RecordingAgent"] = []
    call_count = 0

    def __init__(self, *_: Any, **__: Any) -> None:
        self.prompts: List[str] = []
        _RecordingAgent.instances.append(self)

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        _RecordingAgent.call_count += 1
        if _RecordingAgent.call_count == 1:
            return json.dumps({"draft_narrative": "draft body"})
        return json.dumps(
            {
                "revised_narrative": "final revised narrative",
                "verification_notes": "checked",
            }
        )


def _install_recording_agent(monkeypatch) -> None:
    _RecordingAgent.instances = []
    _RecordingAgent.call_count = 0
    monkeypatch.setattr(analysis_module, "Agent", _RecordingAgent)
    monkeypatch.setattr(analysis_module, "get_strands_model", lambda _name: None)


def _all_prompts() -> str:
    return "\n\n".join(p for inst in _RecordingAgent.instances for p in inst.prompts)


def test_analysis_agent_honours_explicit_is_winning_false_on_high_return(monkeypatch):
    """Even with metrics.annualized_return_pct=15% (legacy ``is_winning=True``),
    an explicit ``is_winning=False`` from the orchestrator must select the
    LOSING template + ``outcome_label="LOSING"`` so the narrative cannot tell
    users the strategy won."""

    _install_recording_agent(monkeypatch)

    AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),
        trades=[],
        rationale="rationale",
        is_winning=False,
    )

    prompts = _all_prompts()
    # The draft template selected must be the losing one. Its checked-in
    # opening line is the simplest cross-prompt sentinel.
    assert "LOSING swing-trading strategy" in prompts, (
        "AnalysisAgent must use analysis_lose.md when is_winning=False is "
        "forced by the orchestrator (issue #529 follow-up)."
    )
    assert "WINNING swing-trading strategy" not in prompts, (
        "AnalysisAgent must NOT use analysis_win.md when is_winning=False is "
        "forced by the orchestrator (issue #529 follow-up)."
    )
    # Self-review's outcome_label must agree.
    assert "Outcome label: LOSING" in prompts
    assert "Outcome label: WINNING" not in prompts


def test_analysis_agent_honours_explicit_is_winning_true_on_low_return(monkeypatch):
    """Symmetric guard: when the orchestrator says is_winning=True the
    narrative must use the WINNING template even if metrics alone would
    have rendered LOSING."""

    _install_recording_agent(monkeypatch)

    low_return = _high_return_metrics().model_copy(
        update={"annualized_return_pct": 3.0, "total_return_pct": 3.5}
    )

    AnalysisAgent().run(
        _spec(),
        low_return,
        trades=[],
        rationale="rationale",
        is_winning=True,
    )

    prompts = _all_prompts()
    assert "WINNING swing-trading strategy" in prompts
    assert "Outcome label: WINNING" in prompts


def test_analysis_agent_falls_back_to_metric_heuristic_when_unset(monkeypatch):
    """Back-compat: when no ``is_winning`` is passed the legacy
    metric-based derivation is preserved (no behaviour change for callers
    that haven't migrated)."""

    _install_recording_agent(monkeypatch)

    AnalysisAgent().run(
        _spec(),
        _high_return_metrics(),  # 15% annualized → legacy heuristic says winning
        trades=[],
        rationale="rationale",
    )

    prompts = _all_prompts()
    assert "WINNING swing-trading strategy" in prompts
    assert "Outcome label: WINNING" in prompts
