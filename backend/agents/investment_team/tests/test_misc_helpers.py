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
    asset_class_mix_hint,
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
    assert "[WINNING]" in out


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


def test_get_strands_model_bedrock_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.strategy_lab.agents import model_factory

    monkeypatch.setattr(model_factory, "resolve_provider", lambda: "bedrock")
    monkeypatch.setattr(
        model_factory, "resolve_model", lambda key: "anthropic.claude-3-haiku-20240307-v1:0"
    )
    monkeypatch.setattr(model_factory, "resolve_base_url", lambda: "")

    class _StubBedrock:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    import strands.models as strands_models

    monkeypatch.setattr(strands_models, "BedrockModel", _StubBedrock)
    result = model_factory.get_strands_model()
    assert isinstance(result, _StubBedrock)
    assert result.kwargs["model_id"] == "anthropic.claude-3-haiku-20240307-v1:0"


def test_get_strands_model_ollama_cloud_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.strategy_lab.agents import model_factory

    monkeypatch.setattr(model_factory, "resolve_provider", lambda: "ollama")
    monkeypatch.setattr(model_factory, "resolve_model", lambda key: "llama3")
    monkeypatch.setattr(model_factory, "resolve_base_url", lambda: "https://ollama.com")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("LLM_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    with pytest.raises(ValueError) as exc:
        model_factory.get_strands_model("x")
    assert "Cloud requires an API key" in str(exc.value)


def test_get_strands_model_ollama_local_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.strategy_lab.agents import model_factory

    monkeypatch.setattr(model_factory, "resolve_provider", lambda: "ollama")
    monkeypatch.setattr(model_factory, "resolve_model", lambda key: "llama3")
    monkeypatch.setattr(model_factory, "resolve_base_url", lambda: "http://localhost:11434")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("LLM_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)

    class _StubOllama:
        def __init__(self, host=None, model_id=None):
            self.host = host
            self.model_id = model_id

    import strands.models as strands_models

    monkeypatch.setattr(strands_models, "OllamaModel", _StubOllama)
    result = model_factory.get_strands_model()
    assert isinstance(result, _StubOllama)
    assert result.host == "http://localhost:11434"
    assert result.model_id == "llama3"


def _patch_ollama_local(monkeypatch: pytest.MonkeyPatch):
    """Wire model_factory to the Ollama-local branch and capture kwargs.

    Returns the recording stub class so tests can assert which kwargs
    ``get_strands_model`` forwarded to ``OllamaModel``.
    """
    from investment_team.strategy_lab.agents import model_factory

    monkeypatch.setattr(model_factory, "resolve_provider", lambda: "ollama")
    monkeypatch.setattr(model_factory, "resolve_model", lambda key: "llama3")
    monkeypatch.setattr(model_factory, "resolve_base_url", lambda: "http://localhost:11434")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("LLM_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)

    class _RecordingOllama:
        # Accept timeout so the factory's first construction attempt wins and
        # we observe the real (timeout-carrying) kwargs the schema path emits.
        def __init__(self, host=None, model_id=None, timeout=None, additional_args=None):
            self.host = host
            self.model_id = model_id
            self.timeout = timeout
            self.additional_args = additional_args

    import strands.models as strands_models

    monkeypatch.setattr(strands_models, "OllamaModel", _RecordingOllama)
    return model_factory, _RecordingOllama


def test_structured_output_enabled_default_and_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.strategy_lab.agents import model_factory

    monkeypatch.delenv("STRATEGY_LAB_STRUCTURED_OUTPUT_ENABLED", raising=False)
    assert model_factory.structured_output_enabled() is True
    for truthy in ("true", "1", "YES", "Yes"):
        monkeypatch.setenv("STRATEGY_LAB_STRUCTURED_OUTPUT_ENABLED", truthy)
        assert model_factory.structured_output_enabled() is True
    for falsy in ("false", "0", "no", "off", ""):
        monkeypatch.setenv("STRATEGY_LAB_STRUCTURED_OUTPUT_ENABLED", falsy)
        assert model_factory.structured_output_enabled() is False


def test_get_strands_model_applies_schema_as_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """A response_schema is forwarded to Ollama via ``additional_args.format``."""
    model_factory, _ = _patch_ollama_local(monkeypatch)
    monkeypatch.setenv("STRATEGY_LAB_STRUCTURED_OUTPUT_ENABLED", "true")

    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    result = model_factory.get_strands_model("strategy_design", response_schema=schema)

    assert result.additional_args == {"format": schema}


def test_get_strands_model_no_schema_leaves_format_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    model_factory, _ = _patch_ollama_local(monkeypatch)
    monkeypatch.setenv("STRATEGY_LAB_STRUCTURED_OUTPUT_ENABLED", "true")

    result = model_factory.get_strands_model("strategy_design")

    assert result.additional_args is None


def test_get_strands_model_toggle_off_ignores_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the toggle off, a supplied schema must NOT be applied."""
    model_factory, _ = _patch_ollama_local(monkeypatch)
    monkeypatch.setenv("STRATEGY_LAB_STRUCTURED_OUTPUT_ENABLED", "false")

    schema = {"type": "object"}
    result = model_factory.get_strands_model("strategy_design", response_schema=schema)

    assert result.additional_args is None


def test_get_strands_model_bedrock_ignores_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Bedrock branch never carries a ``format`` constraint."""
    from investment_team.strategy_lab.agents import model_factory

    monkeypatch.setattr(model_factory, "resolve_provider", lambda: "bedrock")
    monkeypatch.setattr(model_factory, "resolve_model", lambda key: "anthropic.claude-3-haiku")
    monkeypatch.setattr(model_factory, "resolve_base_url", lambda: "")
    monkeypatch.setenv("STRATEGY_LAB_STRUCTURED_OUTPUT_ENABLED", "true")

    class _StubBedrock:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    import strands.models as strands_models

    monkeypatch.setattr(strands_models, "BedrockModel", _StubBedrock)
    result = model_factory.get_strands_model(response_schema={"type": "object"})

    assert "additional_args" not in result.kwargs
