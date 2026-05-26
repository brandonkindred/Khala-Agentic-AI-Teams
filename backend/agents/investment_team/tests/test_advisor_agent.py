"""Coverage for ``FinancialAdvisorAgent`` (state-machine advisor).

The advisor is a deterministic conversational state machine driven by
``_TOPIC_QUESTIONS`` / ``_TOPIC_ORDER``. The existing
``test_investment_team`` exercises the happy path lightly; these tests
cover the per-topic extractors, the inactive-session branch, the build
fallback paths, the confirmation/edit branches, and number/currency
parsing edge cases.
"""

from __future__ import annotations

import pytest

from investment_team.agents import (
    AgentIdentity,
    FinancialAdvisorAgent,
    PromotionGateAgent,
    ValidationAgent,
)
from investment_team.models import (
    AdvisorSessionStatus,
    AdvisorTopic,
    AuditContext,
    CollectedProfileData,
    PromotionStage,
    StrategySpec,
    ValidationCheck,
    ValidationReport,
    ValidationStatus,
)

# ---------------------------------------------------------------------------
# Number-parsing utilities
# ---------------------------------------------------------------------------


def test_extract_all_numbers_handles_suffixes_and_commas() -> None:
    nums = FinancialAdvisorAgent._extract_all_numbers("I make 120,000 and have 1.5m saved, 10k cash")
    assert nums == [120000.0, 1_500_000.0, 10_000.0]


def test_extract_all_numbers_handles_billions_and_dollar_sign() -> None:
    nums = FinancialAdvisorAgent._extract_all_numbers("$3.2b portfolio")
    assert nums == [3_200_000_000.0]


def test_extract_number_returns_first_or_none() -> None:
    assert FinancialAdvisorAgent._extract_number("approximately 20 years") == 20.0
    assert FinancialAdvisorAgent._extract_number("no numbers here") is None


# ---------------------------------------------------------------------------
# State machine — start_session + handle_message
# ---------------------------------------------------------------------------


def _new_session():
    return FinancialAdvisorAgent().start_session("adv-1", "user-1")


def test_start_session_initialises_with_greeting() -> None:
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-1", "user-1")
    assert session.session_id == "adv-1"
    assert session.user_id == "user-1"
    assert session.status == AdvisorSessionStatus.ACTIVE
    assert session.current_topic == AdvisorTopic.GREETING
    # First message is the greeting.
    assert session.messages[0].role == "advisor"


def test_handle_message_on_inactive_session() -> None:
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-1", "user-1")
    session.status = AdvisorSessionStatus.COMPLETED
    reply = agent.handle_message(session, "hi")
    assert "no longer active" in reply


