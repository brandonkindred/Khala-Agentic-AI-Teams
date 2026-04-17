from typing import Any, Dict, List

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
    TradeRecord,
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


# ---------------------------------------------------------------------------
# Trade Simulator (shared engine) tests
# ---------------------------------------------------------------------------


def test_date_diff_days() -> None:
    from agents.investment_team.trade_simulator import date_diff_days

    assert date_diff_days("2023-01-01", "2023-12-31") == 364
    assert date_diff_days("2026-01-01", "2026-01-08") == 7
    assert date_diff_days("2026-01-01", "2026-01-01") == 1
    assert date_diff_days("invalid", "invalid") == 1


def test_compute_metrics_from_trades() -> None:
    from agents.investment_team.trade_simulator import compute_metrics

    trades = [
        TradeRecord(
            trade_num=1,
            entry_date="2026-01-05",
            exit_date="2026-01-12",
            symbol="AAPL",
            side="long",
            entry_price=170.0,
            exit_price=178.0,
            shares=50,
            position_value=8500.0,
            gross_pnl=400.0,
            net_pnl=390.0,
            return_pct=4.71,
            hold_days=7,
            outcome="win",
            cumulative_pnl=390.0,
        ),
        TradeRecord(
            trade_num=2,
            entry_date="2026-01-15",
            exit_date="2026-01-22",
            symbol="MSFT",
            side="long",
            entry_price=380.0,
            exit_price=370.0,
            shares=20,
            position_value=7600.0,
            gross_pnl=-200.0,
            net_pnl=-210.0,
            return_pct=-2.63,
            hold_days=7,
            outcome="loss",
            cumulative_pnl=180.0,
        ),
        TradeRecord(
            trade_num=3,
            entry_date="2026-01-25",
            exit_date="2026-02-01",
            symbol="AAPL",
            side="long",
            entry_price=175.0,
            exit_price=182.0,
            shares=50,
            position_value=8750.0,
            gross_pnl=350.0,
            net_pnl=340.0,
            return_pct=4.0,
            hold_days=7,
            outcome="win",
            cumulative_pnl=520.0,
        ),
    ]

    result = compute_metrics(trades, 100000.0, "2026-01-05", "2026-02-01")

    assert result.win_rate_pct == pytest.approx(66.67, abs=0.1)
    assert result.total_return_pct == pytest.approx(0.52, abs=0.01)
    assert result.profit_factor > 1.0
    assert result.max_drawdown_pct >= 0.0


def test_compute_metrics_empty_trades() -> None:
    from agents.investment_team.trade_simulator import compute_metrics

    result = compute_metrics([], 100000.0, "2023-01-01", "2023-12-31")

    assert result.total_return_pct == 0.0
    assert result.win_rate_pct == 0.0
    assert result.sharpe_ratio == 0.0


def test_compute_metrics_uses_cagr() -> None:
    """Verify annualized return uses CAGR, not linear scaling."""
    from agents.investment_team.trade_simulator import compute_metrics

    # 25% total return over 2.5 years → CAGR ≈ 9.54%, not 10%
    trades = [
        TradeRecord(
            trade_num=1,
            entry_date="2021-01-01",
            exit_date="2023-07-01",
            symbol="SPY",
            side="long",
            entry_price=100.0,
            exit_price=125.0,
            shares=1000,
            position_value=100000.0,
            gross_pnl=25000.0,
            net_pnl=25000.0,
            return_pct=25.0,
            hold_days=912,
            outcome="win",
            cumulative_pnl=25000.0,
        ),
    ]

    result = compute_metrics(trades, 100000.0, "2021-01-01", "2023-07-01")

    # CAGR for 25% over ~2.5 years should be ~9.5%, NOT 10%
    assert result.annualized_return_pct < 10.0
    assert result.annualized_return_pct > 9.0


# ---------------------------------------------------------------------------
# Paper Trading tests
# ---------------------------------------------------------------------------


