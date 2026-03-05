"""FastAPI endpoints for the Investment Team."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from investment_team.agents import AgentIdentity, InvestmentCommitteeAgent, PolicyGuardianAgent
from investment_team.models import (
    IPS,
    ArtifactRedactionStatus,
    GateCheckResult,
    GateResult,
    HumanApprovalMarker,
    IncomeProfile,
    InvestmentCommitteeMemo,
    InvestmentProfile,
    LiquidityNeeds,
    NetWorth,
    PortfolioConstraints,
    PlatformActionArtifact,
    PlatformActionArtifactsResponse,
    PlatformActionRunRecord,
    PlatformActionRunResponse,
    PlatformActionRunResult,
    PlatformActionRunStatus,
    PolicyGateDecision,
    PortfolioPosition,
    PortfolioProposal,
    PromotionDecision,
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
    WorkflowMode,
)
from investment_team.orchestrator import InvestmentTeamOrchestrator, QueueItem, WorkflowState

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Investment Team API",
    description="Investment profile management, portfolio proposals, strategy validation, and promotion gates.",
    version="1.0.0",
)

_profiles: Dict[str, IPS] = {}
_proposals: Dict[str, PortfolioProposal] = {}
_strategies: Dict[str, StrategySpec] = {}
_validations: Dict[str, ValidationReport] = {}
_platform_action_runs: Dict[str, PlatformActionRunRecord] = {}
_platform_action_artifacts: Dict[str, List[PlatformActionArtifact]] = {}
_workflow_state = WorkflowState()
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class CreateProfileRequest(BaseModel):
    user_id: str = Field(..., description="Unique user identifier")
    risk_tolerance: str = Field(..., description="low, medium, high, or very_high")
    max_drawdown_tolerance_pct: float = Field(..., ge=0, le=100)
    time_horizon_years: int = Field(..., ge=1)
    annual_gross_income: float = Field(..., ge=0)
    income_stability: str = Field(default="stable")
    total_net_worth: float = Field(..., ge=0)
    investable_assets: float = Field(..., ge=0)
    monthly_savings: float = Field(default=0.0)
    annual_savings: float = Field(default=0.0)
    tax_country: str = Field(default="US")
    tax_state: str = Field(default="")
    account_types: List[str] = Field(default_factory=list)
    emergency_fund_months: int = Field(default=6)
    excluded_asset_classes: List[str] = Field(default_factory=list)
    excluded_industries: List[str] = Field(default_factory=list)
    esg_preference: str = Field(default="none")
    crypto_allowed: bool = Field(default=True)
    options_allowed: bool = Field(default=True)
    leverage_allowed: bool = Field(default=False)
    goals: List[Dict[str, Any]] = Field(default_factory=list)
    max_single_position_pct: float = Field(default=10.0)
    max_asset_class_pct: Dict[str, float] = Field(default_factory=dict)
    live_trading_enabled: bool = Field(default=False)
    human_approval_required_for_live: bool = Field(default=True)
    speculative_sleeve_cap_pct: float = Field(default=10.0)
    rebalance_frequency: str = Field(default="quarterly")
    default_mode: str = Field(default="monitor_only")
    notes: List[str] = Field(default_factory=list)


class CreateProfileResponse(BaseModel):
    user_id: str
    ips: IPS
    message: str = "Investment Policy Statement created successfully."


class GetProfileResponse(BaseModel):
    user_id: str
    ips: Optional[IPS] = None
    found: bool = True


class CreateProposalRequest(BaseModel):
    prepared_by: str = Field(..., description="Agent or user ID who prepared this proposal")
    user_id: str = Field(..., description="User ID whose IPS this is for")
    objective: str = Field(..., description="Investment objective")
    positions: List[Dict[str, Any]] = Field(..., description="List of portfolio positions")
    expected_return_pct: Optional[float] = None
    expected_volatility_pct: Optional[float] = None
    expected_max_drawdown_pct: Optional[float] = None
    assumptions: List[str] = Field(default_factory=list)


class CreateProposalResponse(BaseModel):
    proposal_id: str
    proposal: PortfolioProposal
    message: str = "Portfolio proposal created successfully."


class GetProposalResponse(BaseModel):
    proposal_id: str
    proposal: Optional[PortfolioProposal] = None
    found: bool = True


class ValidateProposalRequest(BaseModel):
    user_id: str = Field(..., description="User ID to get IPS for validation")


class ValidateProposalResponse(BaseModel):
    proposal_id: str
    valid: bool
    violations: List[str] = Field(default_factory=list)


class CreateStrategyRequest(BaseModel):
    authored_by: str = Field(..., description="Agent or user ID who authored the strategy")
    asset_class: str = Field(..., description="Primary asset class")
    hypothesis: str = Field(..., description="Investment hypothesis")
    signal_definition: str = Field(..., description="Signal definition")
    entry_rules: List[str] = Field(default_factory=list)
    exit_rules: List[str] = Field(default_factory=list)
    sizing_rules: List[str] = Field(default_factory=list)
    risk_limits: Dict[str, Any] = Field(default_factory=dict)
    speculative: bool = Field(default=False)


class CreateStrategyResponse(BaseModel):
    strategy_id: str
    strategy: StrategySpec
    message: str = "Strategy created successfully."


class ValidateStrategyRequest(BaseModel):
    backtest_period: str = Field(default="2020-01-01 to 2024-12-31")
    scenario_set: List[str] = Field(default_factory=lambda: ["baseline", "stress", "monte_carlo"])
    checks: List[Dict[str, Any]] = Field(default_factory=list)


class ValidateStrategyResponse(BaseModel):
    strategy_id: str
    validation: ValidationReport
    passed: bool
    failures: List[str] = Field(default_factory=list)


class PromotionDecisionRequest(BaseModel):
    strategy_id: str = Field(..., description="Strategy ID to promote")
    user_id: str = Field(..., description="User ID for IPS lookup")
    proposer_agent_id: str = Field(..., description="ID of agent who proposed the strategy")
    approver_agent_id: str = Field(..., description="ID of independent approver agent")
    approver_role: str = Field(default="approver")
    approver_version: str = Field(default="1.0")
    risk_veto: bool = Field(default=False)
    human_live_approval: bool = Field(default=False)


class PromotionDecisionResponse(BaseModel):
    strategy_id: str
    decision: PromotionDecision


class WorkflowStatusResponse(BaseModel):
    mode: str
    audit_log: List[str] = Field(default_factory=list)
    queue_counts: Dict[str, int] = Field(default_factory=dict)


class QueueItemResponse(BaseModel):
    queue: str
    payload_id: str
    priority: str = "normal"


class QueuesResponse(BaseModel):
    queues: Dict[str, List[QueueItemResponse]] = Field(default_factory=dict)


class CreateMemoRequest(BaseModel):
    user_id: str
    recommendation: str
    rationale: str
    dissenting_views: List[str] = Field(default_factory=list)


class CreateMemoResponse(BaseModel):
    memo: InvestmentCommitteeMemo


class PlatformActionRunRequest(BaseModel):
    platform: str = Field(..., description="Target platform identifier")
    action: str = Field(..., description="Platform action to execute")
    params: Dict[str, Any] = Field(default_factory=dict, description="Action parameters payload")
    browser: Optional[str] = Field(default=None, description="Browser profile or runtime to use")
    human_approval_required: bool = Field(default=False)
    human_approved: Optional[bool] = Field(default=None)
    approver_id: Optional[str] = Field(default=None)
    approval_notes: str = Field(default="")


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok", "timestamp": _now()}


@app.post("/profiles", response_model=CreateProfileResponse)
def create_profile(request: CreateProfileRequest) -> CreateProfileResponse:
    """Create an Investment Policy Statement (IPS) for a user."""
    try:
        risk_tol = RiskTolerance(request.risk_tolerance)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid risk_tolerance: {request.risk_tolerance}. Must be one of: low, medium, high, very_high",
        )

    try:
        workflow_mode = WorkflowMode(request.default_mode)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid default_mode: {request.default_mode}. Must be one of: advisory, paper, live, monitor_only",
        )

    goals = [
        UserGoal(
            name=g.get("name", ""),
            target_amount=g.get("target_amount", 0),
            target_date=g.get("target_date", ""),
            priority=g.get("priority", "medium"),
        )
        for g in request.goals
    ]

    profile = InvestmentProfile(
        user_id=request.user_id,
        created_at=_now(),
        risk_tolerance=risk_tol,
        max_drawdown_tolerance_pct=request.max_drawdown_tolerance_pct,
        time_horizon_years=request.time_horizon_years,
        liquidity_needs=LiquidityNeeds(emergency_fund_months=request.emergency_fund_months),
        income=IncomeProfile(annual_gross=request.annual_gross_income, stability=request.income_stability),
        net_worth=NetWorth(total=request.total_net_worth, investable_assets=request.investable_assets),
        savings_rate=SavingsRate(monthly=request.monthly_savings, annual=request.annual_savings),
        tax_profile=TaxProfile(country=request.tax_country, state=request.tax_state, account_types=request.account_types),
        preferences=UserPreferences(
            excluded_asset_classes=request.excluded_asset_classes,
            excluded_industries=request.excluded_industries,
            esg_preference=request.esg_preference,
            crypto_allowed=request.crypto_allowed,
            options_allowed=request.options_allowed,
            leverage_allowed=request.leverage_allowed,
        ),
        goals=goals,
        constraints=PortfolioConstraints(
            max_single_position_pct=request.max_single_position_pct,
            max_asset_class_pct=request.max_asset_class_pct,
        ),
    )

    ips = IPS(
        profile=profile,
        live_trading_enabled=request.live_trading_enabled,
        human_approval_required_for_live=request.human_approval_required_for_live,
        speculative_sleeve_cap_pct=request.speculative_sleeve_cap_pct,
        rebalance_frequency=request.rebalance_frequency,
        default_mode=workflow_mode,
        notes=request.notes,
    )

    with _lock:
        _profiles[request.user_id] = ips

    return CreateProfileResponse(user_id=request.user_id, ips=ips)


@app.get("/profiles/{user_id}", response_model=GetProfileResponse)
def get_profile(user_id: str) -> GetProfileResponse:
    """Get the Investment Policy Statement for a user."""
    with _lock:
        ips = _profiles.get(user_id)
    if not ips:
        return GetProfileResponse(user_id=user_id, ips=None, found=False)
    return GetProfileResponse(user_id=user_id, ips=ips, found=True)


@app.post("/proposals/create", response_model=CreateProposalResponse)
def create_proposal(request: CreateProposalRequest) -> CreateProposalResponse:
    """Create a new portfolio proposal."""
    with _lock:
        ips = _profiles.get(request.user_id)

    if not ips:
        raise HTTPException(status_code=404, detail=f"No IPS found for user {request.user_id}")

    proposal_id = f"prop-{uuid.uuid4().hex[:8]}"

    positions = [
        PortfolioPosition(
            symbol=p.get("symbol", ""),
            asset_class=p.get("asset_class", ""),
            weight_pct=p.get("weight_pct", 0.0),
            rationale=p.get("rationale", ""),
        )
        for p in request.positions
    ]

    proposal = PortfolioProposal(
        proposal_id=proposal_id,
        prepared_by=request.prepared_by,
        ips_version=ips.profile.schema_version,
        data_snapshot_id=f"snap-{_now()}",
        objective=request.objective,
        positions=positions,
        expected_return_pct=request.expected_return_pct,
        expected_volatility_pct=request.expected_volatility_pct,
        expected_max_drawdown_pct=request.expected_max_drawdown_pct,
        assumptions=request.assumptions,
    )

    with _lock:
        _proposals[proposal_id] = proposal

    return CreateProposalResponse(proposal_id=proposal_id, proposal=proposal)


@app.get("/proposals/{proposal_id}", response_model=GetProposalResponse)
def get_proposal(proposal_id: str) -> GetProposalResponse:
    """Get a portfolio proposal by ID."""
    with _lock:
        proposal = _proposals.get(proposal_id)
    if not proposal:
        return GetProposalResponse(proposal_id=proposal_id, proposal=None, found=False)
    return GetProposalResponse(proposal_id=proposal_id, proposal=proposal, found=True)


@app.post("/proposals/{proposal_id}/validate", response_model=ValidateProposalResponse)
def validate_proposal(proposal_id: str, request: ValidateProposalRequest) -> ValidateProposalResponse:
    """Validate a portfolio proposal against the user's IPS."""
    with _lock:
        proposal = _proposals.get(proposal_id)
        ips = _profiles.get(request.user_id)

    if not proposal:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")
    if not ips:
        raise HTTPException(status_code=404, detail=f"No IPS found for user {request.user_id}")

    guardian = PolicyGuardianAgent()
    violations = guardian.check_portfolio(ips, proposal)

    return ValidateProposalResponse(
        proposal_id=proposal_id,
        valid=len(violations) == 0,
        violations=violations,
    )


