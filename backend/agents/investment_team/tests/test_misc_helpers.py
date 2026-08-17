"""Coverage for assorted helper modules that were under-tested.

* ``investment_team.orchestrator.InvestmentTeamOrchestrator`` —
  bootstrap, enqueue, integrity, proposal check, web action plumbing.
* ``investment_team.execution.risk_free_rate.get_risk_free_rate`` — env
  override, FRED success / failure, and the hard-coded default.
* ``investment_team.strategy_lab_context`` — alias maps, strict variant,
  prior-results truncation, asset-class mix hint steering.
* ``ConvergenceTracker`` — diversity directive, stall detection, trial
  counter, snapshot+merge_from.
* ``strategy_lab.agents.model_factory.get_strands_model`` — dummy +
  ollama-cloud-without-key error branches, bedrock branch.
"""

from __future__ import annotations

import math
from typing import Any, Dict

import pytest

from investment_team.execution.risk_free_rate import RFR_DEFAULT, get_risk_free_rate
from investment_team.orchestrator import (
    InvestmentTeamOrchestrator,
    QueueItem,
    WorkflowState,
)
from investment_team.strategy_lab.quality_gates.convergence_tracker import (
    ConvergenceTracker,
    _jaccard,
)
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab_context import (
    _entry_archetype,
    _exit_archetypes,
    _is_executed_record,
    aggregate_prior_results,
    asset_class_mix_hint,
    format_prior_attribution,
    format_prior_results,
    normalize_asset_class,
    normalize_asset_class_strict,
)

# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


def test_orchestrator_bootstrap_sets_mode_and_audit() -> None:
    from investment_team.tests.test_investment_team import _sample_ips

    state = WorkflowState()
    InvestmentTeamOrchestrator().bootstrap(state, _sample_ips())
    assert state.audit_log[-1].startswith("workflow_bootstrap:mode=")


def test_orchestrator_enqueue_and_audit() -> None:
    state = WorkflowState()
    InvestmentTeamOrchestrator().enqueue(
        state, QueueItem(queue="research", payload_id="r1", priority="high")
    )
    assert len(state.queues["research"]) == 1
    assert state.audit_log[-1] == "enqueued:research:r1:high"


def test_orchestrator_handle_data_integrity_failure() -> None:
    state = WorkflowState()
    InvestmentTeamOrchestrator().handle_data_integrity(state, False)
    assert state.mode.value == "monitor_only"
    assert "data_integrity_failed" in state.audit_log[-1]


def test_orchestrator_handle_data_integrity_ok_is_noop() -> None:
    state = WorkflowState()
    state.mode = state.mode.LIVE if hasattr(state.mode, "LIVE") else state.mode
    InvestmentTeamOrchestrator().handle_data_integrity(state, True)
    # Audit log should NOT carry the failure breadcrumb.
    assert all("data_integrity_failed" not in entry for entry in state.audit_log)


def test_orchestrator_check_proposal_passes_and_fails() -> None:
    from investment_team.models import PortfolioPosition, PortfolioProposal
    from investment_team.tests.test_investment_team import _sample_ips

    ips = _sample_ips()
    orch = InvestmentTeamOrchestrator()
    state = WorkflowState()

    # Pass: small allocation.
    ok_proposal = PortfolioProposal(
        proposal_id="p-ok",
        prepared_by="x",
        ips_version="1.0",
        data_snapshot_id="snap",
        objective="balanced",
        positions=[
            PortfolioPosition(symbol="VTI", asset_class="equities", weight_pct=5.0, rationale="r")
        ],
    )
    assert orch.check_proposal(state, ips, ok_proposal) == []
    assert state.audit_log[-1].startswith("proposal_passed:")

    # Fail: oversized single position.
    bad_proposal = PortfolioProposal(
        proposal_id="p-bad",
        prepared_by="x",
        ips_version="1.0",
        data_snapshot_id="snap",
        objective="balanced",
        positions=[
            PortfolioPosition(symbol="VTI", asset_class="equities", weight_pct=60.0, rationale="r")
        ],
    )
    violations = orch.check_proposal(state, ips, bad_proposal)
    assert violations  # populated
    assert state.audit_log[-1].startswith("proposal_rejected:")


def test_orchestrator_run_web_action_requires_coordinator() -> None:
    orch = InvestmentTeamOrchestrator()
    with pytest.raises(RuntimeError) as exc:
        orch.run_web_action("noop")
    assert "web interface coordinator is not configured" in str(exc.value)


def test_orchestrator_run_web_action_delegates_to_coordinator() -> None:
    class _Coord:
        def __init__(self):
            self.calls: list[Dict[str, Any]] = []

        def execute_action(self, action, payload=None, workspace_name=None):
            self.calls.append(
                {"action": action, "payload": payload, "workspace_name": workspace_name}
            )
            return {"status": "ok"}

    coord = _Coord()
    orch = InvestmentTeamOrchestrator(web_interface_coordinator=coord)
    result = orch.run_web_action("noop", payload={"k": "v"}, workspace_name="ws1")
    assert result == {"status": "ok"}
    assert coord.calls == [{"action": "noop", "payload": {"k": "v"}, "workspace_name": "ws1"}]