def test_paper_trading_session_model_creation() -> None:
    from agents.investment_team.models import (
        PaperTradingSession,
        PaperTradingStatus,
    )

    session = PaperTradingSession(
        session_id="pt-test-001",
        lab_record_id="lab-abc123",
        strategy=StrategySpec(
            strategy_id="s-pt",
            authored_by="test",
            asset_class="stocks",
            hypothesis="mean reversion",
            signal_definition="RSI oversold bounce",
        ),
        status=PaperTradingStatus.COMPLETED,
        initial_capital=100000.0,
        current_capital=105000.0,
        symbols_traded=["AAPL", "MSFT"],
        data_source="yahoo_finance",
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-03-01T00:00:00Z",
    )

    assert session.session_id == "pt-test-001"
    assert session.lab_record_id == "lab-abc123"
    assert session.status == PaperTradingStatus.COMPLETED
    assert session.initial_capital == 100000.0
    assert len(session.symbols_traded) == 2


def test_compare_performance_aligned() -> None:
    from agents.investment_team.paper_trading_agent import PaperTradingAgent

    backtest = BacktestResult(
        total_return_pct=25.0,
        annualized_return_pct=10.0,
        volatility_pct=14.0,
        sharpe_ratio=0.71,
        max_drawdown_pct=12.0,
        win_rate_pct=55.0,
        profit_factor=1.4,
    )
    paper = BacktestResult(
        total_return_pct=24.0,
        annualized_return_pct=9.5,
        volatility_pct=13.0,
        sharpe_ratio=0.69,
        max_drawdown_pct=14.0,
        win_rate_pct=53.0,
        profit_factor=1.3,
    )

    comparison = PaperTradingAgent.compare_performance(
        paper,
        backtest,
        paper_trade_count=50,
        backtest_trade_count=60,
    )

    assert comparison.overall_aligned is True
    assert comparison.win_rate_aligned is True
    assert comparison.return_aligned is True
    assert comparison.sharpe_aligned is True
    assert comparison.drawdown_aligned is True
    assert comparison.profit_factor_aligned is True


def test_compare_performance_divergent() -> None:
    from agents.investment_team.paper_trading_agent import PaperTradingAgent

    backtest = BacktestResult(
        total_return_pct=25.0,
        annualized_return_pct=10.0,
        volatility_pct=14.0,
        sharpe_ratio=0.71,
        max_drawdown_pct=12.0,
        win_rate_pct=55.0,
        profit_factor=1.4,
    )
    paper = BacktestResult(
        total_return_pct=5.0,
        annualized_return_pct=2.0,
        volatility_pct=20.0,
        sharpe_ratio=0.10,
        max_drawdown_pct=30.0,
        win_rate_pct=38.0,
        profit_factor=0.8,
    )

    comparison = PaperTradingAgent.compare_performance(paper, backtest)

    assert comparison.overall_aligned is False
    assert comparison.win_rate_aligned is False
    assert comparison.return_aligned is False
    assert comparison.profit_factor_aligned is False


def test_compare_performance_zero_backtest_drawdown() -> None:
    """When backtest drawdown is 0, paper drawdown up to 5% is aligned."""
    from agents.investment_team.paper_trading_agent import PaperTradingAgent

    backtest = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=10.0,
        volatility_pct=5.0,
        sharpe_ratio=2.0,
        max_drawdown_pct=0.0,
        win_rate_pct=100.0,
        profit_factor=100.0,
    )
    paper = BacktestResult(
        total_return_pct=9.0,
        annualized_return_pct=9.0,
        volatility_pct=6.0,
        sharpe_ratio=1.8,
        max_drawdown_pct=4.0,
        win_rate_pct=95.0,
        profit_factor=90.0,
    )

    comparison = PaperTradingAgent.compare_performance(paper, backtest)
    assert comparison.drawdown_aligned is True

    # Paper drawdown > 5% when backtest is 0 → not aligned
    paper_bad = BacktestResult(
        total_return_pct=9.0,
        annualized_return_pct=9.0,
        volatility_pct=6.0,
        sharpe_ratio=1.8,
        max_drawdown_pct=6.0,
        win_rate_pct=95.0,
        profit_factor=90.0,
    )
    comparison_bad = PaperTradingAgent.compare_performance(paper_bad, backtest)
    assert comparison_bad.drawdown_aligned is False