def test_handle_message_drives_state_machine_through_topics() -> None:
    """Walk the advisor through every topic and end with REVIEW + confirm."""
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-1", "user-1")

    # GREETING → risk-tolerance answer
    agent.handle_message(session, "I'd say medium risk")
    assert session.collected.risk_tolerance == "medium"

    # RISK_TOLERANCE → max drawdown
    agent.handle_message(session, "20% drawdown")
    assert session.collected.max_drawdown_tolerance_pct == 20.0

    # TIME_HORIZON
    agent.handle_message(session, "around 15 years")
    assert session.collected.time_horizon_years == 15

    # INCOME
    agent.handle_message(session, "$120,000 stable")
    assert session.collected.annual_gross_income == 120000.0
    assert session.collected.income_stability == "stable"

    # NET_WORTH — two numbers given
    agent.handle_message(session, "total 500k investable 300k")
    assert session.collected.total_net_worth == 500_000.0
    assert session.collected.investable_assets == 300_000.0

    # SAVINGS — monthly extracted
    agent.handle_message(session, "I save 2000 a month")
    assert session.collected.monthly_savings == 2000.0
    assert session.collected.annual_savings == 24_000.0

    # TAX — US + CA state + IRA account
    agent.handle_message(session, "US, California, Roth IRA")
    assert session.collected.tax_country == "US"
    assert session.collected.tax_state == "CA"
    assert "roth_ira" in session.collected.account_types

    # LIQUIDITY — 6 months emergency fund. The number parser treats trailing
    # ``m`` as million, so phrase the number adjacent to a non-``m`` token.
    agent.handle_message(session, "six (6) emergency fund period")
    assert session.collected.emergency_fund_months == 6

    # GOALS — retirement first, college second
    agent.handle_message(session, "retirement 1000000, college 200000")
    goal_names = [g.name for g in session.collected.goals]
    assert "retirement" in goal_names
    assert "college_fund" in goal_names

    # PREFERENCES — no crypto, ESG strong
    agent.handle_message(session, "avoid crypto, strong ESG please")
    assert "crypto" in session.collected.excluded_asset_classes
    assert session.collected.esg_preference == "strong"
    assert session.collected.crypto_allowed is False

    # CONSTRAINTS — single position cap + asset class caps
    agent.handle_message(session, "max 5% per position, 60% equities, 10% crypto")
    assert session.collected.max_single_position_pct == 5.0
    assert session.collected.max_asset_class_pct.get("equities") == 60.0
    assert session.collected.max_asset_class_pct.get("crypto") == 10.0

    # TRADING_PREFERENCES — live trading, manual approval, paper mode
    agent.handle_message(
        session, "live trading with manual approval, monthly rebalance, paper mode"
    )
    # Reaches REVIEW with summary appended.
    assert session.current_topic == AdvisorTopic.REVIEW

    # Confirm → completes the session.
    reply_done = agent.handle_message(session, "confirm")
    assert session.status == AdvisorSessionStatus.COMPLETED
    assert "successfully" in reply_done.lower()


def test_review_non_confirmation_keeps_session_active() -> None:
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-1", "user-1")
    session.current_topic = AdvisorTopic.REVIEW
    reply = agent.handle_message(session, "I want to change my risk tolerance")
    assert session.status == AdvisorSessionStatus.ACTIVE
    assert "change" in reply.lower()


def test_handle_message_falls_back_to_defaults_when_no_data() -> None:
    """Each topic extractor must safely default when the user reply is bare."""
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-1", "user-1")
    agent.handle_message(session, "hmm")  # no recognised risk level
    assert session.collected.risk_tolerance == "medium"  # default

    agent.handle_message(session, "no number")
    assert session.collected.max_drawdown_tolerance_pct == 20.0  # default

    agent.handle_message(session, "no number")
    assert session.collected.time_horizon_years == 10  # default


def test_is_confirmation_matches_canonical_words() -> None:
    assert FinancialAdvisorAgent._is_confirmation("confirm") is True
    assert FinancialAdvisorAgent._is_confirmation("Yes please") is True
    assert FinancialAdvisorAgent._is_confirmation("looks good") is True
    assert FinancialAdvisorAgent._is_confirmation("no thanks") is False


# ---------------------------------------------------------------------------
# build_ips
# ---------------------------------------------------------------------------


def test_build_ips_raises_when_required_missing() -> None:
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-1", "user-1")
    # Fill nothing required.
    with pytest.raises(ValueError) as excinfo:
        agent.build_ips(session)
    assert "missing required fields" in str(excinfo.value).lower()


def test_build_ips_fills_required_fields_only() -> None:
    """When all required fields are present, build_ips returns a complete IPS."""
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-1", "user-1")
    c = session.collected
    c.risk_tolerance = "medium"
    c.max_drawdown_tolerance_pct = 20.0
    c.time_horizon_years = 10
    c.annual_gross_income = 120000
    c.total_net_worth = 200000
    c.investable_assets = 150000
    # Optional defaults exercised via missing values.
    ips = agent.build_ips(session)
    assert ips.profile.user_id == "user-1"
    assert ips.profile.savings_rate.annual == 0  # monthly None, annual None
    assert ips.live_trading_enabled is False  # default
    assert ips.human_approval_required_for_live is True  # default
    assert ips.profile.preferences.crypto_allowed is True  # default