def test_orchestrator_promotion_decision_enqueues_escalation_on_reject() -> None:
    """When the gate rejects, the orchestrator should push to escalation queue."""
    from investment_team.agents import AgentIdentity
    from investment_team.tests.test_investment_team import _sample_ips, _sample_validation

    state = WorkflowState()
    orch = InvestmentTeamOrchestrator()

    from investment_team.models import StrategySpec

    strategy = StrategySpec(
        strategy_id="s-r",
        authored_by="alice",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    decision = orch.promotion_decision(
        state=state,
        strategy=strategy,
        validation=_sample_validation().model_copy(update={"strategy_id": "s-r"}),
        ips=_sample_ips(),
        proposer_agent_id="alice",
        approver=AgentIdentity(
            agent_id="alice", role="approver", version="1.0"
        ),  # self-approval → REJECT
        risk_veto=False,
    )
    assert decision.outcome.value == "reject"
    assert any(item.payload_id == "s-r" for item in state.queues["escalation"])


# ---------------------------------------------------------------------------
# risk_free_rate
# ---------------------------------------------------------------------------


def test_get_risk_free_rate_override_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATEGY_LAB_RISK_FREE_RATE", "0.10")
    monkeypatch.setenv("FRED_API_KEY", "test-key-placeholder")
    # Override wins over both env vars.
    assert get_risk_free_rate(override=0.07) == 0.07


def test_get_risk_free_rate_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATEGY_LAB_RISK_FREE_RATE", "0.055")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    assert get_risk_free_rate() == pytest.approx(0.055)


def test_get_risk_free_rate_env_invalid_value_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRATEGY_LAB_RISK_FREE_RATE", "not-a-number")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    assert get_risk_free_rate() == RFR_DEFAULT


def test_get_risk_free_rate_fred_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRATEGY_LAB_RISK_FREE_RATE", raising=False)
    monkeypatch.setenv("FRED_API_KEY", "test-key-placeholder")
    from investment_team.execution import risk_free_rate as rfr_mod

    monkeypatch.setattr(rfr_mod, "_fetch_fred_dgs3mo", lambda key, timeout=10.0: 0.0525)
    assert get_risk_free_rate() == pytest.approx(0.0525)


def test_get_risk_free_rate_fred_returns_none_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STRATEGY_LAB_RISK_FREE_RATE", raising=False)
    monkeypatch.setenv("FRED_API_KEY", "test-key-placeholder")
    from investment_team.execution import risk_free_rate as rfr_mod

    monkeypatch.setattr(rfr_mod, "_fetch_fred_dgs3mo", lambda key, timeout=10.0: None)
    assert get_risk_free_rate() == RFR_DEFAULT


def test_fetch_fred_dgs3mo_parses_first_valid_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.execution import risk_free_rate as rfr_mod

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "observations": [
                    {"value": "."},
                    {"value": ""},
                    {"value": "non-numeric"},
                    {"value": "5.25"},
                ]
            }

    class _Client:
        def __init__(self, timeout=10.0):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return False

        def get(self, url, params=None):
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "Client", _Client)
    assert rfr_mod._fetch_fred_dgs3mo("test-key-placeholder") == pytest.approx(0.0525)


def test_fetch_fred_dgs3mo_swallows_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.execution import risk_free_rate as rfr_mod

    class _Client:
        def __init__(self, timeout=10.0):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return False

        def get(self, url, params=None):
            raise RuntimeError("network down")

    import httpx

    monkeypatch.setattr(httpx, "Client", _Client)
    assert rfr_mod._fetch_fred_dgs3mo("test-key-placeholder") is None


# ---------------------------------------------------------------------------
# strategy_lab_context
# ---------------------------------------------------------------------------


def test_normalize_asset_class_aliases() -> None:
    for alias in ("equity", "equities", "stock", "etf", "etfs", "ETF", " ETFs "):
        assert normalize_asset_class(alias) == "stocks"
    assert normalize_asset_class("fx") == "forex"
    for alias in ("commodity", "metal", "energy"):
        assert normalize_asset_class(alias) == "commodities"
    for alias in ("crypto", "CRYPTO ", "cryptocurrency", "cryptocurrencies"):
        assert normalize_asset_class(alias) == "crypto"
    assert normalize_asset_class(None) == "stocks"
    assert normalize_asset_class("unknown") == "stocks"


def test_normalize_asset_class_strict_raises_on_unknown() -> None:
    assert normalize_asset_class_strict("etf") == "stocks"
    assert normalize_asset_class_strict("etfs") == "stocks"
    assert normalize_asset_class_strict("cryptocurrency") == "crypto"
    assert normalize_asset_class_strict("cryptocurrencies") == "crypto"
    for alias in ("equities", "fx", "commodity"):
        normalize_asset_class_strict(alias)
    with pytest.raises(ValueError) as exc:
        normalize_asset_class_strict("bonds")
    assert "unknown asset_class" in str(exc.value)


def test_format_prior_results_empty_message() -> None:
    assert format_prior_results([]) == "None yet — this is the first strategy."