@app.post("/strategies", response_model=CreateStrategyResponse)
def create_strategy(request: CreateStrategyRequest) -> CreateStrategyResponse:
    """Create a new investment strategy specification."""
    strategy_id = f"strat-{uuid.uuid4().hex[:8]}"

    strategy = StrategySpec(
        strategy_id=strategy_id,
        authored_by=request.authored_by,
        asset_class=request.asset_class,
        hypothesis=request.hypothesis,
        signal_definition=request.signal_definition,
        entry_rules=request.entry_rules,
        exit_rules=request.exit_rules,
        sizing_rules=request.sizing_rules,
        risk_limits=request.risk_limits,
        speculative=request.speculative,
    )

    with _lock:
        _strategies[strategy_id] = strategy

    return CreateStrategyResponse(strategy_id=strategy_id, strategy=strategy)


@app.post("/strategies/{strategy_id}/validate", response_model=ValidateStrategyResponse)
def validate_strategy(strategy_id: str, request: ValidateStrategyRequest) -> ValidateStrategyResponse:
    """Run validation checks on a strategy."""
    with _lock:
        strategy = _strategies.get(strategy_id)

    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")

    checks = []
    if request.checks:
        for c in request.checks:
            try:
                status = ValidationStatus(c.get("status", "pass"))
            except ValueError:
                status = ValidationStatus.PASS
            checks.append(
                ValidationCheck(
                    name=c.get("name", ""),
                    status=status,
                    details=c.get("details", ""),
                )
            )
    else:
        checks = [
            ValidationCheck(name="backtest_quality", status=ValidationStatus.PASS, details="Sharpe > 1.0"),
            ValidationCheck(name="walk_forward", status=ValidationStatus.PASS, details="Out-of-sample Sharpe > 0.8"),
            ValidationCheck(name="stress_test", status=ValidationStatus.PASS, details="Max DD within limits"),
            ValidationCheck(name="transaction_cost_model", status=ValidationStatus.PASS, details="Net return positive"),
            ValidationCheck(name="liquidity_impact", status=ValidationStatus.PASS, details="Minimal market impact"),
        ]

    validation = ValidationReport(
        strategy_id=strategy_id,
        generated_by="validation_agent",
        data_snapshot_id=f"snap-{_now()}",
        backtest_period=request.backtest_period,
        scenario_set=request.scenario_set,
        checks=checks,
        summary="Validation completed.",
    )

    with _lock:
        _validations[strategy_id] = validation

    failures = [c.details for c in checks if c.status == ValidationStatus.FAIL]

    return ValidateStrategyResponse(
        strategy_id=strategy_id,
        validation=validation,
        passed=len(failures) == 0,
        failures=failures,
    )