def test_build_ips_uses_monthly_to_compute_annual_savings() -> None:
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-1", "user-1")
    c = session.collected
    c.risk_tolerance = "medium"
    c.max_drawdown_tolerance_pct = 20.0
    c.time_horizon_years = 10
    c.annual_gross_income = 100_000
    c.total_net_worth = 200_000
    c.investable_assets = 150_000
    c.monthly_savings = 500
    ips = agent.build_ips(session)
    assert ips.profile.savings_rate.annual == 6_000


def test_build_ips_invalid_default_mode_falls_back_to_monitor_only() -> None:
    agent = FinancialAdvisorAgent()
    session = agent.start_session("adv-1", "user-1")
    c = session.collected
    c.risk_tolerance = "medium"
    c.max_drawdown_tolerance_pct = 20.0
    c.time_horizon_years = 10
    c.annual_gross_income = 100_000
    c.total_net_worth = 200_000
    c.investable_assets = 150_000
    c.default_mode = "not-a-valid-mode"
    ips = agent.build_ips(session)
    # Falls back to MONITOR_ONLY rather than raising.
    assert ips.default_mode.value == "monitor_only"


def test_missing_fields_lists_only_required_holes() -> None:
    c = CollectedProfileData()
    missing = FinancialAdvisorAgent.missing_fields(c)
    assert set(missing) == {
        "risk_tolerance",
        "max_drawdown_tolerance_pct",
        "time_horizon_years",
        "annual_gross_income",
        "total_net_worth",
        "investable_assets",
    }


# ---------------------------------------------------------------------------
# ValidationAgent + PromotionGateAgent
# ---------------------------------------------------------------------------


def test_validation_agent_reports_missing_checks() -> None:
    report = ValidationReport(
        strategy_id="s",
        generated_by="v",
        data_snapshot_id="snap",
        backtest_period="2020-2024",
        scenario_set=["baseline"],
        checks=[
            ValidationCheck(name="backtest_quality", status=ValidationStatus.PASS, details=""),
        ],
    )
    failures = ValidationAgent().checklist_failures(report)
    assert any("walk_forward" in f for f in failures)
    assert any("stress_test" in f for f in failures)


def test_validation_agent_reports_failed_checks() -> None:
    report = ValidationReport(
        strategy_id="s",
        generated_by="v",
        data_snapshot_id="snap",
        backtest_period="",
        scenario_set=[],
        checks=[
            ValidationCheck(name="backtest_quality", status=ValidationStatus.FAIL, details="bad"),
            ValidationCheck(name="walk_forward", status=ValidationStatus.PASS, details=""),
            ValidationCheck(name="stress_test", status=ValidationStatus.PASS, details=""),
            ValidationCheck(name="transaction_cost_model", status=ValidationStatus.PASS, details=""),
            ValidationCheck(name="liquidity_impact", status=ValidationStatus.PASS, details=""),
        ],
    )
    failures = ValidationAgent().checklist_failures(report)
    assert any("backtest_quality" in f and "bad" in f for f in failures)


def _valid_validation_report(strategy_id: str = "s") -> ValidationReport:
    return ValidationReport(
        strategy_id=strategy_id,
        generated_by="v",
        data_snapshot_id="snap",
        backtest_period="",
        scenario_set=[],
        checks=[
            ValidationCheck(name="backtest_quality", status=ValidationStatus.PASS, details=""),
            ValidationCheck(name="walk_forward", status=ValidationStatus.PASS, details=""),
            ValidationCheck(name="stress_test", status=ValidationStatus.PASS, details=""),
            ValidationCheck(name="transaction_cost_model", status=ValidationStatus.PASS, details=""),
            ValidationCheck(name="liquidity_impact", status=ValidationStatus.PASS, details=""),
        ],
    )


def _ips(*, live_enabled: bool, approval_required: bool = True):
    from investment_team.tests.test_investment_team import _sample_ips

    ips = _sample_ips()
    ips.live_trading_enabled = live_enabled
    ips.human_approval_required_for_live = approval_required
    return ips