def test_format_prior_results_truncates_long_text() -> None:
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    strat = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="stocks",
        hypothesis="x" * 200,
        signal_definition="s",
        timeframe="1d",
    )
    res = BacktestResult(
        total_return_pct=1.0,
        annualized_return_pct=2.0,
        volatility_pct=10.0,
        sharpe_ratio=0.1,
        max_drawdown_pct=1.0,
        win_rate_pct=50.0,
        profit_factor=1.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    bt = BacktestRecord(
        backtest_id="bt",
        strategy_id="s",
        strategy=strat,
        config=BacktestConfig(
            start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
        ),
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=res,
        trades=[],
    )
    rec = StrategyLabRecord(
        lab_record_id="l1",
        strategy=strat,
        backtest=bt,
        is_winning=True,
        strategy_rationale="r" * 300,
        analysis_narrative="n" * 500,
        created_at="2024-01-01T01:00:00Z",
    )
    out = format_prior_results([rec])
    # Truncation markers ("...") were added when each field exceeded the cap.
    assert "..." in out
    # is_publishable defaults to False (legacy/ungated row) and no skip reason
    # was set, so the label reflects "won but not confirmed publishable".
    assert "[WINNING · NOT PUBLISHABLE]" in out


def test_format_prior_results_labels_publishable_winner() -> None:
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    strat = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    res = BacktestResult(
        total_return_pct=1.0,
        annualized_return_pct=10.0,
        volatility_pct=10.0,
        sharpe_ratio=0.1,
        max_drawdown_pct=1.0,
        win_rate_pct=50.0,
        profit_factor=1.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    bt = BacktestRecord(
        backtest_id="bt",
        strategy_id="s",
        strategy=strat,
        config=BacktestConfig(
            start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
        ),
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=res,
        trades=[],
    )
    rec = StrategyLabRecord(
        lab_record_id="l1",
        strategy=strat,
        backtest=bt,
        is_winning=True,
        is_publishable=True,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2024-01-01T01:00:00Z",
    )
    out = format_prior_results([rec])
    assert "[WINNING · PUBLISHABLE]" in out


def test_format_prior_results_labels_winning_with_skip_reason() -> None:
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    strat = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    res = BacktestResult(
        total_return_pct=1.0,
        annualized_return_pct=10.0,
        volatility_pct=10.0,
        sharpe_ratio=0.1,
        max_drawdown_pct=1.0,
        win_rate_pct=50.0,
        profit_factor=1.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    bt = BacktestRecord(
        backtest_id="bt",
        strategy_id="s",
        strategy=strat,
        config=BacktestConfig(
            start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
        ),
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=res,
        trades=[],
    )
    rec = StrategyLabRecord(
        lab_record_id="l1",
        strategy=strat,
        backtest=bt,
        is_winning=True,
        is_publishable=False,
        publishability_skip_reason="realism_failed",
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2024-01-01T01:00:00Z",
    )
    out = format_prior_results([rec])
    assert "[WINNING · NOT PUBLISHABLE (realism_failed)]" in out


def test_format_prior_results_truncates_to_tail() -> None:
    """When more than max_records, only the tail is kept."""
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    def _record(i: int):
        strat = StrategySpec(
            strategy_id=f"s-{i}",
            authored_by="x",
            asset_class="stocks",
            hypothesis=f"h-{i}",
            signal_definition="s",
            timeframe="1d",
        )
        res = BacktestResult(
            total_return_pct=1.0,
            annualized_return_pct=2.0,
            volatility_pct=10.0,
            sharpe_ratio=0.1,
            max_drawdown_pct=1.0,
            win_rate_pct=50.0,
            profit_factor=1.0,
            calmar_ratio=0.0,
            deflated_sharpe=0.0,
            sortino_ratio=0.0,
        )
        bt = BacktestRecord(
            backtest_id=f"bt-{i}",
            strategy_id=f"s-{i}",
            strategy=strat,
            config=BacktestConfig(
                start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
            ),
            submitted_by="x",
            submitted_at="2024-01-01T00:00:00Z",
            completed_at="2024-01-01T01:00:00Z",
            result=res,
            trades=[],
        )
        return StrategyLabRecord(
            lab_record_id=f"l-{i}",
            strategy=strat,
            backtest=bt,
            is_winning=False,
            strategy_rationale="r",
            analysis_narrative="n",
            created_at=f"2024-01-{i + 1:02d}T00:00:00Z",
        )

    out = format_prior_results([_record(i) for i in range(5)], max_records=2)
    # Only the two newest hypotheses appear.
    assert "h-3" in out
    assert "h-4" in out
    assert "h-0" not in out


def test_format_prior_results_labels_mixed_states_in_single_call() -> None:
    """A single render pass over losing / publishable-winning / non-publishable-
    winning records must label each independently — the label attaches to the
    record's own state, not to whichever record rendered first or the raw
    return the records share."""
    records = [
        _attr_record(i=0, annual_return=2.0),  # losing
        _attr_record(i=1, annual_return=10.0, is_publishable=True),  # winning, publishable
        _attr_record(i=2, annual_return=10.0, is_publishable=False),  # winning, not publishable
    ]
    out = format_prior_results(records)
    assert "[LOSING] stocks | h-0" in out, out
    assert "[WINNING · PUBLISHABLE] stocks | h-1" in out, out
    assert "[WINNING · NOT PUBLISHABLE] stocks | h-2" in out, out


# ---------------------------------------------------------------------------
# Prior-results performance attribution (aggregate_prior_results /
# format_prior_attribution + the entry/exit classifiers).
# ---------------------------------------------------------------------------


def _attr_record(
    *,
    i: int = 0,
    asset_class: str = "stocks",
    win_rate: float = 50.0,
    annual_return: float = 2.0,
    entry_rules=None,
    exit_rules=None,
    sizing=None,
    status: str = "completed",
    requires_redesign: bool = False,
    unparsed_rules=None,
    is_publishable: bool = False,
):
    """Build a ``StrategyLabRecord`` with the DSL knobs the attribution buckets on.

    Each varying dimension (asset class, entry/exit/sizing rules, backtest status,
    win rate, annualized return) is a keyword so individual tests vary exactly the
    axis under test and leave the rest at neutral defaults.
    """
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    spec_kwargs: Dict[str, Any] = dict(
        strategy_id=f"s-{i}",
        authored_by="x",
        asset_class=asset_class,
        hypothesis=f"h-{i}",
        signal_definition="s",
        timeframe="1d",
    )
    if entry_rules is not None:
        spec_kwargs["entry_rules"] = entry_rules
    if exit_rules is not None:
        spec_kwargs["exit_rules"] = exit_rules
    if sizing is not None:
        spec_kwargs["sizing"] = sizing
    if requires_redesign:
        spec_kwargs["requires_redesign"] = requires_redesign
    if unparsed_rules is not None:
        spec_kwargs["unparsed_rules"] = unparsed_rules
    strat = StrategySpec(**spec_kwargs)
    res = BacktestResult(
        total_return_pct=1.0,
        annualized_return_pct=annual_return,
        volatility_pct=10.0,
        sharpe_ratio=0.1,
        max_drawdown_pct=1.0,
        win_rate_pct=win_rate,
        profit_factor=1.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    bt = BacktestRecord(
        backtest_id=f"bt-{i}",
        strategy_id=f"s-{i}",
        strategy=strat,
        config=BacktestConfig(
            start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
        ),
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=res,
        trades=[],
        status=status,
    )
    return StrategyLabRecord(
        lab_record_id=f"l-{i}",
        strategy=strat,
        backtest=bt,
        is_winning=annual_return >= 8.0,
        is_publishable=is_publishable,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at=f"2024-01-{i + 1:02d}T00:00:00Z",
    )


def _rsi_entry(threshold: float = 30.0):
    from investment_team.strategy_lab.spec_dsl import EntryRule, IndicatorRef, Predicate

    return EntryRule(
        when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=threshold)
    )


def _sma_crossover_entry():
    from investment_team.strategy_lab.spec_dsl import EntryRule, IndicatorRef, Predicate

    return EntryRule(
        when=Predicate(
            lhs=IndicatorRef(name="sma", params={"period": 20}),
            op="cross_above",
            rhs=IndicatorRef(name="sma", params={"period": 50}),
        )
    )


def _price_level_entry():
    from investment_team.strategy_lab.spec_dsl import EntryRule, Predicate

    return EntryRule(when=Predicate(lhs="bar.close", op=">", rhs=100.0))


def _close_cross_ema_entry():
    # Prompt-recommended breakout form: price literal on the lhs, indicator on
    # the rhs. The indicator family lives on the rhs here.
    from investment_team.strategy_lab.spec_dsl import EntryRule, IndicatorRef, Predicate

    return EntryRule(
        when=Predicate(
            lhs="bar.close", op="cross_above", rhs=IndicatorRef(name="ema", params={"period": 20})
        )
    )


def _ema_cross_sma_entry():
    # Two DIFFERENT indicators in one predicate (ema crosses sma) — exercises the
    # within-predicate '+' join.
    from investment_team.strategy_lab.spec_dsl import EntryRule, IndicatorRef, Predicate

    return EntryRule(
        when=Predicate(
            lhs=IndicatorRef(name="ema", params={"period": 20}),
            op="cross_above",
            rhs=IndicatorRef(name="sma", params={"period": 50}),
        )
    )


def _trailing_stop():
    from investment_team.strategy_lab.spec_dsl import StopLossRule

    return StopLossRule(pct=0.05, basis="trailing_high")


def _fixed_stop():
    from investment_team.strategy_lab.spec_dsl import StopLossRule

    return StopLossRule(pct=0.05, basis="entry_price")


def _take_profit():
    from investment_team.strategy_lab.spec_dsl import TakeProfitRule

    return TakeProfitRule(pct=0.1)


def test_entry_archetype_indicator_vs_price_vs_crossover() -> None:
    rsi = _attr_record(entry_rules=[_rsi_entry()]).strategy
    price = _attr_record(entry_rules=[_price_level_entry()]).strategy
    crossover = _attr_record(entry_rules=[_sma_crossover_entry()]).strategy
    none = _attr_record(entry_rules=[]).strategy

    assert _entry_archetype(rsi) == "rsi"
    assert _entry_archetype(price) == "price_level"
    assert _entry_archetype(crossover) == "sma_crossover"
    assert _entry_archetype(none) == "none"


def test_entry_archetype_rhs_indicator_in_crossover() -> None:
    # `bar.close cross_above ema` keys on the RHS indicator family — it must NOT
    # collapse to a bare price_level bucket and lose the EMA/SMA/VWAP family.
    strat = _attr_record(entry_rules=[_close_cross_ema_entry()]).strategy
    assert _entry_archetype(strat) == "ema_crossover"


def test_entry_archetype_multi_signal_joins_distinct_sorted() -> None:
    multi = _attr_record(entry_rules=[_sma_crossover_entry(), _rsi_entry()]).strategy
    # Distinct per-rule archetypes, sorted, joined with ',' (the inter-rule
    # separator); each rule here has a single archetype.
    assert _entry_archetype(multi) == "rsi,sma_crossover"


def test_entry_archetype_two_separators_disambiguate_predicate_grouping() -> None:
    # An EMA/SMA cross (two indicators in ONE predicate → '+') alongside a
    # separate RSI rule (',' between rules) must stay unambiguous: the '+' binds
    # ema/sma into the crossover, the ',' separates the RSI rule.
    multi = _attr_record(entry_rules=[_ema_cross_sma_entry(), _rsi_entry()]).strategy
    assert _entry_archetype(multi) == "ema+sma_crossover,rsi"


def test_entry_archetype_all_of_tree_names_every_leg() -> None:
    # A multi-confirmation all_of (trend ∧ pullback) must gather the indicator
    # families across ALL legs — not collapse to "unknown" for lack of a
    # top-level lhs/op (the regression Codex flagged).
    from investment_team.strategy_lab.spec_dsl import AllOf, EntryRule, IndicatorRef, Predicate

    entry = EntryRule(
        when=AllOf(
            of=[
                Predicate(
                    lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 200})
                ),
                Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=40.0),
            ]
        )
    )
    strat = _attr_record(entry_rules=[entry]).strategy
    assert _entry_archetype(strat) == "rsi+sma"


