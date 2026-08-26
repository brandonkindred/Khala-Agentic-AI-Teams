"""Unit tests for the interactive advisory activities.

The happy paths of most activities are exercised end-to-end by the route tests
in ``test_api_routes.py`` (the autouse ``_temporal_dispatch_inline`` fixture runs
each activity in-process). These tests pin the pieces the routes pre-check and
therefore never reach through the app: the ``ApplicationError`` branches for
missing entities, plus the two activities with no store dependency.
"""

from __future__ import annotations

import pytest
from temporalio.exceptions import ApplicationError


def test_committee_memo_activity_drafts_memo() -> None:
    from investment_team.temporal.advisory import committee_memo_activity

    result = committee_memo_activity(
        {
            "user_id": "u1",
            "recommendation": "Increase equity allocation",
            "rationale": "Long horizon, high risk tolerance",
            "dissenting_views": ["watch drawdown"],
        }
    )
    memo = result["memo"]
    assert memo["prepared_for_user_id"] == "u1"
    assert memo["recommendation"] == "Increase equity allocation"
    assert memo["dissenting_views"] == ["watch drawdown"]


def test_advisor_start_activity_persists_session(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.temporal.advisory import advisor_start_activity

    store: dict = {}
    monkeypatch.setattr(api_main, "_advisor_sessions", store)

    result = advisor_start_activity({"session_id": "adv-1", "user_id": "u1"})

    assert result["advisor_message"]
    assert result["session"]["session_id"] == "adv-1"
    assert "adv-1" in store


def test_advisor_message_activity_advances_session(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.temporal.advisory import advisor_message_activity, advisor_start_activity

    store: dict = {}
    monkeypatch.setattr(api_main, "_advisor_sessions", store)
    advisor_start_activity({"session_id": "adv-1", "user_id": "u1"})

    result = advisor_message_activity({"session_id": "adv-1", "message": "hello"})

    assert "advisor_message" in result
    assert "session_status" in result
    assert "current_topic" in result
    assert isinstance(result["missing_fields"], list)


def test_advisor_message_activity_missing_session_raises(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.temporal.advisory import advisor_message_activity

    monkeypatch.setattr(api_main, "_advisor_sessions", {})
    with pytest.raises(ApplicationError, match="not found"):
        advisor_message_activity({"session_id": "nope", "message": "hi"})


def test_advisor_complete_activity_missing_session_raises(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.temporal.advisory import advisor_complete_activity

    monkeypatch.setattr(api_main, "_advisor_sessions", {})
    with pytest.raises(ApplicationError, match="not found"):
        advisor_complete_activity({"session_id": "nope"})


def test_advisor_complete_activity_incomplete_session_raises(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.temporal.advisory import advisor_complete_activity, advisor_start_activity

    store: dict = {}
    monkeypatch.setattr(api_main, "_advisor_sessions", store)
    # A freshly started session has no collected profile data yet.
    advisor_start_activity({"session_id": "adv-1", "user_id": "u1"})

    with pytest.raises(ApplicationError, match="missing required fields"):
        advisor_complete_activity({"session_id": "adv-1"})


def test_advisor_complete_activity_maps_build_ips_value_error(monkeypatch) -> None:
    """build_ips can raise ValueError for reasons other than missing fields
    (e.g. an invalid enum coercion on corrupted persisted data); it must map to
    a typed, non-retryable ApplicationError instead of propagating raw."""
    from temporalio.exceptions import ApplicationError

    from investment_team.agents import FinancialAdvisorAgent
    from investment_team.api import main as api_main
    from investment_team.temporal.advisory import advisor_complete_activity

    session = FinancialAdvisorAgent().start_session("adv-1", "u1")
    monkeypatch.setattr(api_main, "_advisor_sessions", {"adv-1": session})
    monkeypatch.setattr(FinancialAdvisorAgent, "missing_fields", staticmethod(lambda collected: []))

    def _boom(self, session):
        raise ValueError("invalid risk_tolerance value")

    monkeypatch.setattr(FinancialAdvisorAgent, "build_ips", _boom)

    with pytest.raises(ApplicationError, match="invalid risk_tolerance") as ei:
        advisor_complete_activity({"session_id": "adv-1"})
    assert ei.value.type == "ValueError"
    assert ei.value.non_retryable is True


def test_advisor_complete_activity_builds_ips(monkeypatch) -> None:
    from investment_team.agents import FinancialAdvisorAgent
    from investment_team.api import main as api_main
    from investment_team.models import AdvisorSessionStatus
    from investment_team.temporal.advisory import advisor_complete_activity

    # A fully-collected session so build_ips succeeds (mirrors test_advisor_agent).
    session = FinancialAdvisorAgent().start_session("adv-1", "u1")
    c = session.collected
    c.risk_tolerance = "medium"
    c.max_drawdown_tolerance_pct = 20.0
    c.time_horizon_years = 10
    c.annual_gross_income = 120000
    c.total_net_worth = 200000
    c.investable_assets = 150000

    sessions = {"adv-1": session}
    profiles: dict = {}
    monkeypatch.setattr(api_main, "_advisor_sessions", sessions)
    monkeypatch.setattr(api_main, "_profiles", profiles)

    result = advisor_complete_activity({"session_id": "adv-1"})

    assert result["user_id"] == "u1"
    assert result["ips"]["profile"]["user_id"] == "u1"
    assert "u1" in profiles
    assert session.status == AdvisorSessionStatus.COMPLETED


def test_as_model_passthrough_and_coercion() -> None:
    from investment_team.temporal.advisory import _as_model

    class _HasParse:
        @classmethod
        def parse_persisted(cls, raw):
            return ("parsed", raw)

    class _HasValidate:
        @classmethod
        def model_validate(cls, raw):
            return ("validated", raw)

    # None short-circuits.
    assert _as_model(_HasParse, None) is None
    # An existing instance passes through untouched.
    inst = _HasValidate()
    assert _as_model(_HasValidate, inst) is inst
    # A dict is coerced via parse_persisted when available, else model_validate.
    assert _as_model(_HasParse, {"a": 1}) == ("parsed", {"a": 1})
    assert _as_model(_HasValidate, {"b": 2}) == ("validated", {"b": 2})


def test_as_model_propagates_parse_persisted_failure() -> None:
    """A malformed persisted record's parse failure propagates unchanged —
    _as_model itself makes no error-handling promise; callers that need a
    typed ApplicationError wrap their own call (see the paper-trading preamble
    test for that pattern)."""
    from investment_team.temporal.advisory import _as_model

    class _Corrupt:
        @classmethod
        def parse_persisted(cls, raw):
            raise ValueError("corrupted record")

    with pytest.raises(ValueError, match="corrupted record"):
        _as_model(_Corrupt, {"a": 1})


def test_create_proposal_activity_missing_ips_raises(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.temporal.advisory import create_proposal_activity

    monkeypatch.setattr(api_main, "_profiles", {})
    with pytest.raises(ApplicationError, match="No IPS"):
        create_proposal_activity(
            {
                "proposal_id": "prop-1",
                "request": {"user_id": "u1", "prepared_by": "a", "objective": "x"},
            }
        )


def test_validate_proposal_activity_missing_entities_raise(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.temporal.advisory import validate_proposal_activity

    monkeypatch.setattr(api_main, "_proposals", {})
    monkeypatch.setattr(api_main, "_profiles", {})
    with pytest.raises(ApplicationError, match="Proposal .* not found"):
        validate_proposal_activity({"proposal_id": "prop-1", "user_id": "u1"})

    # Proposal present but IPS missing.
    monkeypatch.setattr(api_main, "_proposals", {"prop-1": {"proposal_id": "prop-1"}})
    with pytest.raises(ApplicationError, match="No IPS"):
        validate_proposal_activity({"proposal_id": "prop-1", "user_id": "u1"})


def test_validate_strategy_activity_missing_strategy_raises(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.temporal.advisory import validate_strategy_activity

    monkeypatch.setattr(api_main, "_strategies", {})
    with pytest.raises(ApplicationError, match="Strategy .* not found"):
        validate_strategy_activity({"strategy_id": "s1", "request": {}})


def test_validate_strategy_activity_default_checks_match_required_checks(monkeypatch) -> None:
    """The no-checks-supplied fallback in validate_strategy_activity must derive
    its check names from ValidationAgent.REQUIRED_CHECKS, the canonical set the
    promotion gate enforces (agents.py), rather than a hand-typed literal list
    that could silently drift from it."""
    from investment_team.agents import ValidationAgent
    from investment_team.api import main as api_main
    from investment_team.models import StrategySpec
    from investment_team.temporal.advisory import validate_strategy_activity

    strategy = StrategySpec(
        strategy_id="s1",
        authored_by="alice",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    monkeypatch.setattr(api_main, "_strategies", {"s1": strategy})
    monkeypatch.setattr(api_main, "_validations", {})

    result = validate_strategy_activity({"strategy_id": "s1", "request": {}})

    check_names = {c["name"] for c in result["validation"]["checks"]}
    assert check_names == ValidationAgent.REQUIRED_CHECKS
    assert result["passed"] is True
    assert result["failures"] == []


def test_promotion_decision_activity_missing_entities_raise(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.temporal.advisory import promotion_decision_activity

    base = {
        "strategy_id": "s1",
        "user_id": "u1",
        "proposer_agent_id": "p",
        "approver_agent_id": "a",
    }
    monkeypatch.setattr(api_main, "_strategies", {})
    monkeypatch.setattr(api_main, "_validations", {})
    monkeypatch.setattr(api_main, "_profiles", {})
    with pytest.raises(ApplicationError, match="Strategy .* not found"):
        promotion_decision_activity(dict(base))

    monkeypatch.setattr(api_main, "_strategies", {"s1": {"strategy_id": "s1"}})
    with pytest.raises(ApplicationError, match="no validation report"):
        promotion_decision_activity(dict(base))

    monkeypatch.setattr(api_main, "_validations", {"s1": {"strategy_id": "s1"}})
    with pytest.raises(ApplicationError, match="No IPS"):
        promotion_decision_activity(dict(base))


def test_promotion_decision_activity_returns_delta_without_mutating_shared_state(
    monkeypatch,
) -> None:
    """The activity must never touch the API process's ``_workflow_state``
    singleton (it may run in a different Temporal worker process) — it returns
    the audit-log/escalation delta instead, for the route to apply exactly
    once. A self-approval produces a ``reject`` outcome, exercising both the
    audit-log entry and the escalation-queue delta."""
    from investment_team.api import main as api_main
    from investment_team.models import StrategySpec
    from investment_team.orchestrator import WorkflowState
    from investment_team.temporal.advisory import promotion_decision_activity
    from investment_team.tests.test_investment_team import _sample_ips, _sample_validation

    strategy = StrategySpec(
        strategy_id="s-r",
        authored_by="alice",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    validation = _sample_validation().model_copy(update={"strategy_id": "s-r"})
    ips = _sample_ips()
    user_id = ips.profile.user_id

    monkeypatch.setattr(api_main, "_strategies", {"s-r": strategy})
    monkeypatch.setattr(api_main, "_validations", {"s-r": validation})
    monkeypatch.setattr(api_main, "_profiles", {user_id: ips})
    sentinel_state = WorkflowState()
    monkeypatch.setattr(api_main, "_workflow_state", sentinel_state)

    result = promotion_decision_activity(
        {
            "strategy_id": "s-r",
            "user_id": user_id,
            "proposer_agent_id": "alice",
            "approver_agent_id": "alice",
            "approver_role": "approver",
            "approver_version": "1.0",
            "risk_veto": False,
            "human_live_approval": False,
        }
    )

    assert result["decision"]["outcome"] == "reject"
    assert any(entry.startswith("promotion:s-r:reject") for entry in result["audit_log_appended"])
    assert result["escalation_enqueued"] == {
        "queue": "escalation",
        "payload_id": "s-r",
        "priority": "high",
    }
    # The process-global singleton is untouched — the caller applies the delta.
    assert sentinel_state.audit_log == []
    assert sentinel_state.queues["escalation"] == []