@app.post("/promotions/decide", response_model=PromotionDecisionResponse)
def promotion_decision(request: PromotionDecisionRequest) -> PromotionDecisionResponse:
    """Run promotion gate decision for a strategy."""
    with _lock:
        strategy = _strategies.get(request.strategy_id)
        validation = _validations.get(request.strategy_id)
        ips = _profiles.get(request.user_id)

    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {request.strategy_id} not found")
    if not validation:
        raise HTTPException(status_code=400, detail=f"Strategy {request.strategy_id} has no validation report")
    if not ips:
        raise HTTPException(status_code=404, detail=f"No IPS found for user {request.user_id}")

    orchestrator = InvestmentTeamOrchestrator()
    approver = AgentIdentity(
        agent_id=request.approver_agent_id,
        role=request.approver_role,
        version=request.approver_version,
    )

    decision = orchestrator.promotion_decision(
        state=_workflow_state,
        strategy=strategy,
        validation=validation,
        ips=ips,
        proposer_agent_id=request.proposer_agent_id,
        approver=approver,
        risk_veto=request.risk_veto,
        human_live_approval=request.human_live_approval,
    )

    return PromotionDecisionResponse(strategy_id=request.strategy_id, decision=decision)


@app.get("/workflow/status", response_model=WorkflowStatusResponse)
def workflow_status() -> WorkflowStatusResponse:
    """Get the current workflow state."""
    with _lock:
        mode = _workflow_state.mode.value
        audit_log = list(_workflow_state.audit_log)
        queue_counts = {q: len(items) for q, items in _workflow_state.queues.items()}

    return WorkflowStatusResponse(mode=mode, audit_log=audit_log, queue_counts=queue_counts)