def test_entry_archetype_any_of_tree_carries_crossover_suffix() -> None:
    # any_of with one cross leg → families joined, _crossover suffix applied.
    from investment_team.strategy_lab.spec_dsl import AnyOf, EntryRule, IndicatorRef, Predicate

    entry = EntryRule(
        when=AnyOf(
            of=[
                Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30.0),
                Predicate(
                    lhs="bar.close",
                    op="cross_above",
                    rhs=IndicatorRef(name="ema", params={"period": 20}),
                ),
            ]
        )
    )
    strat = _attr_record(entry_rules=[entry]).strategy
    assert _entry_archetype(strat) == "ema+rsi_crossover"


def test_entry_archetype_leaf_predicate_unchanged_after_tree_support() -> None:
    # Regression guard: single-leaf bucketing is byte-identical to the
    # pre-combinator behaviour.
    strat = _attr_record(entry_rules=[_rsi_entry()]).strategy
    assert _entry_archetype(strat) == "rsi"


def test_entry_archetype_none_or_malformed_when_returns_unknown() -> None:
    # The docstring promises graceful degradation for odd shapes: a rule whose
    # ``when`` is ``None`` or a bare dict (neither a Predicate nor a tree) must
    # bucket as "unknown" without raising — guards the robustness contract.
    none_when = type("Rule", (), {"when": None})()
    dict_when = type("Rule", (), {"when": {}})()
    assert _entry_archetype(type("S", (), {"entry_rules": [none_when]})()) == "unknown"
    assert _entry_archetype(type("S", (), {"entry_rules": [dict_when]})()) == "unknown"


def test_exit_archetypes_maps_each_kind_and_basis() -> None:
    trailing = _attr_record(exit_rules=[_trailing_stop()]).strategy
    fixed = _attr_record(exit_rules=[_fixed_stop()]).strategy
    tp = _attr_record(exit_rules=[_take_profit()]).strategy
    none = _attr_record(exit_rules=[]).strategy

    assert _exit_archetypes(trailing) == ["trailing_stop"]
    assert _exit_archetypes(fixed) == ["fixed_stop"]
    assert _exit_archetypes(tp) == ["take_profit"]
    assert _exit_archetypes(none) == ["none"]


def test_is_executed_record_defaults_to_completed_for_legacy() -> None:
    import types

    # A normal record persists status="completed" and counts as executed.
    assert _is_executed_record(_attr_record(status="completed")) is True

    # Legacy records persisted before BacktestRecord.status existed have no
    # status attribute at all; the getattr(..., "completed") fallback must still
    # treat them as executed (backward compatibility).
    legacy = types.SimpleNamespace(backtest=types.SimpleNamespace())
    assert _is_executed_record(legacy) is True

    # A short-circuit status is still correctly excluded.
    assert _is_executed_record(_attr_record(status="failed: design_stalled")) is False


def test_aggregate_prior_results_empty() -> None:
    assert aggregate_prior_results([]) == {}


def test_aggregate_prior_results_single_record_keys_and_means() -> None:
    rec = _attr_record(
        asset_class="crypto",
        win_rate=60.0,
        annual_return=12.0,
        entry_rules=[_rsi_entry()],
        exit_rules=[_trailing_stop()],
    )
    agg = aggregate_prior_results([rec])

    expected_stats = {
        "win_rate": 60.0,
        "annual_return": 12.0,
        "n": 1,
        "publishable_n": 0,
        "publishable_win_rate": None,
        "publishable_annual_return": None,
    }
    assert agg[("asset_class", "crypto")] == expected_stats
    assert agg[("entry", "rsi")] == expected_stats
    assert agg[("exit", "trailing_stop")] == expected_stats
    # Sizing defaults to fixed_fraction on StrategySpec.
    assert agg[("sizing", "fixed_fraction")]["n"] == 1


def test_aggregate_prior_results_publishable_subset_means() -> None:
    """``publishable_*`` fields track a mean over only the ``is_publishable``
    subset of a bucket's contributing records, distinct from the raw mean over
    all of them — so a bucket's apparent (possibly overfit) raw edge never
    hides whether any of it is actually robust."""
    recs = [
        _attr_record(i=0, asset_class="crypto", win_rate=40.0, annual_return=4.0),
        _attr_record(
            i=1, asset_class="crypto", win_rate=80.0, annual_return=20.0, is_publishable=True
        ),
    ]
    agg = aggregate_prior_results(recs)
    bucket = agg[("asset_class", "crypto")]
    assert bucket["n"] == 2
    assert bucket["win_rate"] == pytest.approx(60.0)
    assert bucket["annual_return"] == pytest.approx(12.0)
    assert bucket["publishable_n"] == 1
    assert bucket["publishable_win_rate"] == pytest.approx(80.0)
    assert bucket["publishable_annual_return"] == pytest.approx(20.0)


