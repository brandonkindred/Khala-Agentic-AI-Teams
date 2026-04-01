"""Agent roles and decision logic for the investment organization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    IPS,
    AdvisorSession,
    AdvisorSessionStatus,
    AdvisorTopic,
    AuditContext,
    ChatMessage,
    CollectedProfileData,
    GateCheckResult,
    GateResult,
    IncomeProfile,
    InvestmentCommitteeMemo,
    InvestmentProfile,
    LiquidityNeeds,
    NetWorth,
    PortfolioConstraints,
    PortfolioProposal,
    PromotionDecision,
    PromotionGate,
    PromotionStage,
    RiskTolerance,
    SavingsRate,
    StrategySpec,
    TaxProfile,
    UserGoal,
    UserPreferences,
    ValidationReport,
    ValidationStatus,
    WorkflowMode,
)


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str
    role: str
    version: str


class PolicyGuardianAgent:
    """Enforces IPS hard constraints before any recommendation advances."""

    def check_portfolio(self, ips: IPS, proposal: PortfolioProposal) -> List[str]:
        violations: List[str] = []
        constraints = ips.profile.constraints

        if not proposal.positions:
            return ["Portfolio has no positions."]

        asset_class_weights: Dict[str, float] = {}
        for pos in proposal.positions:
            asset_class_weights[pos.asset_class] = (
                asset_class_weights.get(pos.asset_class, 0.0) + pos.weight_pct
            )

            if pos.weight_pct > constraints.max_single_position_pct:
                violations.append(
                    f"{pos.symbol} exceeds single-position cap "
                    f"({pos.weight_pct}% > {constraints.max_single_position_pct}%)."
                )

        total_weight = sum(pos.weight_pct for pos in proposal.positions)
        if total_weight > 100:
            violations.append(f"Total portfolio weight exceeds 100% ({total_weight}%).")

        for asset_class, weight in sorted(asset_class_weights.items()):
            class_cap = constraints.max_asset_class_pct.get(asset_class)
            if class_cap is not None and weight > class_cap:
                violations.append(
                    f"Asset class {asset_class} exceeds cap ({weight}% > {class_cap}%)."
                )

        excluded_classes = set(ips.profile.preferences.excluded_asset_classes)
        for asset_class in sorted(excluded_classes):
            if asset_class_weights.get(asset_class, 0) > 0:
                violations.append(f"Asset class {asset_class} is excluded by IPS preferences.")

        if not ips.profile.preferences.crypto_allowed and asset_class_weights.get("crypto", 0) > 0:
            violations.append("Crypto position present despite IPS disallowing crypto.")
        if (
            not ips.profile.preferences.options_allowed
            and asset_class_weights.get("options", 0) > 0
        ):
            violations.append("Options position present despite IPS disallowing options.")

        speculative_weight = sum(
            pos.weight_pct for pos in proposal.positions if pos.asset_class in {"crypto", "options"}
        )
        if speculative_weight > ips.speculative_sleeve_cap_pct:
            violations.append(
                f"Speculative sleeve exceeds IPS cap ({speculative_weight}% > {ips.speculative_sleeve_cap_pct}%)."
            )

        return violations


class ValidationAgent:
    """Checks if a strategy passed required testing and scenario standards."""

    REQUIRED_CHECKS = {
        "backtest_quality",
        "walk_forward",
        "stress_test",
        "transaction_cost_model",
        "liquidity_impact",
    }

    def checklist_failures(self, report: ValidationReport) -> List[str]:
        failures: List[str] = []
        present = {c.name for c in report.checks}

        for required in sorted(self.REQUIRED_CHECKS - present):
            failures.append(f"Missing required validation check: {required}.")

        for check in report.checks:
            if check.status == ValidationStatus.FAIL:
                failures.append(f"Failed validation check: {check.name} ({check.details}).")

        return failures


class PromotionGateAgent:
    """Applies universal promotion checklist and enforces separation-of-duties gates."""

    _validator = ValidationAgent()

    def decide(
        self,
        strategy: StrategySpec,
        validation: ValidationReport,
        ips: IPS,
        proposer_agent_id: str,
        approver: AgentIdentity,
        risk_veto: bool,
        human_live_approval: bool = False,
    ) -> PromotionDecision:
        gate_results: List[GateCheckResult] = []

        def build_audit() -> AuditContext:
            return AuditContext(
                data_snapshot_id=validation.data_snapshot_id,
                assumptions=strategy.audit.assumptions,
                gate_trace=[f"{r.gate.value}:{r.result.value}" for r in gate_results],
                agent_versions={
                    "proposer": strategy.authored_by,
                    "approver": approver.version,
                    "validator": validation.generated_by,
                },
            )

        if proposer_agent_id == approver.agent_id:
            gate_results.append(
                GateCheckResult(
                    gate=PromotionGate.SEPARATION_OF_DUTIES,
                    result=GateResult.FAIL,
                    details="Proposer cannot self-approve.",
                )
            )
            return PromotionDecision(
                strategy_id=strategy.strategy_id,
                decided_by=approver.agent_id,
                outcome=PromotionStage.REJECT,
                rationale="Separation-of-duties violation: proposer cannot self-approve.",
                required_actions=["Assign independent approval agent."],
                gate_results=gate_results,
                audit=build_audit(),
            )
        gate_results.append(
            GateCheckResult(
                gate=PromotionGate.SEPARATION_OF_DUTIES,
                result=GateResult.PASS,
                details="Independent approver verified.",
            )
        )

        if risk_veto:
            gate_results.append(
                GateCheckResult(
                    gate=PromotionGate.RISK_VETO,
                    result=GateResult.FAIL,
                    details="Risk management veto invoked.",
                )
            )
            return PromotionDecision(
                strategy_id=strategy.strategy_id,
                decided_by=approver.agent_id,
                outcome=PromotionStage.REJECT,
                rationale="Risk management veto invoked.",
                required_actions=["Address risk concerns and rerun validation."],
                gate_results=gate_results,
                audit=build_audit(),
            )
        gate_results.append(
            GateCheckResult(
                gate=PromotionGate.RISK_VETO,
                result=GateResult.PASS,
                details="No veto from risk management.",
            )
        )

        failures = self._validator.checklist_failures(validation)
        if validation.strategy_id != strategy.strategy_id:
            failures.append("Validation report strategy_id does not match strategy spec.")
        if failures:
            gate_results.append(
                GateCheckResult(
                    gate=PromotionGate.VALIDATION,
                    result=GateResult.FAIL,
                    details="Validation checklist not satisfied.",
                )
            )
            return PromotionDecision(
                strategy_id=strategy.strategy_id,
                decided_by=approver.agent_id,
                outcome=PromotionStage.REVISE,
                rationale="Validation checklist not satisfied.",
                required_actions=failures,
                gate_results=gate_results,
                audit=build_audit(),
            )
        gate_results.append(
            GateCheckResult(
                gate=PromotionGate.VALIDATION,
                result=GateResult.PASS,
                details="All required validation checks passed.",
            )
        )

        if not ips.live_trading_enabled:
            gate_results.append(
                GateCheckResult(
                    gate=PromotionGate.IPS_PERMISSION,
                    result=GateResult.WARN,
                    details="IPS does not permit live trading.",
                )
            )
            return PromotionDecision(
                strategy_id=strategy.strategy_id,
                decided_by=approver.agent_id,
                outcome=PromotionStage.PAPER,
                rationale="IPS does not permit live trading; defaulting to paper mode.",
                required_actions=[
                    "Obtain explicit human approval + IPS update for live promotion."
                ],
                gate_results=gate_results,
                audit=build_audit(),
            )

        gate_results.append(
            GateCheckResult(
                gate=PromotionGate.IPS_PERMISSION,
                result=GateResult.PASS,
                details="IPS permits live trading.",
            )
        )

        if ips.human_approval_required_for_live and not human_live_approval:
            gate_results.append(
                GateCheckResult(
                    gate=PromotionGate.HUMAN_APPROVAL,
                    result=GateResult.WARN,
                    details="Human live approval pending.",
                )
            )
            return PromotionDecision(
                strategy_id=strategy.strategy_id,
                decided_by=approver.agent_id,
                outcome=PromotionStage.PAPER,
                rationale="Live trading requires explicit human approval.",
                required_actions=["Obtain human approval before live promotion."],
                gate_results=gate_results,
                audit=build_audit(),
            )

        gate_results.append(
            GateCheckResult(
                gate=PromotionGate.HUMAN_APPROVAL,
                result=GateResult.PASS,
                details="Human live approval confirmed.",
            )
        )

        return PromotionDecision(
            strategy_id=strategy.strategy_id,
            decided_by=approver.agent_id,
            outcome=PromotionStage.LIVE,
            rationale="All checklist gates passed, no veto, IPS allows live trading, and human approval was recorded.",
            required_actions=["Enable tight risk limits and monitor-first rollout window."],
            gate_results=gate_results,
            audit=build_audit(),
        )


class InvestmentCommitteeAgent:
    """Produces a user-facing recommendation memo with rationale and dissent."""

    def draft_memo(
        self,
        user_id: str,
        recommendation: str,
        rationale: str,
        dissenting_views: List[str] | None = None,
    ) -> InvestmentCommitteeMemo:
        return InvestmentCommitteeMemo(
            memo_id=f"icm-{user_id}",
            prepared_for_user_id=user_id,
            recommendation=recommendation,
            rationale=rationale,
            dissenting_views=dissenting_views or [],
            attachments=[],
        )


# ---------------------------------------------------------------------------
# Financial Advisor — conversational profile-builder
# ---------------------------------------------------------------------------

# Maps each topic to the questions the advisor asks and which collected fields
# it populates.  The advisor walks through topics in AdvisorTopic order.
_TOPIC_QUESTIONS: Dict[AdvisorTopic, str] = {
    AdvisorTopic.GREETING: (
        "Welcome! I'm your financial advisor assistant. I'll help you build your "
        "investment profile by asking a series of questions — no tedious forms required. "
        "Let's get started.\n\n"
        "First, how would you describe your comfort level with investment risk? "
        "For example: low (preserve capital), medium (balanced growth), "
        "high (aggressive growth), or very high (maximum returns, accept large swings)."
    ),
    AdvisorTopic.RISK_TOLERANCE: (
        "Thanks. And what's the maximum portfolio drawdown (percentage drop from peak) "
        "you could tolerate before you'd lose sleep? For example, 10%, 20%, 30%?"
    ),
    AdvisorTopic.TIME_HORIZON: (
        "How many years do you plan to keep this money invested before you'll need it? "
        "For instance, 5 years, 10 years, 20+ years?"
    ),
    AdvisorTopic.INCOME: (
        "Let's talk about your income. What is your approximate annual gross income, "
        "and would you describe your income as stable, variable, or uncertain?"
    ),
    AdvisorTopic.NET_WORTH: (
        "What is your approximate total net worth, and how much of that "
        "would you consider investable assets (i.e. cash and securities you "
        "could allocate to an investment portfolio)?"
    ),
    AdvisorTopic.SAVINGS: (
        "Roughly how much do you save each month? "
        "(I'll calculate the annual figure for you if you just give me the monthly number.)"
    ),
    AdvisorTopic.TAX: (
        "A couple of tax-related questions: What country do you file taxes in? "
        "If the US, which state? And what types of investment accounts do you have "
        "or plan to use? (e.g. taxable brokerage, 401k, IRA, Roth IRA, etc.)"
    ),
    AdvisorTopic.LIQUIDITY: (
        "How many months of living expenses do you keep as an emergency fund? "
        "And do you have any large planned expenses coming up "
        "(like buying a house, car, wedding, tuition)? If so, what and roughly when/how much?"
    ),
    AdvisorTopic.GOALS: (
        "What are your main investment goals? For each, I'd love to know:\n"
        "- The goal name (e.g. retirement, house down payment, college fund)\n"
        "- A target dollar amount\n"
        "- A target date\n"
        "- Priority (high, medium, or low)\n\n"
        "Feel free to list as many as you like."
    ),
    AdvisorTopic.PREFERENCES: (
        "Do you have any investment preferences or exclusions?\n"
        "- Any asset classes you want to avoid? (e.g. crypto, options, commodities)\n"
        "- Any industries to exclude? (e.g. tobacco, firearms, fossil fuels)\n"
        "- Do you care about ESG/socially responsible investing?\n"
        "- Are you okay with cryptocurrency? Options? Leverage?"
    ),
    AdvisorTopic.CONSTRAINTS: (
        "Almost there! Do you have any portfolio concentration limits in mind?\n"
        "- Maximum percentage in any single position? (default is 10%)\n"
        "- Maximum percentage in any asset class? "
        "(e.g. no more than 60% equities, 10% crypto, etc.)"
    ),
    AdvisorTopic.TRADING_PREFERENCES: (
        "Last section — trading preferences:\n"
        "- Would you like to enable live trading, or keep it in advisory/paper mode?\n"
        "- Should live trades require your manual approval?\n"
        "- What cap would you set on speculative positions? (default 10%)\n"
        "- How often should the portfolio rebalance? (quarterly, monthly, annually)"
    ),
    AdvisorTopic.REVIEW: (
        "That's everything I need! Here's a summary of what I've collected. "
        "Please review it and let me know if anything needs to change. "
        "When you're happy, just say 'looks good' or 'confirm' and I'll finalize "
        "your Investment Policy Statement."
    ),
}

_TOPIC_ORDER: List[AdvisorTopic] = list(AdvisorTopic)


def _next_topic(current: AdvisorTopic) -> Optional[AdvisorTopic]:
    """Return the topic after *current*, or None if we've reached the end."""
    idx = _TOPIC_ORDER.index(current)
    if idx + 1 < len(_TOPIC_ORDER):
        return _TOPIC_ORDER[idx + 1]
    return None


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class FinancialAdvisorAgent:
    """Conversational agent that collects investment profile data from a user.

    Instead of making the user fill out a giant form, the advisor walks through
    a structured series of friendly questions, extracts the relevant data from
    each user reply, and accumulates a ``CollectedProfileData``.  When all
    topics are covered the advisor can build a complete ``IPS``.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_session(self, session_id: str, user_id: str) -> AdvisorSession:
        """Create a new advisor conversation and return the opening message."""
        now = _now_iso()
        greeting = _TOPIC_QUESTIONS[AdvisorTopic.GREETING]
        session = AdvisorSession(
            session_id=session_id,
            user_id=user_id,
            status=AdvisorSessionStatus.ACTIVE,
            current_topic=AdvisorTopic.GREETING,
            messages=[ChatMessage(role="advisor", content=greeting, timestamp=now)],
            collected=CollectedProfileData(),
            created_at=now,
            updated_at=now,
        )
        return session

    def handle_message(self, session: AdvisorSession, user_message: str) -> str:
        """Process a user message and return the advisor's next response.

        Mutates *session* in-place (appends messages, advances topic, updates
        collected data).
        """
        if session.status != AdvisorSessionStatus.ACTIVE:
            return "This session is no longer active. Please start a new session."

        now = _now_iso()
        session.messages.append(ChatMessage(role="user", content=user_message, timestamp=now))
        session.updated_at = now

        # If we're in REVIEW and the user confirms, finalize.
        if session.current_topic == AdvisorTopic.REVIEW:
            if self._is_confirmation(user_message):
                session.status = AdvisorSessionStatus.COMPLETED
                reply = (
                    "Your Investment Policy Statement has been created successfully! "
                    "You can now view your profile or start exploring investment strategies."
                )
                session.messages.append(
                    ChatMessage(role="advisor", content=reply, timestamp=_now_iso())
                )
                return reply
            else:
                # User wants to change something — try to apply edits
                reply = (
                    "No problem — tell me what you'd like to change and I'll update it. "
                    "When you're satisfied, just say 'confirm'."
                )
                session.messages.append(
                    ChatMessage(role="advisor", content=reply, timestamp=_now_iso())
                )
                return reply

        # Extract data from the user's reply for the current topic.
        self._extract_topic_data(session, user_message)

        # Advance to the next topic.
        next_topic = _next_topic(session.current_topic)
        if next_topic is None:
            session.status = AdvisorSessionStatus.COMPLETED
            reply = "Your Investment Policy Statement has been created. Thank you!"
            session.messages.append(
                ChatMessage(role="advisor", content=reply, timestamp=_now_iso())
            )
            return reply

        session.current_topic = next_topic
        reply = _TOPIC_QUESTIONS[next_topic]

        # If we've reached REVIEW, prepend the summary.
        if next_topic == AdvisorTopic.REVIEW:
            summary = self._build_summary(session.collected)
            reply = reply + "\n\n" + summary

        session.messages.append(ChatMessage(role="advisor", content=reply, timestamp=_now_iso()))
        return reply

    def build_ips(self, session: AdvisorSession) -> IPS:
        """Convert collected data into a full IPS.  Raises ValueError if required
        fields are missing."""
        c = session.collected
        missing = self.missing_fields(c)
        if missing:
            raise ValueError(f"Cannot build IPS — missing required fields: {', '.join(missing)}")

        profile = InvestmentProfile(
            user_id=session.user_id,
            created_at=session.created_at,
            risk_tolerance=RiskTolerance(c.risk_tolerance),
            max_drawdown_tolerance_pct=c.max_drawdown_tolerance_pct,  # type: ignore[arg-type]
            time_horizon_years=c.time_horizon_years,  # type: ignore[arg-type]
            liquidity_needs=LiquidityNeeds(
                emergency_fund_months=c.emergency_fund_months or 6,
                planned_large_expenses=c.planned_large_expenses,
            ),
            income=IncomeProfile(
                annual_gross=c.annual_gross_income or 0,
                stability=c.income_stability or "stable",
            ),
            net_worth=NetWorth(
                total=c.total_net_worth or 0,
                investable_assets=c.investable_assets or 0,
            ),
            savings_rate=SavingsRate(
                monthly=c.monthly_savings or 0,
                annual=c.annual_savings or (c.monthly_savings or 0) * 12,
            ),
            tax_profile=TaxProfile(
                country=c.tax_country or "US",
                state=c.tax_state or "",
                account_types=c.account_types,
            ),
            preferences=UserPreferences(
                excluded_asset_classes=c.excluded_asset_classes,
                excluded_industries=c.excluded_industries,
                esg_preference=c.esg_preference or "none",
                crypto_allowed=c.crypto_allowed if c.crypto_allowed is not None else True,
                options_allowed=c.options_allowed if c.options_allowed is not None else True,
                leverage_allowed=c.leverage_allowed if c.leverage_allowed is not None else False,
            ),
            goals=c.goals,
            constraints=PortfolioConstraints(
                max_single_position_pct=c.max_single_position_pct or 10,
                max_asset_class_pct=c.max_asset_class_pct,
            ),
        )

        try:
            workflow_mode = (
                WorkflowMode(c.default_mode) if c.default_mode else WorkflowMode.MONITOR_ONLY
            )
        except ValueError:
            workflow_mode = WorkflowMode.MONITOR_ONLY

        return IPS(
            profile=profile,
            live_trading_enabled=c.live_trading_enabled
            if c.live_trading_enabled is not None
            else False,
            human_approval_required_for_live=(
                c.human_approval_required_for_live
                if c.human_approval_required_for_live is not None
                else True
            ),
            speculative_sleeve_cap_pct=c.speculative_sleeve_cap_pct or 10,
            rebalance_frequency=c.rebalance_frequency or "quarterly",
            default_mode=workflow_mode,
        )

    @staticmethod
    def missing_fields(collected: CollectedProfileData) -> List[str]:
        """Return a list of required fields that have not been collected yet."""
        required: List[Tuple[str, Any]] = [
            ("risk_tolerance", collected.risk_tolerance),
            ("max_drawdown_tolerance_pct", collected.max_drawdown_tolerance_pct),
            ("time_horizon_years", collected.time_horizon_years),
            ("annual_gross_income", collected.annual_gross_income),
            ("total_net_worth", collected.total_net_worth),
            ("investable_assets", collected.investable_assets),
        ]
        return [name for name, value in required if value is None]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_confirmation(text: str) -> bool:
        lowered = text.strip().lower()
        confirms = {
            "confirm",
            "looks good",
            "yes",
            "lgtm",
            "approved",
            "ok",
            "okay",
            "done",
            "correct",
            "y",
        }
        return any(re.search(rf"\b{re.escape(c)}\b", lowered) for c in confirms)

    def _extract_topic_data(self, session: AdvisorSession, text: str) -> None:  # noqa: C901
        """Parse the user reply and populate the relevant collected fields."""
        topic = session.current_topic
        c = session.collected
        lowered = text.strip().lower()

        if topic == AdvisorTopic.GREETING:
            # First real answer — risk tolerance
            for level in ("very_high", "very high", "high", "medium", "low"):
                if level in lowered:
                    c.risk_tolerance = level.replace(" ", "_")
                    break
            if c.risk_tolerance is None:
                c.risk_tolerance = "medium"

        elif topic == AdvisorTopic.RISK_TOLERANCE:
            pct = self._extract_number(text)
            c.max_drawdown_tolerance_pct = pct if pct is not None else 20.0

        elif topic == AdvisorTopic.TIME_HORIZON:
            years = self._extract_number(text)
            c.time_horizon_years = int(years) if years is not None else 10

        elif topic == AdvisorTopic.INCOME:
            numbers = self._extract_all_numbers(text)
            if numbers:
                c.annual_gross_income = numbers[0]
            for kw in ("stable", "variable", "uncertain"):
                if kw in lowered:
                    c.income_stability = kw
                    break
            if c.income_stability is None:
                c.income_stability = "stable"

        elif topic == AdvisorTopic.NET_WORTH:
            numbers = self._extract_all_numbers(text)
            if len(numbers) >= 2:
                c.total_net_worth = numbers[0]
                c.investable_assets = numbers[1]
            elif len(numbers) == 1:
                c.total_net_worth = numbers[0]
                c.investable_assets = numbers[0]

        elif topic == AdvisorTopic.SAVINGS:
            amount = self._extract_number(text)
            if amount is not None:
                c.monthly_savings = amount
                c.annual_savings = amount * 12

        elif topic == AdvisorTopic.TAX:
            # Country
            if "us" in lowered or "united states" in lowered or "america" in lowered:
                c.tax_country = "US"
            elif "uk" in lowered or "united kingdom" in lowered:
                c.tax_country = "UK"
            elif "canada" in lowered:
                c.tax_country = "CA"
            else:
                c.tax_country = "US"

            # State (simple extraction for US states)
            us_states = {
                "california": "CA",
                "ca": "CA",
                "new york": "NY",
                "ny": "NY",
                "texas": "TX",
                "tx": "TX",
                "florida": "FL",
                "fl": "FL",
                "illinois": "IL",
                "il": "IL",
                "washington": "WA",
                "wa": "WA",
                "colorado": "CO",
                "co": "CO",
                "massachusetts": "MA",
                "ma": "MA",
                "georgia": "GA",
                "ga": "GA",
                "pennsylvania": "PA",
                "pa": "PA",
                "ohio": "OH",
                "virginia": "VA",
                "va": "VA",
                "north carolina": "NC",
                "nc": "NC",
                "new jersey": "NJ",
                "nj": "NJ",
                "arizona": "AZ",
                "az": "AZ",
                "michigan": "MI",
                "mi": "MI",
                "oregon": "OR",
                "nevada": "NV",
                "nv": "NV",
                "minnesota": "MN",
                "mn": "MN",
                "tennessee": "TN",
                "tn": "TN",
                "maryland": "MD",
                "md": "MD",
                "utah": "UT",
                "connecticut": "CT",
            }
            for name, abbr in us_states.items():
                if name in lowered:
                    c.tax_state = abbr
                    break

            # Account types
            account_keywords = {
                "taxable": "taxable",
                "brokerage": "taxable",
                "401k": "401k",
                "401(k)": "401k",
                "ira": "ira",
                "traditional ira": "ira",
                "roth": "roth_ira",
                "roth ira": "roth_ira",
                "hsa": "hsa",
                "529": "529",
            }
            found_accounts: List[str] = []
            for kw, acct in account_keywords.items():
                if kw in lowered and acct not in found_accounts:
                    found_accounts.append(acct)
            c.account_types = found_accounts if found_accounts else ["taxable"]

        elif topic == AdvisorTopic.LIQUIDITY:
            numbers = self._extract_all_numbers(text)
            if numbers:
                c.emergency_fund_months = int(numbers[0])
            else:
                c.emergency_fund_months = 6
            # Planned expenses are complex — for now store if user mentions them
            # The LLM-powered version would parse these more thoroughly

        elif topic == AdvisorTopic.GOALS:
            # Simple goal extraction — look for common goal keywords
            goal_keywords = {
                "retire": "retirement",
                "retirement": "retirement",
                "house": "house_down_payment",
                "home": "house_down_payment",
                "down payment": "house_down_payment",
                "college": "college_fund",
                "education": "education_fund",
                "emergency": "emergency_fund",
                "travel": "travel",
                "wedding": "wedding",
                "car": "car_purchase",
            }
            found_goals: List[str] = []
            for kw, goal_name in goal_keywords.items():
                if kw in lowered and goal_name not in found_goals:
                    found_goals.append(goal_name)

            numbers = self._extract_all_numbers(text)
            for i, goal_name in enumerate(found_goals):
                target_amount = numbers[i] if i < len(numbers) else 100000
                c.goals.append(
                    UserGoal(
                        name=goal_name,
                        target_amount=target_amount,
                        target_date="",
                        priority="high" if i == 0 else "medium",
                    )
                )

            if not c.goals:
                c.goals.append(
                    UserGoal(
                        name="general_growth",
                        target_amount=100000,
                        target_date="",
                        priority="medium",
                    )
                )

        elif topic == AdvisorTopic.PREFERENCES:
            # Asset class exclusions
            exclusion_map = {
                "crypto": "crypto",
                "cryptocurrency": "crypto",
                "options": "options",
                "commodities": "commodities",
                "real estate": "real_estate",
                "forex": "fx",
                "fx": "fx",
            }
            for kw, cls in exclusion_map.items():
                if kw in lowered and (
                    "no " + kw in lowered or "avoid " + kw in lowered or "exclude " + kw in lowered
                ):
                    if cls not in c.excluded_asset_classes:
                        c.excluded_asset_classes.append(cls)

            # ESG
            if "esg" in lowered or "socially responsible" in lowered or "sustainable" in lowered:
                c.esg_preference = "strong" if "strong" in lowered else "moderate"
            else:
                c.esg_preference = "none"

            # Crypto / options / leverage
            c.crypto_allowed = "no crypto" not in lowered and "avoid crypto" not in lowered
            c.options_allowed = "no option" not in lowered and "avoid option" not in lowered
            c.leverage_allowed = "yes" in lowered and "leverage" in lowered

        elif topic == AdvisorTopic.CONSTRAINTS:
            numbers = self._extract_all_numbers(text)
            if numbers:
                c.max_single_position_pct = numbers[0]
            else:
                c.max_single_position_pct = 10.0

            # Parse asset class caps like "60% equities, 10% crypto"
            cap_pattern = re.compile(
                r"(\d+)%?\s*(equit|stock|bond|crypto|option|real.?estate|fx|commodit)", re.I
            )
            for match in cap_pattern.finditer(text):
                pct = float(match.group(1))
                asset_raw = match.group(2).lower()
                asset_map = {
                    "equit": "equities",
                    "stock": "equities",
                    "bond": "bonds_treasuries",
                    "crypto": "crypto",
                    "option": "options",
                    "real": "real_estate",
                    "fx": "fx",
                    "commodit": "commodities",
                }
                for prefix, cls in asset_map.items():
                    if asset_raw.startswith(prefix):
                        c.max_asset_class_pct[cls] = pct
                        break

        elif topic == AdvisorTopic.TRADING_PREFERENCES:
            c.live_trading_enabled = (
                "live" in lowered and "no" not in lowered.split("live")[0][-10:]
            )
            c.human_approval_required_for_live = (
                "manual" in lowered or "approval" in lowered or "approve" in lowered
            )
            if c.human_approval_required_for_live is False:
                c.human_approval_required_for_live = True  # safe default

            pct = self._extract_number(text)
            c.speculative_sleeve_cap_pct = pct if pct is not None else 10.0

            for freq in ("monthly", "quarterly", "annually", "weekly"):
                if freq in lowered:
                    c.rebalance_frequency = freq
                    break
            if c.rebalance_frequency is None:
                c.rebalance_frequency = "quarterly"

            if "advisory" in lowered:
                c.default_mode = "advisory"
            elif "paper" in lowered:
                c.default_mode = "paper"
            elif "live" in lowered:
                c.default_mode = "live"
            else:
                c.default_mode = "monitor_only"

    @staticmethod
    def _extract_number(text: str) -> Optional[float]:
        """Extract the first number from text, handling k/m/b suffixes and commas."""
        nums = FinancialAdvisorAgent._extract_all_numbers(text)
        return nums[0] if nums else None

    @staticmethod
    def _extract_all_numbers(text: str) -> List[float]:
        """Extract all numbers from text, handling k/m/b suffixes and commas."""
        text = text.replace(",", "").replace("$", "")
        results: List[float] = []
        for match in re.finditer(r"(\d+(?:\.\d+)?)\s*([kmb])?", text, re.I):
            value = float(match.group(1))
            suffix = (match.group(2) or "").lower()
            multipliers = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
            results.append(value * multipliers.get(suffix, 1))
        return results

    @staticmethod
    def _build_summary(c: CollectedProfileData) -> str:
        """Build a human-readable summary of collected data."""
        lines = [
            "**Profile Summary**",
            f"- Risk tolerance: {c.risk_tolerance or 'not set'}",
            f"- Max drawdown tolerance: {c.max_drawdown_tolerance_pct or 'not set'}%",
            f"- Time horizon: {c.time_horizon_years or 'not set'} years",
            f"- Annual gross income: ${c.annual_gross_income:,.0f}"
            if c.annual_gross_income
            else "- Annual income: not set",
            f"- Income stability: {c.income_stability or 'not set'}",
            f"- Total net worth: ${c.total_net_worth:,.0f}"
            if c.total_net_worth
            else "- Net worth: not set",
            f"- Investable assets: ${c.investable_assets:,.0f}"
            if c.investable_assets
            else "- Investable assets: not set",
            f"- Monthly savings: ${c.monthly_savings:,.0f}"
            if c.monthly_savings
            else "- Monthly savings: not set",
            f"- Tax country: {c.tax_country or 'US'}, State: {c.tax_state or 'N/A'}",
            f"- Account types: {', '.join(c.account_types) if c.account_types else 'taxable'}",
            f"- Emergency fund: {c.emergency_fund_months or 6} months",
            f"- Goals: {', '.join(g.name for g in c.goals) if c.goals else 'none specified'}",
            f"- ESG preference: {c.esg_preference or 'none'}",
            f"- Crypto allowed: {c.crypto_allowed}",
            f"- Options allowed: {c.options_allowed}",
            f"- Leverage allowed: {c.leverage_allowed}",
            f"- Max single position: {c.max_single_position_pct or 10}%",
            f"- Live trading: {c.live_trading_enabled or False}",
            f"- Rebalance frequency: {c.rebalance_frequency or 'quarterly'}",
        ]
        return "\n".join(lines)
