import pytest
from agents.investment_team.agents import AgentIdentity, PolicyGuardianAgent, PromotionGateAgent
from agents.investment_team.models import (
    IPS,
    IncomeProfile,
    InvestmentProfile,
    LiquidityNeeds,
    NetWorth,
    PlannedLargeExpense,
    PortfolioConstraints,
    PortfolioPosition,
    PortfolioProposal,
    PromotionStage,
    RiskTolerance,
    SavingsRate,
    StrategySpec,
    TaxProfile,
    UserGoal,
    UserPreferences,
    ValidationCheck,
    ValidationReport,
    ValidationStatus,
)
from agents.investment_team.orchestrator import InvestmentTeamOrchestrator, WorkflowState
from agents.investment_team.tool_agents.web_interfaces import (
    BrowserType,
    InvestmentWebInterfaceCoordinator,
    WebAgentConfig,
)


def _sample_ips() -> IPS:
    profile = InvestmentProfile(
        user_id="u1",
        created_at="2026-01-01T00:00:00Z",
        risk_tolerance=RiskTolerance.MEDIUM,
        max_drawdown_tolerance_pct=20,
        time_horizon_years=10,
        liquidity_needs=LiquidityNeeds(
            emergency_fund_months=6,
            planned_large_expenses=[PlannedLargeExpense(name="car", amount=10000, date="2027-01-01T00:00:00Z")],
        ),
        income=IncomeProfile(annual_gross=120000, stability="stable"),
        net_worth=NetWorth(total=300000, investable_assets=200000),
        savings_rate=SavingsRate(monthly=2000, annual=24000),
        tax_profile=TaxProfile(country="US", state="CA", account_types=["taxable"]),
        preferences=UserPreferences(),
        goals=[UserGoal(name="retire", target_amount=1_000_000, target_date="2035-01-01T00:00:00Z", priority="high")],
        constraints=PortfolioConstraints(
            max_single_position_pct=10,
            max_asset_class_pct={"equities": 70, "crypto": 10, "options": 10},
        ),
    )
    return IPS(profile=profile, live_trading_enabled=True, human_approval_required_for_live=True)


def _sample_validation() -> ValidationReport:
    return ValidationReport(
        strategy_id="s1",
        generated_by="validator",
        data_snapshot_id="snap-1",
        backtest_period="2018-2025",
        scenario_set=["rates_up", "risk_off"],
        checks=[
            ValidationCheck(name="backtest_quality", status=ValidationStatus.PASS, details="ok"),
            ValidationCheck(name="walk_forward", status=ValidationStatus.PASS, details="ok"),
            ValidationCheck(name="stress_test", status=ValidationStatus.PASS, details="ok"),
            ValidationCheck(name="transaction_cost_model", status=ValidationStatus.PASS, details="ok"),
            ValidationCheck(name="liquidity_impact", status=ValidationStatus.PASS, details="ok"),
        ],
    )


def test_policy_guardian_checks_aggregate_caps_and_speculative_sleeve() -> None:
    ips = _sample_ips()
    proposal = PortfolioProposal(
        proposal_id="p1",
        prepared_by="designer",
        ips_version="1.0",
        data_snapshot_id="snap-1",
        objective="balanced",
        positions=[
            PortfolioPosition(symbol="BTC", asset_class="crypto", weight_pct=8, rationale="alpha"),
            PortfolioPosition(symbol="ETH", asset_class="crypto", weight_pct=6, rationale="alpha"),
        ],
    )

    violations = PolicyGuardianAgent().check_portfolio(ips, proposal)

    assert any("Asset class crypto exceeds cap" in item for item in violations)
    assert any("Speculative sleeve exceeds IPS cap" in item for item in violations)


def test_promotion_gate_requires_human_approval_for_live() -> None:
    ips = _sample_ips()
    strategy = StrategySpec(strategy_id="s1", authored_by="research", asset_class="equities", hypothesis="h", signal_definition="s")
    decision = PromotionGateAgent().decide(
        strategy=strategy,
        validation=_sample_validation(),
        ips=ips,
        proposer_agent_id="proposer-1",
        approver=AgentIdentity(agent_id="approver-1", role="promotion_gate", version="1.0"),
        risk_veto=False,
        human_live_approval=False,
    )

    assert decision.outcome == PromotionStage.PAPER
    assert any(g.gate.value == "human_approval" for g in decision.gate_results)


def test_orchestrator_degrades_to_monitor_only_on_integrity_failure() -> None:
    state = WorkflowState()
    orch = InvestmentTeamOrchestrator()

    orch.handle_data_integrity(state, integrity_ok=False)

    assert state.mode.value == "monitor_only"


def test_promotion_gate_revises_when_validation_strategy_mismatch() -> None:
    ips = _sample_ips()
    strategy = StrategySpec(strategy_id="s1", authored_by="research", asset_class="equities", hypothesis="h", signal_definition="s")
    validation = _sample_validation()
    validation.strategy_id = "other"

    decision = PromotionGateAgent().decide(
        strategy=strategy,
        validation=validation,
        ips=ips,
        proposer_agent_id="proposer-1",
        approver=AgentIdentity(agent_id="approver-1", role="promotion_gate", version="1.0"),
        risk_veto=False,
    )

    assert decision.outcome == PromotionStage.REVISE
    assert any("strategy_id does not match" in action for action in decision.required_actions)