def test_aggregate_prior_results_publishable_fields_none_when_no_publishable_record() -> None:
    """A bucket with zero publishable contributors reports ``None`` (not 0.0,
    which would misleadingly read as a genuine zero-return robust result)."""
    agg = aggregate_prior_results([_attr_record(i=0, asset_class="forex")])
    bucket = agg[("asset_class", "forex")]
    assert bucket["publishable_n"] == 0
    assert bucket["publishable_win_rate"] is None
    assert bucket["publishable_annual_return"] is None


def test_aggregate_prior_results_averages_within_bucket() -> None:
    recs = [
        _attr_record(i=0, win_rate=40.0, annual_return=4.0, entry_rules=[_rsi_entry()]),
        _attr_record(i=1, win_rate=60.0, annual_return=8.0, entry_rules=[_rsi_entry()]),
    ]
    agg = aggregate_prior_results(recs)
    bucket = agg[("entry", "rsi")]
    assert bucket["n"] == 2
    assert bucket["win_rate"] == pytest.approx(50.0)
    assert bucket["annual_return"] == pytest.approx(6.0)


def test_aggregate_prior_results_distinct_entry_buckets() -> None:
    recs = [
        _attr_record(i=0, entry_rules=[_rsi_entry()]),
        _attr_record(i=1, entry_rules=[_sma_crossover_entry()]),
    ]
    agg = aggregate_prior_results(recs)
    assert ("entry", "rsi") in agg
    assert ("entry", "sma_crossover") in agg
    assert agg[("entry", "rsi")]["n"] == 1
    assert agg[("entry", "sma_crossover")]["n"] == 1


def test_aggregate_prior_results_exit_multi_membership() -> None:
    rec = _attr_record(exit_rules=[_trailing_stop(), _take_profit()])
    agg = aggregate_prior_results([rec])
    # The one record is counted under BOTH exit buckets.
    assert agg[("exit", "trailing_stop")]["n"] == 1
    assert agg[("exit", "take_profit")]["n"] == 1


@pytest.mark.parametrize(
    "non_executed_status",
    [
        "failed: spec_unimplementable",
        "failed: spec_validation",
        "failed: code_synthesis",
        "failed: design_not_ready",
        "failed: design_stalled",
        "failed: budget_exhausted",
    ],
)
def test_aggregate_prior_results_excludes_non_executed_status(non_executed_status: str) -> None:
    # Pre-backtest short-circuit records persist placeholder zero-trade metrics
    # (and a possibly-coerced asset class); none of them must reach attribution.
    recs = [
        _attr_record(i=0, entry_rules=[_rsi_entry()], status="completed"),
        _attr_record(i=1, entry_rules=[_sma_crossover_entry()], status=non_executed_status),
    ]
    agg = aggregate_prior_results(recs)
    assert ("entry", "rsi") in agg
    assert ("entry", "sma_crossover") not in agg


def test_aggregate_prior_results_respects_max_records_tail() -> None:
    recs = [_attr_record(i=i, annual_return=float(i)) for i in range(5)]
    agg = aggregate_prior_results(recs, max_records=2)
    # Only the two newest (i=3, i=4) survive the tail trim → asset_class n == 2.
    assert agg[("asset_class", "stocks")]["n"] == 2
    assert agg[("asset_class", "stocks")]["annual_return"] == pytest.approx(3.5)


def test_aggregate_prior_results_excludes_redesign_pending_design_dims() -> None:
    # A legacy redesign-pending row (prose rules migrated to unparsed_rules, empty
    # entry/exit rules) must NOT make entry:none / exit:none look like a winning
    # archetype. Its genuine asset_class still counts.
    legacy = _attr_record(
        i=0,
        asset_class="crypto",
        annual_return=30.0,
        requires_redesign=True,
        unparsed_rules=["buy when it looks cheap"],
    )
    agg = aggregate_prior_results([legacy])
    assert ("asset_class", "crypto") in agg
    assert ("entry", "none") not in agg
    assert ("exit", "none") not in agg
    assert not any(dim == "sizing" for dim, _ in agg)


def test_aggregate_prior_results_unparsed_rules_alone_excludes_design_dims() -> None:
    # unparsed_rules present without requires_redesign is enough to skip the
    # structured dimensions.
    legacy = _attr_record(i=0, unparsed_rules=["sell on a hunch"], entry_rules=[_rsi_entry()])
    agg = aggregate_prior_results([legacy])
    assert ("asset_class", "stocks") in agg
    assert ("entry", "rsi") not in agg


def test_aggregate_prior_results_filters_before_tail_trim() -> None:
    # An older executed run followed by a window-full of recent pre-backtest
    # short-circuits. Filtering must precede the tail-trim, else the executed
    # evidence is hidden behind the recent short-circuits and attribution is
    # empty (regression: slice-before-filter returned {}).
    recs = [_attr_record(i=0, entry_rules=[_rsi_entry()], status="completed")]
    recs += [_attr_record(i=j, status="failed: design_not_ready") for j in range(1, 5)]
    agg = aggregate_prior_results(recs, max_records=2)
    assert ("entry", "rsi") in agg
    assert agg[("asset_class", "stocks")]["n"] == 1


def test_aggregate_prior_results_max_records_zero_yields_empty() -> None:
    # max_records=0 must mean "keep none", not slip through ordered[-0:] (the
    # whole list). Guards the contract that 0 → {}.
    recs = [_attr_record(i=i, entry_rules=[_rsi_entry()]) for i in range(3)]
    assert aggregate_prior_results(recs, max_records=0) == {}


def test_aggregate_prior_results_cache_reused_across_calls_and_windows() -> None:
    # A shared cache dict must let a second call against the same ``records``
    # object reuse the sorted/filtered pass instead of recomputing it — even
    # when the two calls use different ``max_records`` windows.
    recs = [_attr_record(i=i, entry_rules=[_rsi_entry()], annual_return=float(i)) for i in range(5)]
    cache: dict = {}
    first = aggregate_prior_results(recs, max_records=5, cache=cache)
    assert len(cache) == 1
    second = aggregate_prior_results(recs, max_records=2, cache=cache)
    # Same cache entry reused (no second sort/filter pass), no new key added.
    assert len(cache) == 1
    # Cached (pre-trim) results are identical to the uncached computation.
    assert first == aggregate_prior_results(recs, max_records=5)
    assert second == aggregate_prior_results(recs, max_records=2)


