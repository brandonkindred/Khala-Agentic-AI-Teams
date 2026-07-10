"""Temporal workflows + activities for the investment team's interactive surfaces.

These are the short, request/response operations the top-level
``InvestmentTeamOrchestrator`` and the conversational advisor drive — proposal
creation & validation, strategy creation & validation, promotion decisions,
committee memos, and the advisor session lifecycle. Unlike the heavy backtest /
Strategy Lab pipelines (their own coarse ``investment-queue`` and fine-grained
``strategy-lab-queue``), each of these is one quick unit of work, so each is a
single-activity workflow dispatched **execute-and-wait** from its FastAPI route.
They run on their own ``investment-advisory-queue`` so a multi-hour backtest
activity can never head-of-line-block an interactive call.

Each activity reuses the existing agent / orchestrator logic verbatim (the
module-level singletons in ``investment_team.api.main``) and re-reads the
persistent stores by identifier — the same pattern as
``investment_team.temporal.workflows.run_backtest_activity`` — so no business
logic is duplicated here. Sandbox-safety: this module is re-imported by the
temporalio workflow sandbox to register the workflow classes, so every heavy
import lives inside an activity body and nothing here touches restricted
builtins at module top level.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

logger = logging.getLogger(__name__)

ADVISORY_TASK_QUEUE = "investment-advisory-queue"
ADVISORY_WORKFLOW_ID_PREFIX = "investment-adv-"

# Interactive activities are non-idempotent (e.g. the advisor appends chat
# messages, promotion enqueues escalation) and dispatched execute-and-wait, so a
# blind Temporal retry could double-apply a mutation on a worker crash. Bound to
# a single attempt — a crash surfaces to the caller, which can safely re-issue.
_ADVISORY_RETRY = RetryPolicy(maximum_attempts=1)
_ADVISORY_TIMEOUT = timedelta(minutes=2)


def _as_model(model_cls: Any, raw: Any) -> Any:
    """Coerce a persisted value into ``model_cls``.

    The persistent stores return the model instance itself in unit tests
    (``_InMemoryDict``) but a JSON dict in production (``_PersistentDict``); this
    accepts either so the reused agent methods always receive a real model.

    Preconditions:
        ``raw`` is ``None``, an instance of ``model_cls``, or its JSON dump.
    Postconditions:
        Returns ``None`` for ``None`` input, else a ``model_cls`` instance
        (via ``parse_persisted`` when available, else ``model_validate``).
    """
    if raw is None or isinstance(raw, model_cls):
        return raw
    parser = getattr(model_cls, "parse_persisted", None)
    if parser is not None:
        return parser(raw)
    return model_cls.model_validate(raw)


# ---------------------------------------------------------------------------
# Activities — proposals & strategies
# ---------------------------------------------------------------------------


@activity.defn(name="investment_create_proposal")
def create_proposal_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Build and persist a ``PortfolioProposal`` (reuses the route's logic).

    Preconditions:
        ``payload`` carries ``proposal_id`` (already minted) and ``request``
        (a ``CreateProposalRequest`` JSON dump whose ``user_id`` has an IPS).
    Postconditions:
        Stores the proposal under ``proposal_id`` and returns
        ``{"proposal": <PortfolioProposal JSON>}``. Raises ``ApplicationError``
        when the user's IPS is missing.
    """
    from investment_team.api.main import _lock, _now, _profiles, _proposals
    from investment_team.models import IPS, PortfolioPosition, PortfolioProposal

    req = dict(payload["request"])
    proposal_id = payload["proposal_id"]

    with _lock:
        raw_ips = _profiles.get(req["user_id"])
    if raw_ips is None:
        raise ApplicationError(
            f"No IPS found for user {req['user_id']}", type="NotFound", non_retryable=True
        )
    ips = _as_model(IPS, raw_ips)

    positions = [
        PortfolioPosition(
            symbol=p.get("symbol", ""),
            asset_class=p.get("asset_class", ""),
            weight_pct=p.get("weight_pct", 0.0),
            rationale=p.get("rationale", ""),
        )
        for p in req.get("positions", [])
    ]
    proposal = PortfolioProposal(
        proposal_id=proposal_id,
        prepared_by=req["prepared_by"],
        ips_version=ips.profile.schema_version,
        data_snapshot_id=f"snap-{_now()}",
        objective=req["objective"],
        positions=positions,
        expected_return_pct=req.get("expected_return_pct"),
        expected_volatility_pct=req.get("expected_volatility_pct"),
        expected_max_drawdown_pct=req.get("expected_max_drawdown_pct"),
        assumptions=req.get("assumptions", []),
    )
    with _lock:
        _proposals[proposal_id] = proposal
    return {"proposal": proposal.model_dump(mode="json")}