def test_compare_performance_return_absolute_tolerance() -> None:
    """Phase 5: all returns use ±2.0pp absolute tolerance now."""
    from agents.investment_team.paper_trading_agent import PaperTradingAgent

    backtest = BacktestResult(
        total_return_pct=12.0,
        annualized_return_pct=5.0,
        volatility_pct=10.0,
        sharpe_ratio=0.5,
        max_drawdown_pct=10.0,
        win_rate_pct=55.0,
        profit_factor=1.2,
    )
    paper_close = BacktestResult(
        total_return_pct=9.0,
        annualized_return_pct=3.5,
        volatility_pct=11.0,
        sharpe_ratio=0.32,
        max_drawdown_pct=12.0,
        win_rate_pct=52.0,
        profit_factor=1.1,
    )

    comparison = PaperTradingAgent.compare_performance(paper_close, backtest)
    # |3.5 - 5.0| = 1.5 ≤ 2.0 → aligned
    assert comparison.return_aligned is True

    paper_far = BacktestResult(
        total_return_pct=5.0,
        annualized_return_pct=2.0,
        volatility_pct=11.0,
        sharpe_ratio=0.18,
        max_drawdown_pct=12.0,
        win_rate_pct=52.0,
        profit_factor=1.1,
    )
    comparison_far = PaperTradingAgent.compare_performance(paper_far, backtest)
    # |2.0 - 5.0| = 3.0 > 2.0 → not aligned
    assert comparison_far.return_aligned is False


def test_compare_performance_insufficient_sample() -> None:
    """Fewer than 30 paper trades → overall_aligned is always False."""
    from agents.investment_team.paper_trading_agent import PaperTradingAgent

    backtest = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=10.0,
        volatility_pct=14.0,
        sharpe_ratio=0.71,
        max_drawdown_pct=12.0,
        win_rate_pct=55.0,
        profit_factor=1.4,
    )
    paper = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=10.0,
        volatility_pct=14.0,
        sharpe_ratio=0.71,
        max_drawdown_pct=12.0,
        win_rate_pct=55.0,
        profit_factor=1.4,
    )

    comparison = PaperTradingAgent.compare_performance(
        paper,
        backtest,
        paper_trade_count=10,
        backtest_trade_count=50,
    )
    assert comparison.overall_aligned is False


def test_paper_trading_verdict_enum_values() -> None:
    from agents.investment_team.models import PaperTradingVerdict

    assert PaperTradingVerdict.READY_FOR_LIVE.value == "ready_for_live"
    assert PaperTradingVerdict.NOT_PERFORMANT.value == "not_performant"


# ---------------------------------------------------------------------------
# Market Data Service tests
# ---------------------------------------------------------------------------


def test_market_data_service_get_symbols_for_strategy() -> None:
    from agents.investment_team.market_data_service import MarketDataService

    service = MarketDataService()

    stock_strategy = StrategySpec(
        strategy_id="s1",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
    )
    symbols = service.get_symbols_for_strategy(stock_strategy)
    assert "AAPL" in symbols
    assert "BTC" not in symbols

    crypto_strategy = StrategySpec(
        strategy_id="s2",
        authored_by="test",
        asset_class="crypto",
        hypothesis="h",
        signal_definition="s",
    )
    symbols = service.get_symbols_for_strategy(crypto_strategy)
    assert "BTC" in symbols
    assert "AAPL" not in symbols


def test_market_data_service_get_symbols_for_forex() -> None:
    from agents.investment_team.market_data_service import MarketDataService

    service = MarketDataService()
    strategy = StrategySpec(
        strategy_id="s-fx",
        authored_by="test",
        asset_class="forex",
        hypothesis="h",
        signal_definition="s",
    )
    symbols = service.get_symbols_for_strategy(strategy)
    assert any("=X" in s for s in symbols)