def _strategy() -> StrategySpec:
    return StrategySpec(
        strategy_id="s",
        authored_by="proposer-1",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )


def test_promotion_gate_rejects_self_approval() -> None:
    agent = PromotionGateAgent()
    decision = agent.decide(
        strategy=_strategy(),
        validation=_valid_validation_report(),
        ips=_ips(live_enabled=True),
        proposer_agent_id="bob",
        approver=AgentIdentity(agent_id="bob", role="approver", version="1.0"),
        risk_veto=False,
    )
    assert decision.outcome == PromotionStage.REJECT
    assert any("self-approve" in (r.details or "") for r in decision.gate_results)


def test_promotion_gate_rejects_on_risk_veto() -> None:
    agent = PromotionGateAgent()
    decision = agent.decide(
        strategy=_strategy(),
        validation=_valid_validation_report(),
        ips=_ips(live_enabled=True),
        proposer_agent_id="alice",
        approver=AgentIdentity(agent_id="bob", role="approver", version="1.0"),
        risk_veto=True,
    )
    assert decision.outcome == PromotionStage.REJECT
    assert any(r.gate.value == "risk_veto" for r in decision.gate_results)


def test_promotion_gate_revise_on_validation_failures() -> None:
    """Failing validation → REVISE with the failing-check details echoed."""
    bad_report = ValidationReport(
        strategy_id="s",
        generated_by="v",
        data_snapshot_id="snap",
        backtest_period="",
        scenario_set=[],
        checks=[
            ValidationCheck(name="backtest_quality", status=ValidationStatus.FAIL, details="bad"),
            ValidationCheck(name="walk_forward", status=ValidationStatus.PASS, details=""),
            ValidationCheck(name="stress_test", status=ValidationStatus.PASS, details=""),
            ValidationCheck(name="transaction_cost_model", status=ValidationStatus.PASS, details=""),
            ValidationCheck(name="liquidity_impact", status=ValidationStatus.PASS, details=""),
        ],
    )
    decision = PromotionGateAgent().decide(
        strategy=_strategy(),
        validation=bad_report,
        ips=_ips(live_enabled=True),
        proposer_agent_id="alice",
        approver=AgentIdentity(agent_id="bob", role="approver", version="1.0"),
        risk_veto=False,
    )
    assert decision.outcome == PromotionStage.REVISE


def test_promotion_gate_revise_when_validation_strategy_id_mismatch() -> None:
    """Validation strategy_id != strategy.strategy_id → adds mismatch failure."""
    report = _valid_validation_report(strategy_id="OTHER-ID")
    decision = PromotionGateAgent().decide(
        strategy=_strategy(),
        validation=report,
        ips=_ips(live_enabled=True),
        proposer_agent_id="alice",
        approver=AgentIdentity(agent_id="bob", role="approver", version="1.0"),
        risk_veto=False,
    )
    assert decision.outcome == PromotionStage.REVISE


def test_promotion_gate_paper_when_ips_disallows_live() -> None:
    decision = PromotionGateAgent().decide(
        strategy=_strategy(),
        validation=_valid_validation_report(),
        ips=_ips(live_enabled=False),
        proposer_agent_id="alice",
        approver=AgentIdentity(agent_id="bob", role="approver", version="1.0"),
        risk_veto=False,
    )
    assert decision.outcome == PromotionStage.PAPER
    assert any(r.gate.value == "ips_permission" for r in decision.gate_results)


def test_promotion_gate_live_when_all_clear_with_human_approval() -> None:
    decision = PromotionGateAgent().decide(
        strategy=_strategy(),
        validation=_valid_validation_report(),
        ips=_ips(live_enabled=True),
        proposer_agent_id="alice",
        approver=AgentIdentity(agent_id="bob", role="approver", version="1.0"),
        risk_veto=False,
        human_live_approval=True,
    )
    assert decision.outcome == PromotionStage.LIVE
    assert isinstance(decision.audit, AuditContext)
