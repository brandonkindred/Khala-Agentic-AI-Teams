import pytest
from agents.investment_team.agents import (
    AgentIdentity,
    FinancialAdvisorAgent,
    PolicyGuardianAgent,
    PromotionGateAgent,
)
from agents.investment_team.models import (
    IPS,
    AdvisorSessionStatus,
    AdvisorTopic,
    BacktestConfig,
    BacktestRecord,
    BacktestResult,
    CollectedProfileData,
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
            planned_large_expenses=[
                PlannedLargeExpense(name="car", amount=10000, date="2027-01-01T00:00:00Z")
            ],
        ),
        income=IncomeProfile(annual_gross=120000, stability="stable"),
        net_worth=NetWorth(total=300000, investable_assets=200000),
        savings_rate=SavingsRate(monthly=2000, annual=24000),
        tax_profile=TaxProfile(country="US", state="CA", account_types=["taxable"]),
        preferences=UserPreferences(),
        goals=[
            UserGoal(
                name="retire",
                target_amount=1_000_000,
                target_date="2035-01-01T00:00:00Z",
                priority="high",
            )
        ],
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
            ValidationCheck(
                name="transaction_cost_model", status=ValidationStatus.PASS, details="ok"
            ),
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
    strategy = StrategySpec(
        strategy_id="s1",
        authored_by="research",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
    )
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
    strategy = StrategySpec(
        strategy_id="s1",
        authored_by="research",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
    )
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
        positions=[
            PortfolioPosition(symbol="BTC", asset_class="crypto", weight_pct=5, rationale="alpha")
        ],
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

    result = orch.run_web_action(
        action="capture_chart", payload={"symbol": "SPY"}, workspace_name="swing"
    )

    assert result["provider"] == "tradingview"
    assert result["results"]["open_workspace"]["details"]["workspace"] == "swing"


def test_orchestrator_web_action_accepts_protocol_compatible_coordinator() -> None:
    class _CoordinatorStub:
        def execute_action(self, action, payload=None, workspace_name=None):
            return {
                "provider": "stub",
                "results": {"run_action": {"action": action, "payload": payload}},
                "workspace": workspace_name,
            }

    orch = InvestmentTeamOrchestrator(web_interface_coordinator=_CoordinatorStub())

    result = orch.run_web_action(
        action="capture_chart", payload={"symbol": "IWM"}, workspace_name="test"
    )

    assert result["provider"] == "stub"
    assert result["workspace"] == "test"


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


def test_web_interface_coordinator_accepts_webkit_browser_config() -> None:
    coordinator = InvestmentWebInterfaceCoordinator(
        provider="tradingview",
        config=WebAgentConfig(browser="webkit"),
    )

    result = coordinator.execute_action(action="capture_chart", payload={"symbol": "DIA"})

    assert result["results"]["login"]["details"]["browser"] == "webkit"


def test_web_interface_coordinator_logs_out_when_action_fails() -> None:
    class _FailingAgent:
        def __init__(self) -> None:
            self.logged_out = False

        def login(self):
            return type(
                "Result",
                (),
                {"provider": "quantconnect", "action": "login", "status": "ok", "details": {}},
            )()

        def open_workspace(self, workspace_name=None):
            return type(
                "Result",
                (),
                {
                    "provider": "quantconnect",
                    "action": "open_workspace",
                    "status": "ok",
                    "details": {},
                },
            )()

        def run_action(self, action, payload=None):
            raise RuntimeError("run failed")

        def collect_artifacts(self):
            return []

        def logout(self):
            self.logged_out = True
            return type(
                "Result",
                (),
                {"provider": "quantconnect", "action": "logout", "status": "ok", "details": {}},
            )()

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


def test_backtest_record_captures_strategy_and_metrics() -> None:
    strategy = StrategySpec(
        strategy_id="s-backtest",
        authored_by="research",
        asset_class="equities",
        hypothesis="mean reversion",
        signal_definition="z-score",
        entry_rules=["z < -2"],
        exit_rules=["z > -0.5"],
    )

    record = BacktestRecord(
        backtest_id="bt-1",
        strategy_id=strategy.strategy_id,
        strategy=strategy,
        config=BacktestConfig(start_date="2020-01-01", end_date="2024-12-31"),
        submitted_by="trading-agent-1",
        submitted_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:05:00Z",
        result=BacktestResult(
            total_return_pct=12.5,
            annualized_return_pct=3.0,
            volatility_pct=8.4,
            sharpe_ratio=0.36,
            max_drawdown_pct=7.1,
            win_rate_pct=54.2,
            profit_factor=1.24,
        ),
    )

    assert record.strategy.strategy_id == "s-backtest"
    assert record.result.sharpe_ratio == 0.36
    assert record.config.initial_capital == 100000.0


# ---------------------------------------------------------------------------
# Financial Advisor Agent tests
# ---------------------------------------------------------------------------


def test_advisor_start_session_returns_greeting() -> None:
    agent = FinancialAdvisorAgent()
    session = agent.start_session(session_id="adv-1", user_id="u1")

    assert session.session_id == "adv-1"
    assert session.user_id == "u1"
    assert session.status == AdvisorSessionStatus.ACTIVE
    assert session.current_topic == AdvisorTopic.GREETING
    assert len(session.messages) == 1
    assert session.messages[0].role == "advisor"
    assert "financial advisor" in session.messages[0].content.lower()