def test_market_data_service_get_symbols_for_futures() -> None:
    from agents.investment_team.market_data_service import MarketDataService

    service = MarketDataService()
    strategy = StrategySpec(
        strategy_id="s-fut",
        authored_by="test",
        asset_class="futures",
        hypothesis="h",
        signal_definition="s",
    )
    symbols = service.get_symbols_for_strategy(strategy)
    assert any("=F" in s for s in symbols)


def test_market_data_service_get_symbols_for_commodities() -> None:
    from agents.investment_team.market_data_service import MarketDataService

    service = MarketDataService()
    strategy = StrategySpec(
        strategy_id="s-com",
        authored_by="test",
        asset_class="commodities",
        hypothesis="h",
        signal_definition="s",
    )
    symbols = service.get_symbols_for_strategy(strategy)
    assert "GLD" in symbols


def test_market_data_service_fetch_ohlcv_range_routes_by_asset_class() -> None:
    """Verify fetch_ohlcv_range tries Yahoo first for all asset classes via the provider chain."""
    from unittest.mock import patch

    from agents.investment_team.market_data_service import MarketDataService

    service = MarketDataService()

    # Stocks: Yahoo is the first provider in the chain
    with patch.object(service, "_fetch_yahoo", return_value=[]) as mock_yahoo:
        service.fetch_ohlcv_range("AAPL", "stocks", "2023-01-01", "2023-12-31")
        mock_yahoo.assert_called_once_with("AAPL", "stocks", "2023-01-01", "2023-12-31")

    # Crypto: Yahoo is also the first provider, followed by Twelve Data, then CoinGecko
    with patch.object(service, "_fetch_yahoo", return_value=[]) as mock_yahoo:
        with patch.object(service, "_fetch_twelve_data", return_value=[]):
            with patch.object(service, "_fetch_coingecko", return_value=[]):
                service.fetch_ohlcv_range("BTC", "crypto", "2023-01-01", "2023-12-31")
                mock_yahoo.assert_called_once_with("BTC", "crypto", "2023-01-01", "2023-12-31")

    # Forex: Yahoo first in chain
    with patch.object(service, "_fetch_yahoo", return_value=[]) as mock_yahoo:
        service.fetch_ohlcv_range("EURUSD=X", "forex", "2023-01-01", "2023-12-31")
        mock_yahoo.assert_called_once_with("EURUSD=X", "forex", "2023-01-01", "2023-12-31")


def test_market_data_service_fetch_multi_symbol_range() -> None:
    from unittest.mock import patch

    from agents.investment_team.market_data_service import MarketDataService, OHLCVBar

    service = MarketDataService()
    sample_bar = OHLCVBar(
        date="2023-06-01", open=150.0, high=155.0, low=148.0, close=153.0, volume=1000000
    )

    with patch.object(service, "fetch_ohlcv_range", return_value=[sample_bar]):
        result = service.fetch_multi_symbol_range(
            ["AAPL", "MSFT"], "stocks", "2023-01-01", "2023-12-31"
        )

    assert "AAPL" in result
    assert "MSFT" in result
    assert len(result["AAPL"]) == 1
    assert result["AAPL"][0].close == 153.0


def test_agent_catalog_includes_signal_intelligence_expert() -> None:
    from agents.investment_team.agent_catalog import CORE_AGENTS

    assert any(agent.name == "Signal Intelligence Expert" for agent in CORE_AGENTS)


def test_trade_record_backward_compat_defaults() -> None:
    """Legacy persisted rows without the new fields deserialize cleanly."""
    legacy = {
        "trade_num": 1,
        "entry_date": "2023-01-01",
        "exit_date": "2023-01-02",
        "symbol": "LEG",
        "side": "long",
        "entry_price": 100.0,
        "exit_price": 101.0,
        "shares": 5,
        "position_value": 500.0,
        "gross_pnl": 5.0,
        "net_pnl": 4.5,
        "return_pct": 1.0,
        "hold_days": 1,
        "outcome": "win",
        "cumulative_pnl": 4.5,
    }
    trade = TradeRecord(**legacy)

    # New fields default to None / "market"
    assert trade.entry_bid_price is None
    assert trade.entry_fill_price is None
    assert trade.exit_bid_price is None
    assert trade.exit_fill_price is None
    assert trade.entry_order_type == "market"
    assert trade.exit_order_type == "market"