def test_format_prior_attribution_empty_sentinel() -> None:
    out = format_prior_attribution([])
    assert "Not enough" in out


def test_format_prior_attribution_shows_sample_size_and_groups() -> None:
    recs = [
        _attr_record(
            i=0, asset_class="crypto", win_rate=58.0, annual_return=11.0, entry_rules=[_rsi_entry()]
        ),
        _attr_record(
            i=1,
            asset_class="stocks",
            win_rate=41.0,
            annual_return=3.0,
            entry_rules=[_sma_crossover_entry()],
        ),
    ]
    out = format_prior_attribution(recs)
    # Every rendered bucket line carries its sample size.
    assert "n=" in out
    # All four dimension group headers are present (records have entry/exit/sizing
    # populated, so no dimension is empty).
    assert "Asset class" in out
    assert "Entry archetype" in out
    assert "Exit type" in out
    assert "Position sizing" in out
    # Thin (n=1) buckets are flagged so the model discounts them.
    assert "(thin sample)" in out


def test_format_prior_attribution_all_redesign_pending_renders_asset_class_only() -> None:
    # When every executed record is redesign-pending, the structured design
    # dimensions are suppressed and only the genuine asset_class section renders.
    recs = [
        _attr_record(
            i=0,
            asset_class="crypto",
            annual_return=12.0,
            requires_redesign=True,
            unparsed_rules=["buy when it looks cheap"],
        ),
    ]
    out = format_prior_attribution(recs)
    assert "Asset class" in out
    assert "crypto" in out
    assert "Entry archetype" not in out
    assert "Exit type" not in out
    assert "Position sizing" not in out


def test_format_prior_attribution_rejects_non_positive_thin_n() -> None:
    rec = _attr_record(entry_rules=[_rsi_entry()])
    with pytest.raises(AssertionError):
        format_prior_attribution([rec], thin_n=0)


def test_asset_class_mix_hint_empty_records() -> None:
    out = asset_class_mix_hint([])
    assert "No prior lab strategies" in out


def test_asset_class_mix_hint_warns_when_stocks_overrepresented() -> None:
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    def _record(i: int, asset_class: str):
        strat = StrategySpec(
            strategy_id=f"s-{i}",
            authored_by="x",
            asset_class=asset_class,
            hypothesis=f"h-{i}",
            signal_definition="s",
            timeframe="1d",
        )
        res = BacktestResult(
            total_return_pct=1.0,
            annualized_return_pct=2.0,
            volatility_pct=10.0,
            sharpe_ratio=0.1,
            max_drawdown_pct=1.0,
            win_rate_pct=50.0,
            profit_factor=1.0,
            calmar_ratio=0.0,
            deflated_sharpe=0.0,
            sortino_ratio=0.0,
        )
        bt = BacktestRecord(
            backtest_id=f"bt-{i}",
            strategy_id=f"s-{i}",
            strategy=strat,
            config=BacktestConfig(
                start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
            ),
            submitted_by="x",
            submitted_at="2024-01-01T00:00:00Z",
            completed_at="2024-01-01T01:00:00Z",
            result=res,
            trades=[],
        )
        return StrategyLabRecord(
            lab_record_id=f"l-{i}",
            strategy=strat,
            backtest=bt,
            is_winning=False,
            strategy_rationale="r",
            analysis_narrative="n",
            created_at=f"2024-01-{i + 1:02d}T00:00:00Z",
        )

    records = [_record(i, "stocks") for i in range(5)]
    out = asset_class_mix_hint(records)
    assert "Equities are relatively heavy" in out


def test_asset_class_mix_hint_falls_back_to_stocks_for_unknown_class() -> None:
    """Records with unrecognised asset_class still funnel into the stocks count."""
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    strat = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="crypto",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    res = BacktestResult(
        total_return_pct=1.0,
        annualized_return_pct=2.0,
        volatility_pct=10.0,
        sharpe_ratio=0.1,
        max_drawdown_pct=1.0,
        win_rate_pct=50.0,
        profit_factor=1.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    bt = BacktestRecord(
        backtest_id="bt",
        strategy_id="s",
        strategy=strat,
        config=BacktestConfig(
            start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
        ),
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=res,
        trades=[],
    )
    rec = StrategyLabRecord(
        lab_record_id="l",
        strategy=strat,
        backtest=bt,
        is_winning=False,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2024-01-01T01:00:00Z",
    )
    out = asset_class_mix_hint([rec])
    assert "Underrepresented" in out


# ---------------------------------------------------------------------------
# ConvergenceTracker
# ---------------------------------------------------------------------------


def _spec(asset_class: str = "stocks"):
    from investment_team.models import StrategySpec

    return StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class=asset_class,
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )


def _gate(name: str, passed: bool = True) -> QualityGateResult:
    return QualityGateResult(
        gate_name=name,
        passed=passed,
        details="",
        severity="critical" if not passed else "info",
        phase="design",
    )


def test_convergence_tracker_record_and_directives() -> None:
    tracker = ConvergenceTracker(window_size=3, max_history=4)
    # Five identical strategies → max_history trimming kicks in.
    for _ in range(5):
        tracker.record(_spec("stocks"), [_gate("backtest_quality", passed=False)])

    assert len(tracker._signatures) == 4  # trimmed to max_history
    # Stocks dominates → diversity directive surfaces.
    directive = tracker.get_diversity_directive()
    assert directive is not None
    assert "stocks" in directive

    # Failure directives fire when a mode passes the threshold.
    directives = tracker.get_failure_directives(min_occurrences=2)
    assert any("backtest_quality" in d for d in directives)


def test_convergence_tracker_no_diversity_directive_for_balanced_history() -> None:
    tracker = ConvergenceTracker()
    for ac in ("stocks", "crypto", "forex"):
        tracker.record(_spec(ac), [])
    assert tracker.get_diversity_directive() is None


def test_convergence_tracker_no_directive_when_history_too_short() -> None:
    tracker = ConvergenceTracker()
    tracker.record(_spec(), [])
    tracker.record(_spec(), [])
    assert tracker.get_diversity_directive() is None


def test_convergence_tracker_stall_detection() -> None:
    tracker = ConvergenceTracker(window_size=3)
    # Three identical strategies → stalled.
    for _ in range(3):
        tracker.record(_spec(), [])
    assert tracker.is_stalled(threshold=0.5) is True
    assert tracker.get_stall_directive() is not None

    # Diverse strategies → not stalled.
    tracker2 = ConvergenceTracker(window_size=3)
    for ac in ("stocks", "crypto", "forex"):
        tracker2.record(_spec(ac), [])
    assert tracker2.is_stalled(threshold=0.99) is False
    assert tracker2.get_stall_directive() is None