@activity.defn(name="investment_validate_proposal")
def validate_proposal_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a proposal against the user's IPS via ``PolicyGuardianAgent``.

    Preconditions:
        ``payload`` carries ``proposal_id`` and ``user_id`` that both resolve.
    Postconditions:
        Returns ``{"valid": bool, "violations": list[str]}``. Raises
        ``ApplicationError`` when the proposal or IPS is missing.
    """
    from investment_team.api.main import _lock, _policy_guardian, _profiles, _proposals
    from investment_team.models import IPS, PortfolioProposal

    proposal_id = payload["proposal_id"]
    user_id = payload["user_id"]
    with _lock:
        raw_proposal = _proposals.get(proposal_id)
        raw_ips = _profiles.get(user_id)
    if raw_proposal is None:
        raise ApplicationError(
            f"Proposal {proposal_id} not found", type="NotFound", non_retryable=True
        )
    if raw_ips is None:
        raise ApplicationError(
            f"No IPS found for user {user_id}", type="NotFound", non_retryable=True
        )

    proposal = _as_model(PortfolioProposal, raw_proposal)
    ips = _as_model(IPS, raw_ips)
    violations = _policy_guardian.check_portfolio(ips, proposal)
    return {"valid": len(violations) == 0, "violations": violations}


@activity.defn(name="investment_create_strategy")
def create_strategy_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a already-validated ``StrategySpec``.

    Preconditions:
        ``payload["strategy"]`` is a ``StrategySpec`` JSON dump (the route
        constructed and validated it, translating client errors to HTTP 422)
        and ``payload["strategy_id"]`` is its id.
    Postconditions:
        Stores the strategy and returns ``{"strategy": <StrategySpec JSON>}``.
    """
    from investment_team.api.main import _lock, _strategies
    from investment_team.models import StrategySpec

    strategy_id = payload["strategy_id"]
    strategy = StrategySpec.parse_persisted(payload["strategy"])
    with _lock:
        _strategies[strategy_id] = strategy
    return {"strategy": strategy.model_dump(mode="json")}