def test_advisor_advances_through_topics() -> None:
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-2", "u2")

    # Greeting -> user says risk tolerance
    agent.handle_message(session, "I'd say medium risk tolerance")
    assert session.current_topic == AdvisorTopic.RISK_TOLERANCE
    assert session.collected.risk_tolerance == "medium"

    # Risk tolerance -> max drawdown
    agent.handle_message(session, "I could handle a 25% drop")
    assert session.current_topic == AdvisorTopic.TIME_HORIZON
    assert session.collected.max_drawdown_tolerance_pct == 25.0

    # Time horizon
    agent.handle_message(session, "About 15 years")
    assert session.current_topic == AdvisorTopic.INCOME
    assert session.collected.time_horizon_years == 15


def test_advisor_extracts_income_data() -> None:
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-3", "u3")
    # Skip to income topic
    session.current_topic = AdvisorTopic.INCOME

    agent.handle_message(session, "I make about 150k per year and it's pretty stable")
    assert session.collected.annual_gross_income == 150000
    assert session.collected.income_stability == "stable"


def test_advisor_extracts_net_worth() -> None:
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-4", "u4")
    session.current_topic = AdvisorTopic.NET_WORTH

    agent.handle_message(session, "Total net worth is about 500k, with 300k investable")
    assert session.collected.total_net_worth == 500000
    assert session.collected.investable_assets == 300000


def test_advisor_extracts_savings() -> None:
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-5", "u5")
    session.current_topic = AdvisorTopic.SAVINGS

    agent.handle_message(session, "I save about $3000 per month")
    assert session.collected.monthly_savings == 3000
    assert session.collected.annual_savings == 36000


def test_advisor_extracts_tax_info() -> None:
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-6", "u6")
    session.current_topic = AdvisorTopic.TAX

    agent.handle_message(session, "US, California. I have a 401k and a Roth IRA")
    assert session.collected.tax_country == "US"
    assert session.collected.tax_state == "CA"
    assert "401k" in session.collected.account_types
    assert "roth_ira" in session.collected.account_types


def test_advisor_extracts_preferences() -> None:
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-7", "u7")
    session.current_topic = AdvisorTopic.PREFERENCES

    agent.handle_message(session, "No crypto please, I care about ESG investing")
    assert session.collected.crypto_allowed is False
    assert session.collected.esg_preference == "moderate"


def test_advisor_full_conversation_builds_ips() -> None:
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-full", "u-full")

    # Walk through all topics
    replies = [
        "I'm a high risk investor",
        "30% drawdown is fine",
        "20 years",
        "200k annual income, stable",
        "Total worth 1m, investable 600k",
        "5000 a month",
        "US, New York, I have a taxable brokerage and 401k",
        "6 months emergency fund, no big expenses planned",
        "Retirement, 2m target, high priority",
        "I'm fine with everything, no exclusions, no ESG preference",
        "10% max position, 70% equities max",
        "Paper mode, quarterly rebalance, 10% speculative cap",
    ]

    for reply in replies:
        agent.handle_message(session, reply)

    # Should be at REVIEW now
    assert session.current_topic == AdvisorTopic.REVIEW

    # Confirm
    agent.handle_message(session, "Looks good, confirm")
    assert session.status == AdvisorSessionStatus.COMPLETED

    # Build the IPS
    ips = agent.build_ips(session)
    assert ips.profile.user_id == "u-full"
    assert ips.profile.risk_tolerance == RiskTolerance.HIGH
    assert ips.profile.max_drawdown_tolerance_pct == 30.0
    assert ips.profile.time_horizon_years == 20
    assert ips.profile.income.annual_gross == 200000
    assert ips.profile.net_worth.investable_assets == 600000
    assert ips.profile.savings_rate.monthly == 5000
    assert ips.profile.tax_profile.state == "NY"
    assert ips.rebalance_frequency == "quarterly"


def test_advisor_missing_fields_detected() -> None:
    collected = CollectedProfileData()
    missing = FinancialAdvisorAgent.missing_fields(collected)
    assert "risk_tolerance" in missing
    assert "annual_gross_income" in missing
    assert "time_horizon_years" in missing
    assert len(missing) == 6


def test_advisor_build_ips_rejects_incomplete_data() -> None:
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-incomplete", "u-inc")

    with pytest.raises(ValueError, match="missing required fields"):
        agent.build_ips(session)


def test_advisor_number_extraction_with_suffixes() -> None:
    agent = FinancialAdvisorAgent()
    assert agent._extract_number("about 150k") == 150000
    assert agent._extract_number("2.5m in assets") == 2500000
    assert agent._extract_number("$3,000 per month") == 3000
    assert agent._extract_number("no numbers here") is None


def test_advisor_session_inactive_after_completion() -> None:
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-done", "u-done")
    session.status = AdvisorSessionStatus.COMPLETED

    reply = agent.handle_message(session, "hello")
    assert "no longer active" in reply.lower()


def test_agent_catalog_includes_financial_advisor() -> None:
    from agents.investment_team.agent_catalog import CORE_AGENTS

    assert any(agent.name == "Financial Advisor Agent" for agent in CORE_AGENTS)