def test_policy_guardian_rejects_excluded_asset_class() -> None:
    ips = _sample_ips()
    ips.profile.preferences.excluded_asset_classes = ["crypto"]
    proposal = PortfolioProposal(
        proposal_id="p2",
        prepared_by="designer",
        ips_version="1.0",
        data_snapshot_id="snap-1",
        objective="balanced",
        positions=[PortfolioPosition(symbol="BTC", asset_class="crypto", weight_pct=5, rationale="alpha")],
    )

    violations = PolicyGuardianAgent().check_portfolio(ips, proposal)

    assert any("excluded by IPS preferences" in item for item in violations)


def test_web_interface_coordinator_selects_provider_and_runs_action() -> None:
    coordinator = InvestmentWebInterfaceCoordinator(
        provider="quantconnect",
        config=WebAgentConfig(browser=BrowserType.FIREFOX, workspace_name="alpha-lab"),
    )

    result = coordinator.execute_action(action="deploy_strategy", payload={"strategy_id": "s1"})

    assert result["provider"] == "quantconnect"
    assert result["results"]["login"]["details"]["browser"] == "firefox"
    assert result["results"]["open_workspace"]["details"]["workspace"] == "alpha-lab"
    assert result["artifacts"][0]["action"] == "deploy_strategy"


def test_orchestrator_web_action_uses_optional_coordinator() -> None:
    coordinator = InvestmentWebInterfaceCoordinator(
        provider="tradingview",
        config=WebAgentConfig(browser=BrowserType.CHROMIUM),
    )
    orch = InvestmentTeamOrchestrator(web_interface_coordinator=coordinator)

    result = orch.run_web_action(action="capture_chart", payload={"symbol": "SPY"}, workspace_name="swing")

    assert result["provider"] == "tradingview"
    assert result["results"]["open_workspace"]["details"]["workspace"] == "swing"


def test_orchestrator_web_action_requires_configured_coordinator() -> None:
    orch = InvestmentTeamOrchestrator()

    try:
        orch.run_web_action(action="noop")
    except RuntimeError as exc:
        assert "not configured" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when coordinator missing")


def test_web_interface_coordinator_returns_run_scoped_artifacts() -> None:
    coordinator = InvestmentWebInterfaceCoordinator(
        provider="quantconnect",
        config=WebAgentConfig(browser=BrowserType.CHROMIUM),
    )

    first = coordinator.execute_action(action="deploy_strategy", payload={"strategy_id": "s1"})
    second = coordinator.execute_action(action="deploy_strategy", payload={"strategy_id": "s2"})

    assert len(first["artifacts"]) == 1
    assert first["artifacts"][0]["payload"]["strategy_id"] == "s1"
    assert len(second["artifacts"]) == 1
    assert second["artifacts"][0]["payload"]["strategy_id"] == "s2"


def test_web_interface_coordinator_accepts_string_browser_config() -> None:
    coordinator = InvestmentWebInterfaceCoordinator(
        provider="tradingview",
        config=WebAgentConfig(browser="firefox"),
    )

    result = coordinator.execute_action(action="capture_chart", payload={"symbol": "QQQ"})

    assert result["results"]["login"]["details"]["browser"] == "firefox"


def test_web_interface_coordinator_logs_out_when_action_fails() -> None:
    class _FailingAgent:
        def __init__(self) -> None:
            self.logged_out = False

        def login(self):
            return type("Result", (), {"provider": "quantconnect", "action": "login", "status": "ok", "details": {}})()

        def open_workspace(self, workspace_name=None):
            return type(
                "Result",
                (),
                {"provider": "quantconnect", "action": "open_workspace", "status": "ok", "details": {}},
            )()

        def run_action(self, action, payload=None):
            raise RuntimeError("run failed")

        def collect_artifacts(self):
            return []

        def logout(self):
            self.logged_out = True
            return type("Result", (), {"provider": "quantconnect", "action": "logout", "status": "ok", "details": {}})()

    coordinator = InvestmentWebInterfaceCoordinator(
        provider="quantconnect",
        config=WebAgentConfig(browser=BrowserType.CHROMIUM),
    )
    failing_agent = _FailingAgent()
    coordinator._build_agent = lambda provider, config: failing_agent  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="run failed"):
        coordinator.execute_action(action="deploy_strategy", payload={"strategy_id": "s1"})

    assert failing_agent.logged_out is True



def test_agent_catalog_includes_global_risk_manager() -> None:
    from agents.investment_team.agent_catalog import CORE_AGENTS

    assert any(agent.name == "Global Risk Manager Agent" for agent in CORE_AGENTS)


def test_spec_models_parse_minimal_promotion_decision() -> None:
    from agents.investment_team.spec_models import PromotionDecisionV1

    decision = PromotionDecisionV1(
        decision_id="d-1",
        subject_id="s-1",
        subject_type="strategy",
        stage="paper",
        decision="promote_to_live_candidate",
        reasons=["paper performance acceptable"],
        required_changes=[],
        risk_manager_veto=False,
        human_approval_required=True,
        created_at="2026-01-01T00:00:00Z",
        created_by_agent="global_risk_manager",
    )

    assert decision.subject_type.value == "strategy"
    assert decision.decision.value == "promote_to_live_candidate"