@activity.defn(name="investment_validate_strategy")
def validate_strategy_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Build and persist a ``ValidationReport`` (reuses the route's logic).

    Preconditions:
        ``payload`` carries ``strategy_id`` (an existing strategy) and
        ``request`` (a ``ValidateStrategyRequest`` JSON dump).
    Postconditions:
        Stores the report and returns
        ``{"validation": <ValidationReport JSON>, "passed": bool,
        "failures": list[str]}``. Raises ``ApplicationError`` when the strategy
        is missing.
    """
    from investment_team.api.main import _lock, _now, _strategies, _validations
    from investment_team.models import ValidationCheck, ValidationReport, ValidationStatus

    strategy_id = payload["strategy_id"]
    req = dict(payload["request"])
    with _lock:
        raw = _strategies.get(strategy_id)
    if raw is None:
        raise ApplicationError(
            f"Strategy {strategy_id} not found", type="NotFound", non_retryable=True
        )

    checks: list[Any] = []
    if req.get("checks"):
        for c in req["checks"]:
            try:
                status = ValidationStatus(c.get("status", "pass"))
            except ValueError:
                status = ValidationStatus.PASS
            checks.append(
                ValidationCheck(name=c.get("name", ""), status=status, details=c.get("details", ""))
            )
    else:
        checks = [
            ValidationCheck(
                name="backtest_quality", status=ValidationStatus.PASS, details="Sharpe > 1.0"
            ),
            ValidationCheck(
                name="walk_forward",
                status=ValidationStatus.PASS,
                details="Out-of-sample Sharpe > 0.8",
            ),
            ValidationCheck(
                name="stress_test", status=ValidationStatus.PASS, details="Max DD within limits"
            ),
            ValidationCheck(
                name="transaction_cost_model",
                status=ValidationStatus.PASS,
                details="Net return positive",
            ),
            ValidationCheck(
                name="liquidity_impact",
                status=ValidationStatus.PASS,
                details="Minimal market impact",
            ),
        ]

    validation = ValidationReport(
        strategy_id=strategy_id,
        generated_by="validation_agent",
        data_snapshot_id=f"snap-{_now()}",
        backtest_period=req.get("backtest_period", "2020-01-01 to 2024-12-31"),
        scenario_set=req.get("scenario_set", ["baseline", "stress", "monte_carlo"]),
        checks=checks,
        summary="Validation completed.",
    )
    with _lock:
        _validations[strategy_id] = validation
    failures = [c.details for c in checks if c.status == ValidationStatus.FAIL]
    return {
        "validation": validation.model_dump(mode="json"),
        "passed": len(failures) == 0,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Activities — promotion & committee
# ---------------------------------------------------------------------------


@activity.defn(name="investment_promotion_decision")
def promotion_decision_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the promotion-gate decision via ``InvestmentTeamOrchestrator``.

    Preconditions:
        ``payload`` carries ``strategy_id``/``user_id`` (both resolve, with a
        validation report for the strategy) plus the proposer/approver ids and
        the ``risk_veto`` / ``human_live_approval`` flags.
    Postconditions:
        Appends to the shared ``_workflow_state`` audit log (and escalation
        queue on reject/revise) and returns ``{"decision": <PromotionDecision
        JSON>}``. Raises ``ApplicationError`` on a missing strategy / validation
        / IPS.
    """
    from investment_team.agents import AgentIdentity
    from investment_team.api.main import (
        _lock,
        _orchestrator,
        _profiles,
        _strategies,
        _validations,
        _workflow_state,
    )
    from investment_team.models import IPS, StrategySpec, ValidationReport

    strategy_id = payload["strategy_id"]
    with _lock:
        raw_strategy = _strategies.get(strategy_id)
        raw_validation = _validations.get(strategy_id)
        raw_ips = _profiles.get(payload["user_id"])
    if raw_strategy is None:
        raise ApplicationError(
            f"Strategy {strategy_id} not found", type="NotFound", non_retryable=True
        )
    if raw_validation is None:
        raise ApplicationError(
            f"Strategy {strategy_id} has no validation report",
            type="NoValidation",
            non_retryable=True,
        )
    if raw_ips is None:
        raise ApplicationError(
            f"No IPS found for user {payload['user_id']}", type="NotFound", non_retryable=True
        )

    strategy = _as_model(StrategySpec, raw_strategy)
    validation = _as_model(ValidationReport, raw_validation)
    ips = _as_model(IPS, raw_ips)
    approver = AgentIdentity(
        agent_id=payload["approver_agent_id"],
        role=payload.get("approver_role", "approver"),
        version=payload.get("approver_version", "1.0"),
    )
    decision = _orchestrator.promotion_decision(
        state=_workflow_state,
        strategy=strategy,
        validation=validation,
        ips=ips,
        proposer_agent_id=payload["proposer_agent_id"],
        approver=approver,
        risk_veto=bool(payload.get("risk_veto", False)),
        human_live_approval=bool(payload.get("human_live_approval", False)),
    )
    return {"decision": decision.model_dump(mode="json")}


@activity.defn(name="investment_committee_memo")
def committee_memo_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Draft an investment-committee memo via ``InvestmentCommitteeAgent``.

    Preconditions:
        ``payload`` carries ``user_id``, ``recommendation``, ``rationale`` and
        optional ``dissenting_views``.
    Postconditions:
        Returns ``{"memo": <InvestmentCommitteeMemo JSON>}``.
    """
    from investment_team.api.main import _committee_agent

    memo = _committee_agent.draft_memo(
        user_id=payload["user_id"],
        recommendation=payload["recommendation"],
        rationale=payload["rationale"],
        dissenting_views=payload.get("dissenting_views", []),
    )
    return {"memo": memo.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# Activities — financial advisor session lifecycle
# ---------------------------------------------------------------------------


@activity.defn(name="investment_advisor_start")
def advisor_start_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Start an advisor session via ``FinancialAdvisorAgent.start_session``.

    Preconditions:
        ``payload`` carries ``session_id`` (already minted) and ``user_id``.
    Postconditions:
        Stores the session and returns
        ``{"advisor_message": str, "session": <AdvisorSession JSON>}``.
    """
    from investment_team.api.main import _advisor_agent, _advisor_sessions, _lock

    session_id = payload["session_id"]
    session = _advisor_agent.start_session(session_id=session_id, user_id=payload["user_id"])
    with _lock:
        _advisor_sessions[session_id] = session
    return {
        "advisor_message": session.messages[0].content,
        "session": session.model_dump(mode="json"),
    }


@activity.defn(name="investment_advisor_message")
def advisor_message_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Advance an advisor session via ``FinancialAdvisorAgent.handle_message``.

    Preconditions:
        ``payload`` carries an existing ``session_id`` and the user ``message``.
    Postconditions:
        Persists the mutated session and returns the advisor reply, the new
        session status / current topic, and remaining ``missing_fields``. Raises
        ``ApplicationError`` when the session is missing.
    """
    from investment_team.api.main import _advisor_agent, _advisor_sessions, _lock
    from investment_team.models import AdvisorSession

    session_id = payload["session_id"]
    with _lock:
        raw = _advisor_sessions.get(session_id)
    if raw is None:
        raise ApplicationError(
            f"Advisor session {session_id} not found", type="NotFound", non_retryable=True
        )

    session = _as_model(AdvisorSession, raw)
    reply = _advisor_agent.handle_message(session, payload["message"])
    missing = _advisor_agent.missing_fields(session.collected)
    with _lock:
        _advisor_sessions[session_id] = session
    return {
        "advisor_message": reply,
        "session_status": session.status.value,
        "current_topic": session.current_topic.value,
        "missing_fields": missing,
    }


@activity.defn(name="investment_advisor_complete")
def advisor_complete_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Finalize an advisor session into an IPS via ``FinancialAdvisorAgent``.

    Preconditions:
        ``payload`` carries an existing ``session_id`` whose collected data has
        no missing required fields.
    Postconditions:
        Stores the built IPS under the session's user, marks the session
        completed, and returns ``{"user_id": str, "ips": <IPS JSON>}``. Raises
        ``ApplicationError`` when the session is missing or still incomplete.
    """
    from investment_team.api.main import _advisor_agent, _advisor_sessions, _lock, _profiles
    from investment_team.models import AdvisorSession, AdvisorSessionStatus

    session_id = payload["session_id"]
    with _lock:
        raw = _advisor_sessions.get(session_id)
    if raw is None:
        raise ApplicationError(
            f"Advisor session {session_id} not found", type="NotFound", non_retryable=True
        )

    session = _as_model(AdvisorSession, raw)
    missing = _advisor_agent.missing_fields(session.collected)
    if missing:
        raise ApplicationError(
            f"Cannot finalize — missing required fields: {', '.join(missing)}",
            type="MissingFields",
            non_retryable=True,
        )
    ips = _advisor_agent.build_ips(session)
    with _lock:
        _profiles[session.user_id] = ips
        session.status = AdvisorSessionStatus.COMPLETED
        _advisor_sessions[session_id] = session
    return {"user_id": session.user_id, "ips": ips.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# Workflows — one thin execute-and-wait driver per activity
# ---------------------------------------------------------------------------


async def _run_single_activity(fn: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one advisory activity and return its result.

    Preconditions:
        ``fn`` is an ``@activity.defn`` function in this module; ``payload`` is
        the single dict it consumes.
    Postconditions:
        Returns the activity's result dict (no Temporal-level retry — see
        ``_ADVISORY_RETRY``).
    """
    return await workflow.execute_activity(
        fn,
        args=[payload],
        start_to_close_timeout=_ADVISORY_TIMEOUT,
        retry_policy=_ADVISORY_RETRY,
    )


@workflow.defn(name="InvestmentCreateProposalWorkflow")
class CreateProposalWorkflow:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Durably create a portfolio proposal."""
        return await _run_single_activity(create_proposal_activity, payload)


@workflow.defn(name="InvestmentValidateProposalWorkflow")
class ValidateProposalWorkflow:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Durably validate a proposal against an IPS."""
        return await _run_single_activity(validate_proposal_activity, payload)


@workflow.defn(name="InvestmentCreateStrategyWorkflow")
class CreateStrategyWorkflow:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Durably persist a strategy spec."""
        return await _run_single_activity(create_strategy_activity, payload)


@workflow.defn(name="InvestmentValidateStrategyWorkflow")
class ValidateStrategyWorkflow:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Durably run strategy validation checks."""
        return await _run_single_activity(validate_strategy_activity, payload)


@workflow.defn(name="InvestmentPromotionDecisionWorkflow")
class PromotionDecisionWorkflow:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Durably run the promotion-gate decision."""
        return await _run_single_activity(promotion_decision_activity, payload)


@workflow.defn(name="InvestmentCommitteeMemoWorkflow")
class CommitteeMemoWorkflow:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Durably draft a committee memo."""
        return await _run_single_activity(committee_memo_activity, payload)


@workflow.defn(name="InvestmentAdvisorStartWorkflow")
class AdvisorStartWorkflow:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Durably start an advisor session."""
        return await _run_single_activity(advisor_start_activity, payload)


@workflow.defn(name="InvestmentAdvisorMessageWorkflow")
class AdvisorMessageWorkflow:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Durably advance an advisor session with a user message."""
        return await _run_single_activity(advisor_message_activity, payload)


@workflow.defn(name="InvestmentAdvisorCompleteWorkflow")
class AdvisorCompleteWorkflow:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Durably finalize an advisor session into an IPS."""
        return await _run_single_activity(advisor_complete_activity, payload)


ADVISORY_WORKFLOWS = [
    CreateProposalWorkflow,
    ValidateProposalWorkflow,
    CreateStrategyWorkflow,
    ValidateStrategyWorkflow,
    PromotionDecisionWorkflow,
    CommitteeMemoWorkflow,
    AdvisorStartWorkflow,
    AdvisorMessageWorkflow,
    AdvisorCompleteWorkflow,
]

ADVISORY_ACTIVITIES = [
    create_proposal_activity,
    validate_proposal_activity,
    create_strategy_activity,
    validate_strategy_activity,
    promotion_decision_activity,
    committee_memo_activity,
    advisor_start_activity,
    advisor_message_activity,
    advisor_complete_activity,
]

__all__ = [
    "ADVISORY_ACTIVITIES",
    "ADVISORY_TASK_QUEUE",
    "ADVISORY_WORKFLOWS",
    "ADVISORY_WORKFLOW_ID_PREFIX",
    "AdvisorCompleteWorkflow",
    "AdvisorMessageWorkflow",
    "AdvisorStartWorkflow",
    "CommitteeMemoWorkflow",
    "CreateProposalWorkflow",
    "CreateStrategyWorkflow",
    "PromotionDecisionWorkflow",
    "ValidateProposalWorkflow",
    "ValidateStrategyWorkflow",
    "advisor_complete_activity",
    "advisor_message_activity",
    "advisor_start_activity",
    "committee_memo_activity",
    "create_proposal_activity",
    "create_strategy_activity",
    "promotion_decision_activity",
    "validate_proposal_activity",
    "validate_strategy_activity",
]