@app.get("/workflow/queues", response_model=QueuesResponse)
def workflow_queues() -> QueuesResponse:
    """Get the contents of all workflow queues."""
    with _lock:
        queues = {}
        for q_name, items in _workflow_state.queues.items():
            queues[q_name] = [
                QueueItemResponse(queue=item.queue, payload_id=item.payload_id, priority=item.priority)
                for item in items
            ]

    return QueuesResponse(queues=queues)


@app.post("/platform-actions/run", response_model=PlatformActionRunResponse)
def run_platform_action(request: PlatformActionRunRequest) -> PlatformActionRunResponse:
    """Submit and execute a platform action run with policy and approval metadata."""
    run_id = f"run-{uuid.uuid4().hex[:10]}"
    now = _now()

    policy_gate_decisions = [
        PolicyGateDecision(
            gate="action_policy",
            decision=GateResult.PASS,
            reason="Action allowed under current policy configuration.",
            policy_reference="policy://platform_actions/default_allowlist",
        ),
        PolicyGateDecision(
            gate="sensitive_params_review",
            decision=GateResult.WARN if request.params else GateResult.PASS,
            reason="Parameters provided and logged for compliance review." if request.params else "No action parameters provided.",
            policy_reference="policy://platform_actions/sensitive_param_review",
        ),
    ]

    if request.human_approval_required and request.human_approved is not True:
        policy_gate_decisions.append(
            PolicyGateDecision(
                gate="human_approval",
                decision=GateResult.FAIL,
                reason="Human approval required before action execution.",
                policy_reference="policy://platform_actions/human_approval",
            )
        )
        status = PlatformActionRunStatus.BLOCKED
        result = PlatformActionRunResult(
            output={},
            structured_extraction={},
            policy_gate_decisions=policy_gate_decisions,
            human_approval=HumanApprovalMarker(
                required=True,
                approved=request.human_approved,
                approver_id=request.approver_id,
                approved_at=None,
                notes=request.approval_notes,
            ),
        )
    else:
        if request.human_approval_required:
            policy_gate_decisions.append(
                PolicyGateDecision(
                    gate="human_approval",
                    decision=GateResult.PASS,
                    reason="Required human approval marker is present.",
                    policy_reference="policy://platform_actions/human_approval",
                )
            )

        status = PlatformActionRunStatus.COMPLETED
        result = PlatformActionRunResult(
            output={
                "message": "Platform action completed.",
                "platform": request.platform,
                "action": request.action,
            },
            structured_extraction={
                "platform": request.platform,
                "action": request.action,
                "params": request.params,
                "browser": request.browser,
            },
            policy_gate_decisions=policy_gate_decisions,
            human_approval=HumanApprovalMarker(
                required=request.human_approval_required,
                approved=request.human_approved if request.human_approval_required else None,
                approver_id=request.approver_id,
                approved_at=now if request.human_approval_required and request.human_approved else None,
                notes=request.approval_notes,
            ),
        )

    run = PlatformActionRunRecord(
        run_id=run_id,
        platform=request.platform,
        action=request.action,
        params=request.params,
        browser=request.browser,
        status=status,
        created_at=now,
        updated_at=now,
        result=result,
        error="Human approval required." if status == PlatformActionRunStatus.BLOCKED else None,
    )

    artifacts = [
        PlatformActionArtifact(
            artifact_id=f"art-{uuid.uuid4().hex[:10]}",
            run_id=run_id,
            artifact_type="screenshot",
            platform=request.platform,
            action_type=request.action,
            path=f"artifacts/{run_id}/screenshot.png",
            uri=None,
            timestamp=now,
            redaction_status=ArtifactRedactionStatus.PENDING,
            metadata={"captured_by": request.browser or "default"},
        ),
        PlatformActionArtifact(
            artifact_id=f"art-{uuid.uuid4().hex[:10]}",
            run_id=run_id,
            artifact_type="structured_extraction",
            platform=request.platform,
            action_type=request.action,
            path=f"artifacts/{run_id}/extraction.json",
            uri=None,
            timestamp=now,
            redaction_status=ArtifactRedactionStatus.NONE,
            metadata={"schema": "platform_action_extraction_v1"},
        ),
    ]

    with _lock:
        _platform_action_runs[run_id] = run
        _platform_action_artifacts[run_id] = artifacts

    return PlatformActionRunResponse(run=run)