# ---------------------------------------------------------------------------
# Strategy Lab cycle — paper-trading step gating
# ---------------------------------------------------------------------------
#
# The cycle is now orchestrator-based: ``orchestrator.run_cycle(...)`` produces
# a complete StrategyLabRecord, and the paper-trading step runs *after* that
# based on ``record.is_winning``. These tests stub the orchestrator to return
# a pre-built record so we can assert the paper-trading gating/failure
# contract without spinning up real code generation + sandbox execution.


class _InMemoryDict:
    """Minimal _PersistentDict-compatible stand-in for cycle tests."""

    def __init__(self) -> None:
        self._d: Dict[str, Any] = {}

    def __setitem__(self, key: str, value: Any) -> None:
        self._d[key] = value.model_dump(mode="json") if hasattr(value, "model_dump") else value

    def __getitem__(self, key: str) -> Any:
        return self._d[key]

    def get(self, key: str, default=None):
        return self._d.get(key, default)

    def values(self):
        return self._d.values()

    def __contains__(self, key: str) -> bool:
        return key in self._d

    def pop(self, key: str, default=None):
        return self._d.pop(key, default)


def _install_inmemory_stores(monkeypatch):
    """Replace the cycle's persistent dicts with in-memory shims."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_strategy_lab_records", _InMemoryDict())
    monkeypatch.setattr(api_main, "_strategies", _InMemoryDict())
    monkeypatch.setattr(api_main, "_backtests", _InMemoryDict())
    monkeypatch.setattr(api_main, "_paper_trading_sessions", _InMemoryDict())


def _cycle_models():
    """Return the models module that ``api.main`` uses (via ``investment_team.*``).

    pytest's ``pythonpath = agents`` config makes both ``agents.investment_team.models``
    and ``investment_team.models`` importable, but Pydantic v2 compares model
    classes by identity — so cycle tests must use the same module path that
    the API internally uses, otherwise ``BacktestRecord(config=...)`` etc. raise
    ``model_type`` errors.
    """
    from investment_team import models as cycle_models

    return cycle_models


def _backtest_result(*, winning: bool) -> Any:
    m = _cycle_models()
    if winning:
        return m.BacktestResult(
            total_return_pct=50.0,
            annualized_return_pct=20.0,  # > 8% threshold
            volatility_pct=10.0,
            sharpe_ratio=1.5,
            max_drawdown_pct=8.0,
            win_rate_pct=60.0,
            profit_factor=2.0,
        )
    return m.BacktestResult(
        total_return_pct=-5.0,
        annualized_return_pct=-2.0,  # below 8% threshold
        volatility_pct=10.0,
        sharpe_ratio=-0.2,
        max_drawdown_pct=10.0,
        win_rate_pct=40.0,
        profit_factor=0.5,
    )


def _cycle_backtest_config() -> Any:
    m = _cycle_models()
    return m.BacktestConfig(
        start_date="2021-01-01",
        end_date="2024-12-31",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )


def _make_lab_record(
    *,
    winning: bool,
    strategy_code: str = "def strategy(): pass\n",
) -> Any:
    """Build a pre-assembled StrategyLabRecord for cycle tests to stub the orchestrator."""
    m = _cycle_models()
    config = _cycle_backtest_config()
    strategy = m.StrategySpec(
        strategy_id="strat-lab-test",
        authored_by="strategy_ideation_agent",
        asset_class="equities",
        hypothesis="test",
        signal_definition="sig",
        entry_rules=["e1"],
        exit_rules=["x1"],
        sizing_rules=["s1"],
        risk_limits={},
        speculative=False,
    )
    backtest = m.BacktestRecord(
        backtest_id="bt-lab-test",
        strategy_id=strategy.strategy_id,
        strategy=strategy,
        config=config,
        submitted_by="strategy_ideation_agent",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T00:01:00Z",
        result=_backtest_result(winning=winning),
        notes=[],
        trades=[],
    )
    return m.StrategyLabRecord(
        lab_record_id="lab-test",
        strategy=strategy,
        backtest=backtest,
        is_winning=winning,
        strategy_rationale="rationale",
        analysis_narrative="narrative",
        created_at="2024-01-01T00:01:00Z",
        strategy_code=strategy_code,
    )


class _FakeOrchestrator:
    """Stand-in for StrategyLabOrchestrator.run_cycle() in cycle tests."""

    def __init__(self, record) -> None:
        self._record = record

    def run_cycle(
        self,
        *,
        prior_records=None,
        config=None,
        signal_brief=None,
        on_phase=None,
        exclude_asset_classes=None,
    ):
        return self._record


def test_cycle_skips_paper_trading_when_strategy_loses(monkeypatch) -> None:
    """Losing strategy: cycle must record paper_trading_status='skipped' (not_winning) and never call the helper."""
    from investment_team.api import main as api_main

    _install_inmemory_stores(monkeypatch)

    paper_calls: List[bool] = []

    def _should_not_be_called(*args, **kwargs):
        paper_calls.append(True)
        raise AssertionError("Paper trading must not run for a losing strategy")

    monkeypatch.setattr(api_main, "_run_paper_trading_step", _should_not_be_called)

    phases: List[str] = []

    def capture_phase(phase: str, data=None) -> None:
        phases.append(phase)

    losing_record = _make_lab_record(winning=False)
    record = api_main._run_one_strategy_lab_cycle(
        _cycle_backtest_config(),
        _FakeOrchestrator(losing_record),
        on_phase=capture_phase,
        paper_trading_enabled=True,
    )

    assert paper_calls == [], "Helper must not be invoked for losing strategies"
    assert record.is_winning is False
    assert record.paper_trading_status == "skipped"
    assert record.paper_trading_skipped_reason == "not_winning"
    assert record.paper_trading_session_id is None
    assert record.paper_trading_verdict is None
    assert "paper_trading_skipped" in phases
    assert "paper_trading" not in phases  # never entered the step


def test_cycle_skips_paper_trading_when_disabled(monkeypatch) -> None:
    """paper_trading_enabled=False: skip with reason='disabled' even for winners."""
    from investment_team.api import main as api_main

    _install_inmemory_stores(monkeypatch)

    def _should_not_be_called(**kwargs):
        raise AssertionError("Paper trading must not run when disabled")

    monkeypatch.setattr(api_main, "_run_paper_trading_step", _should_not_be_called)

    winning_record = _make_lab_record(winning=True)
    record = api_main._run_one_strategy_lab_cycle(
        _cycle_backtest_config(),
        _FakeOrchestrator(winning_record),
        paper_trading_enabled=False,
    )

    assert record.is_winning is True
    assert record.paper_trading_status == "skipped"
    assert record.paper_trading_skipped_reason == "disabled"
    assert record.paper_trading_session_id is None


def test_cycle_skips_paper_trading_when_strategy_code_missing(monkeypatch) -> None:
    """Orchestrator returned a winning record with no strategy_code: skip gracefully."""
    from investment_team.api import main as api_main

    _install_inmemory_stores(monkeypatch)

    def _should_not_be_called(**kwargs):
        raise AssertionError("Paper trading must not run without strategy_code")

    monkeypatch.setattr(api_main, "_run_paper_trading_step", _should_not_be_called)

    record_no_code = _make_lab_record(winning=True, strategy_code="")
    record_no_code.strategy_code = None

    record = api_main._run_one_strategy_lab_cycle(
        _cycle_backtest_config(),
        _FakeOrchestrator(record_no_code),
        paper_trading_enabled=True,
    )

    assert record.paper_trading_status == "skipped"
    assert record.paper_trading_skipped_reason == "no_strategy_code"


def test_cycle_runs_paper_trading_for_winners(monkeypatch) -> None:
    """Winning strategy with paper_trading_enabled=True: helper runs and session_id is stored."""
    from investment_team.api import main as api_main

    m = _cycle_models()
    _install_inmemory_stores(monkeypatch)

    captured_kwargs: Dict[str, Any] = {}

    def fake_paper_step(**kwargs):
        captured_kwargs.update(kwargs)
        strategy = kwargs["strategy"]
        return m.PaperTradingSession(
            session_id="pt-test-1",
            lab_record_id="",  # set by caller
            strategy=strategy,
            status=m.PaperTradingStatus.COMPLETED,
            initial_capital=100_000.0,
            current_capital=105_000.0,
            trades=[],
            verdict=m.PaperTradingVerdict.READY_FOR_LIVE,
            symbols_traded=["X"],
            data_source="test",
            data_period_start="2024-01-01",
            data_period_end="2024-06-01",
            started_at="2024-06-01T00:00:00Z",
            completed_at="2024-06-01T01:00:00Z",
        )

    monkeypatch.setattr(api_main, "_run_paper_trading_step", fake_paper_step)

    winning_record = _make_lab_record(winning=True, strategy_code="def run(): return []\n")
    record = api_main._run_one_strategy_lab_cycle(
        _cycle_backtest_config(),
        _FakeOrchestrator(winning_record),
        paper_trading_enabled=True,
        paper_trading_lookback_days=200,
    )

    assert record.is_winning is True
    assert record.paper_trading_status == "completed"
    assert record.paper_trading_session_id == "pt-test-1"
    assert record.paper_trading_verdict == m.PaperTradingVerdict.READY_FOR_LIVE
    assert record.paper_trading_skipped_reason is None
    assert record.paper_trading_error is None

    # Cycle forwarded lookback_days and inherited execution assumptions from config
    assert captured_kwargs["lookback_days"] == 200
    assert captured_kwargs["strategy_code"] == "def run(): return []\n"
    assert captured_kwargs["initial_capital"] == 100_000.0
    assert captured_kwargs["transaction_cost_bps"] == 5.0
    assert captured_kwargs["slippage_bps"] == 2.0


def test_cycle_records_paper_trading_failure_as_non_fatal(monkeypatch) -> None:
    """Paper-trading exception: cycle persists a valid winning record with status='failed'."""
    from investment_team.api import main as api_main

    _install_inmemory_stores(monkeypatch)

    def boom(**kwargs):
        raise RuntimeError("simulated sandbox crash")

    monkeypatch.setattr(api_main, "_run_paper_trading_step", boom)

    winning_record = _make_lab_record(winning=True)
    record = api_main._run_one_strategy_lab_cycle(
        _cycle_backtest_config(),
        _FakeOrchestrator(winning_record),
        paper_trading_enabled=True,
    )

    # Winner still recorded
    assert record.is_winning is True
    assert record.paper_trading_status == "failed"
    assert record.paper_trading_error is not None
    assert "simulated sandbox crash" in record.paper_trading_error
    assert record.paper_trading_session_id is None


def test_cycle_records_paper_trading_no_market_data_as_skipped(monkeypatch) -> None:
    """_PaperTradingDataUnavailable: cycle marks the record as skipped=no_market_data (non-fatal)."""
    from investment_team.api import main as api_main

    _install_inmemory_stores(monkeypatch)

    def no_data(**kwargs):
        raise api_main._PaperTradingDataUnavailable("providers exhausted")

    monkeypatch.setattr(api_main, "_run_paper_trading_step", no_data)

    winning_record = _make_lab_record(winning=True)
    record = api_main._run_one_strategy_lab_cycle(
        _cycle_backtest_config(),
        _FakeOrchestrator(winning_record),
        paper_trading_enabled=True,
    )

    assert record.is_winning is True
    assert record.paper_trading_status == "skipped"
    assert record.paper_trading_skipped_reason == "no_market_data"
    assert record.paper_trading_error is None