def test_convergence_tracker_trial_counter() -> None:
    tracker = ConvergenceTracker()
    assert tracker.trial_count == 0
    tracker.increment_trials(3)
    assert tracker.trial_count == 3
    with pytest.raises(ValueError):
        tracker.increment_trials(-1)


def test_convergence_tracker_snapshot_and_merge_from() -> None:
    primary = ConvergenceTracker()
    primary.increment_trials(2)
    snap = primary.snapshot()
    snap.increment_trials(5)

    primary.merge_from(snap)
    assert primary.trial_count == 2 + 5


def test_convergence_tracker_merge_from_raises_on_negative_delta() -> None:
    primary = ConvergenceTracker()
    primary.increment_trials(5)
    snap = primary.snapshot()
    # Forcibly walk back the snapshot's trial_count.
    snap._trial_count = 2
    with pytest.raises(ValueError):
        primary.merge_from(snap)


def test_jaccard_helper() -> None:
    assert _jaccard(set(), set()) == 1.0
    assert _jaccard({"a"}, {"a"}) == 1.0
    assert _jaccard({"a"}, {"b"}) == 0.0
    assert _jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# model_factory
# ---------------------------------------------------------------------------


def test_get_strands_model_dummy_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """``LLM_PROVIDER=dummy`` is unsupported in Strands paths."""
    from investment_team.strategy_lab.agents import model_factory

    monkeypatch.setattr(model_factory, "resolve_provider", lambda: "dummy")
    monkeypatch.setattr(model_factory, "resolve_model", lambda key: "llama-3.1")
    monkeypatch.setattr(model_factory, "resolve_base_url", lambda: "http://example.com")
    with pytest.raises(ValueError) as exc:
        model_factory.get_strands_model("strategy_ideation")
    assert "dummy" in str(exc.value).lower()


@pytest.mark.parametrize("bad_key", ["", "   ", None])
def test_get_strands_model_rejects_empty_agent_key(
    monkeypatch: pytest.MonkeyPatch, bad_key
) -> None:
    """The documented ``agent_key`` precondition is enforced: an empty/blank key
    raises ``ValueError`` rather than failing obscurely downstream."""
    from investment_team.strategy_lab.agents import model_factory

    monkeypatch.setattr(model_factory, "resolve_provider", lambda: "ollama")
    with pytest.raises(ValueError, match="agent_key must be a non-empty string"):
        model_factory.get_strands_model(bad_key)