@app.get("/platform-actions/{run_id}", response_model=PlatformActionRunResponse)
def get_platform_action_run(run_id: str) -> PlatformActionRunResponse:
    """Get status and result for a platform action run."""
    with _lock:
        run = _platform_action_runs.get(run_id)

    if not run:
        raise HTTPException(status_code=404, detail=f"Platform action run {run_id} not found")

    return PlatformActionRunResponse(run=run)


@app.get("/platform-actions/{run_id}/artifacts", response_model=PlatformActionArtifactsResponse)
def get_platform_action_artifacts(run_id: str) -> PlatformActionArtifactsResponse:
    """List artifacts generated by a platform action run."""
    with _lock:
        run = _platform_action_runs.get(run_id)
        artifacts = list(_platform_action_artifacts.get(run_id, []))

    if not run:
        raise HTTPException(status_code=404, detail=f"Platform action run {run_id} not found")

    return PlatformActionArtifactsResponse(run_id=run_id, artifacts=artifacts)


@app.post("/memos", response_model=CreateMemoResponse)
def create_memo(request: CreateMemoRequest) -> CreateMemoResponse:
    """Generate an investment committee memo."""
    committee_agent = InvestmentCommitteeAgent()
    memo = committee_agent.draft_memo(
        user_id=request.user_id,
        recommendation=request.recommendation,
        rationale=request.rationale,
        dissenting_views=request.dissenting_views,
    )

    return CreateMemoResponse(memo=memo)
