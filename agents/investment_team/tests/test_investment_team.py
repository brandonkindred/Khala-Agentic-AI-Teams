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