def test_get_strands_model_unsupported_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown ``LLM_PROVIDER`` fails fast instead of silently routing to Ollama."""
    from investment_team.strategy_lab.agents import model_factory

    monkeypatch.setattr(model_factory, "resolve_provider", lambda: "openai")
    monkeypatch.setattr(model_factory, "resolve_model", lambda key: "gpt-4")
    monkeypatch.setattr(model_factory, "resolve_base_url", lambda: "http://example.com")
    with pytest.raises(ValueError, match="Unsupported LLM_PROVIDER"):
        model_factory.get_strands_model("strategy_ideation")


def test_get_strands_model_bedrock_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.strategy_lab.agents import model_factory

    monkeypatch.setattr(model_factory, "resolve_provider", lambda: "bedrock")
    monkeypatch.setattr(
        model_factory, "resolve_model", lambda key: "anthropic.claude-3-haiku-20240307-v1:0"
    )
    monkeypatch.setattr(model_factory, "resolve_base_url", lambda: "")
    monkeypatch.setenv("STRATEGY_LAB_LLM_TIMEOUT", "77")

    # Declare ``boto_client_config`` explicitly so the factory's introspection
    # forwards the transport timeout through it (the real strands BedrockModel
    # signature shape).
    class _StubBedrock:
        def __init__(self, *, model_id=None, boto_client_config=None):
            self.model_id = model_id
            self.boto_client_config = boto_client_config

    import strands.models as strands_models

    monkeypatch.setattr(strands_models, "BedrockModel", _StubBedrock)
    result = model_factory.get_strands_model()
    assert isinstance(result, _StubBedrock)
    assert result.model_id == "anthropic.claude-3-haiku-20240307-v1:0"
    assert result.boto_client_config is not None
    # The timeout is forwarded as a float verbatim (no int() truncation).
    assert result.boto_client_config.read_timeout == 77.0


def test_get_strands_model_ollama_cloud_without_env_key_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keyless-looking env is NOT a fail-fast trigger: Ollama Cloud auth is
    resolved by ``llm_service.factory.get_client`` from the Postgres-backed
    provider list (each entry carries its own key, with no environment
    fallback), so ``get_strands_model`` must not reject this configuration by
    checking ``OLLAMA_API_KEY``/``LLM_OLLAMA_API_KEY`` against
    ``resolve_base_url()`` — that would diverge from ``get_client``'s actual
    resolution and misfire against a correctly configured deployment (the
    provider-list entry's key lives in Postgres, never in these env vars)."""
    import llm_service.strands_adapter as adapter
    from investment_team.strategy_lab.agents import model_factory

    monkeypatch.setattr(model_factory, "resolve_provider", lambda: "ollama")
    monkeypatch.setattr(model_factory, "resolve_model", lambda key: "llama3")
    monkeypatch.setattr(model_factory, "resolve_base_url", lambda: "https://ollama.com")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("LLM_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)

    sentinel = object()
    monkeypatch.setattr(adapter, "_get_strands_model", lambda **_kw: sentinel)

    assert model_factory.get_strands_model("x") is sentinel


def _patch_ollama_llm_service(monkeypatch: pytest.MonkeyPatch):
    """Wire model_factory to the Ollama branch and capture the llm_service routing.

    The default Ollama path delegates to
    ``llm_service.strands_adapter._get_strands_model`` (the hardened path). This
    patches that source attribute with a recorder so tests can assert what
    ``get_strands_model`` forwards. ``get_strands_model`` imports the adapter
    lazily (``from llm_service.strands_adapter import _get_strands_model``) at
    call time, so patching the module attribute is picked up.

    Returns ``(model_factory, recorder)`` where ``recorder.calls`` is a list of
    the kwargs each routed call forwarded.
    """
    import llm_service.strands_adapter as adapter
    from investment_team.strategy_lab.agents import model_factory

    monkeypatch.setattr(model_factory, "resolve_provider", lambda: "ollama")
    monkeypatch.setattr(model_factory, "resolve_model", lambda key: "llama3")
    monkeypatch.setattr(model_factory, "resolve_base_url", lambda: "http://localhost:11434")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("LLM_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list = []

        def __call__(self, *, agent_key=None, response_format="json", **kw):
            self.calls.append({"agent_key": agent_key, "response_format": response_format, **kw})
            return ("LLM_SERVICE_MODEL", agent_key, response_format)

    recorder = _Recorder()
    monkeypatch.setattr(adapter, "_get_strands_model", recorder)
    return model_factory, recorder


def test_get_strands_model_ollama_routes_through_llm_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default Ollama path delegates to the hardened llm_service adapter
    (carrying the empty/thinking-only-response handling) instead of building a
    native strands ``OllamaModel``."""
    model_factory, recorder = _patch_ollama_llm_service(monkeypatch)

    result = model_factory.get_strands_model()

    assert recorder.calls == [
        {"agent_key": "strategy_ideation", "response_format": "json", "temperature": 0.0}
    ]
    assert result == ("LLM_SERVICE_MODEL", "strategy_ideation", "json")


def test_get_strands_model_forwards_response_format_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``response_format="text"`` (used by code synthesis, which emits a raw
    Python file) is forwarded to the llm_service adapter so the turn is not
    routed through ``json_object`` mode."""
    model_factory, recorder = _patch_ollama_llm_service(monkeypatch)

    model_factory.get_strands_model("strategy_code_synthesis", response_format="text")

    assert recorder.calls == [
        {"agent_key": "strategy_code_synthesis", "response_format": "text", "temperature": 0.0}
    ]


def test_get_strands_model_rejects_invalid_response_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown ``response_format`` is a caller bug and raises ``ValueError``."""
    model_factory, _ = _patch_ollama_llm_service(monkeypatch)

    with pytest.raises(ValueError, match="response_format must be"):
        model_factory.get_strands_model("strategy_design", response_format="xml")


def test_resolve_strands_timeout_garbage_value_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric ``STRATEGY_LAB_LLM_TIMEOUT`` falls back to ``resolve_timeout``."""
    from investment_team.strategy_lab.agents import model_factory

    monkeypatch.setenv("STRATEGY_LAB_LLM_TIMEOUT", "not-a-number")
    monkeypatch.setattr(model_factory, "resolve_timeout", lambda key: 321.0)

    assert model_factory._resolve_strands_timeout("strategy_design") == 321.0


@pytest.mark.parametrize("bad", ["-5", "0", "-0.5", "inf", "-inf", "nan"])
def test_resolve_strands_timeout_non_positive_or_nonfinite_value_falls_back(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """A cleanly-parsing but non-positive OR non-finite timeout is a
    misconfiguration and falls back (``inf > 0`` is ``True`` but an infinite read
    timeout would never cancel a hung call)."""
    from investment_team.strategy_lab.agents import model_factory

    monkeypatch.setenv("STRATEGY_LAB_LLM_TIMEOUT", bad)
    monkeypatch.setattr(model_factory, "resolve_timeout", lambda key: 321.0)

    assert model_factory._resolve_strands_timeout("strategy_design") == 321.0


@pytest.mark.parametrize(
    "bad_resolved",
    [0.0, -1.0, float("inf"), float("nan"), "not-a-number", None, True],
)
def test_resolve_strands_timeout_guards_resolve_timeout_postcondition(
    monkeypatch: pytest.MonkeyPatch, bad_resolved: object
) -> None:
    """A non-positive, non-finite, or non-numeric ``resolve_timeout`` result
    falls back to the default, so the function's positive-finite postcondition
    holds unconditionally (a non-numeric return must NOT raise ``TypeError``)."""
    from investment_team.strategy_lab.agents import model_factory

    monkeypatch.delenv("STRATEGY_LAB_LLM_TIMEOUT", raising=False)
    monkeypatch.setattr(model_factory, "resolve_timeout", lambda key: bad_resolved)

    result = model_factory._resolve_strands_timeout("strategy_design")
    assert result == model_factory._DEFAULT_TRANSPORT_TIMEOUT
    assert result > 0 and math.isfinite(result)


@pytest.mark.parametrize("bad", [-1.0, 0.0, float("inf"), float("nan")])
def test_get_strands_model_rejects_non_positive_or_nonfinite_explicit_timeout(
    monkeypatch: pytest.MonkeyPatch, bad: float
) -> None:
    """An explicit non-positive/non-finite ``timeout`` raises ``ValueError`` — an
    explicit ``raise`` (not ``assert``) so the guard survives ``python -O``."""
    model_factory, _ = _patch_ollama_llm_service(monkeypatch)

    with pytest.raises(ValueError, match="positive, finite"):
        model_factory.get_strands_model("strategy_design", timeout=bad)


@pytest.mark.parametrize("bad", ["900", True, [900]])
def test_get_strands_model_rejects_non_numeric_explicit_timeout(
    monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    """A non-numeric explicit ``timeout`` raises a clear ``TypeError`` at the
    boundary rather than an obscure one from ``math.isfinite`` (``bool`` counts
    as non-numeric here — ``True``/``False`` are not meaningful timeouts)."""
    model_factory, _ = _patch_ollama_llm_service(monkeypatch)

    with pytest.raises(TypeError, match="must be a number"):
        model_factory.get_strands_model("strategy_design", timeout=bad)


def test_get_strands_model_explicit_timeout_forwarded_to_adapter_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid explicit ``timeout`` on the Ollama path is honoured: the factory
    builds a dedicated OllamaLLMClient carrying that read timeout and hands it to
    the adapter via ``client=`` (rather than silently dropping it)."""
    model_factory, recorder = _patch_ollama_llm_service(monkeypatch)

    model_factory.get_strands_model("strategy_design", timeout=45.0)

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["agent_key"] == "strategy_design"
    assert call["response_format"] == "json"
    assert call["temperature"] == 0.6
    client = call["client"]
    assert client.timeout == 45.0
    assert client.model == "llama3"


def test_get_strands_model_no_explicit_timeout_omits_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit timeout, no ``client=`` is forwarded — the adapter's
    default (cached) client owns the transport timeout."""
    model_factory, recorder = _patch_ollama_llm_service(monkeypatch)

    model_factory.get_strands_model("strategy_design")

    assert recorder.calls == [
        {"agent_key": "strategy_design", "response_format": "json", "temperature": 0.6}
    ]


def test_get_strands_model_bedrock_carries_no_additional_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Bedrock branch constructs a native BedrockModel with no ``format`` /
    ``additional_args`` constraint."""
    from investment_team.strategy_lab.agents import model_factory

    monkeypatch.setattr(model_factory, "resolve_provider", lambda: "bedrock")
    monkeypatch.setattr(model_factory, "resolve_model", lambda key: "anthropic.claude-3-haiku")
    monkeypatch.setattr(model_factory, "resolve_base_url", lambda: "")

    class _StubBedrock:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    import strands.models as strands_models

    monkeypatch.setattr(strands_models, "BedrockModel", _StubBedrock)
    result = model_factory.get_strands_model()

    assert "additional_args" not in result.kwargs
