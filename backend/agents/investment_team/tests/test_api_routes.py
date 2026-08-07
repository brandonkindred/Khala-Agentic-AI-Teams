"""Route-level coverage for ``investment_team.api.main``.

Uses ``fastapi.testclient.TestClient`` against the live ``app`` after
swapping the module-level persistent dicts with in-memory shims. The
tests focus on validation/branching that the existing per-helper unit
tests don't cover:

* ``POST /profiles`` — happy path + invalid risk_tolerance + invalid
  default_mode + IPS lookup on /profiles/{user_id}.
* ``POST /proposals/create`` — happy path + 404 when IPS missing.
* ``GET /proposals/{id}`` — found + not found.
* ``POST /proposals/{id}/validate`` — happy + missing proposal/ips.
* ``POST /strategies`` — happy + strict extra=forbid 422.
* ``POST /strategies/{id}/validate`` — happy (custom checks + defaults)
  + 404 when strategy missing.
* ``POST /promotions/decide`` — 404/400 paths and happy path.
* ``GET /workflow/status`` and ``GET /workflow/queues``.
* ``POST /memos``.
* Strategy lab listing/config/results, paper-trade results, providers.
* Advisor session lifecycle (start → messages → get → complete + errors).
* Backtest job lifecycle (list/status/cancel/delete + 404s).

Every test is hermetic: no LLM, no Postgres, no network. Persistent
dicts and the job-service-backed dicts inside ``api.main`` are stubbed
at module scope.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

import pytest


class _InMemoryDict:
    """In-memory shim mirroring ``api.main._PersistentDict``.

    Stores the original value verbatim (Pydantic models keep their type) so the
    routes that access ``.profile.schema_version`` etc. work the same as when
    the API has just-written values still in process memory. Production's
    ``_PersistentDict`` round-trips through JSON; tests for those paths use the
    re-hydration helpers in the routes themselves.
    """

    def __init__(self) -> None:
        self._d: Dict[str, Any] = {}

    def __setitem__(self, key: str, value: Any) -> None:
        self._d[key] = value

    def __getitem__(self, key: str) -> Any:
        return self._d[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._d

    def __delitem__(self, key: str) -> None:
        """Preconditions: none. Postconditions: removes ``key``; raises ``KeyError`` if absent."""
        del self._d[key]

    def pop(self, key: str, *args: Any) -> Any:
        """Preconditions: at most one default in ``args`` (else ``TypeError``, matching
        ``dict.pop``). Postconditions: removes and returns ``key``'s value, or the sole
        default if provided and ``key`` is absent; raises ``KeyError`` if absent and no
        default was given."""
        if len(args) > 1:
            raise TypeError(f"pop expected at most 2 arguments, got {1 + len(args)}")
        if args:
            return self._d.pop(key, args[0])
        return self._d.pop(key)

    def values(self) -> List[Any]:
        return list(self._d.values())


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch):
    """Return a TestClient with the module's persistent dicts replaced."""
    from fastapi.testclient import TestClient

    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_profiles", _InMemoryDict())
    monkeypatch.setattr(api_main, "_proposals", _InMemoryDict())
    monkeypatch.setattr(api_main, "_strategies", _InMemoryDict())
    monkeypatch.setattr(api_main, "_validations", _InMemoryDict())
    monkeypatch.setattr(api_main, "_backtests", _InMemoryDict())
    monkeypatch.setattr(api_main, "_strategy_lab_records", _InMemoryDict())
    monkeypatch.setattr(api_main, "_paper_trading_sessions", _InMemoryDict())
    monkeypatch.setattr(api_main, "_advisor_sessions", _InMemoryDict())

    # Reset workflow state to a known baseline so audit-log queries are
    # deterministic across tests.
    from investment_team.orchestrator import WorkflowState

    monkeypatch.setattr(api_main, "_workflow_state", WorkflowState())

    return TestClient(api_main.app)


def test_inmemorydict_delitem_raises_keyerror_for_missing_key() -> None:
    d = _InMemoryDict()
    with pytest.raises(KeyError):
        del d["missing"]


def test_inmemorydict_pop_raises_typeerror_for_extra_defaults() -> None:
    d = _InMemoryDict()
    with pytest.raises(TypeError):
        d.pop("missing", "default1", "default2")


# ---------------------------------------------------------------------------
# Profile + Proposal routes
# ---------------------------------------------------------------------------


def _profile_payload(user_id: str = "u1", **overrides: Any) -> Dict[str, Any]:
    payload = {
        "user_id": user_id,
        "risk_tolerance": "medium",
        "max_drawdown_tolerance_pct": 20.0,
        "time_horizon_years": 10,
        "annual_gross_income": 120_000.0,
        "total_net_worth": 300_000.0,
        "investable_assets": 200_000.0,
        "tax_country": "US",
        "monthly_savings": 1000.0,
        "annual_savings": 12_000.0,
        "max_single_position_pct": 10.0,
        "max_asset_class_pct": {"equities": 70.0, "crypto": 5.0},
        "goals": [
            {
                "name": "retire",
                "target_amount": 1_000_000,
                "target_date": "2040-01-01T00:00:00Z",
                "priority": "high",
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_health(api_client) -> None:
    resp = api_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "timestamp" in body


def test_create_profile_happy_path_round_trips(api_client) -> None:
    resp = api_client.post("/profiles", json=_profile_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "u1"
    assert body["ips"]["profile"]["risk_tolerance"] == "medium"

    # /profiles/{user_id} returns the same IPS now.
    resp_get = api_client.get("/profiles/u1")
    assert resp_get.status_code == 200
    body_get = resp_get.json()
    assert body_get["found"] is True
    assert body_get["ips"]["profile"]["user_id"] == "u1"


def test_create_profile_invalid_risk_tolerance(api_client) -> None:
    """An unrecognized risk_tolerance is rejected at the request-validation
    boundary (422), not passed through to business logic — risk_tolerance is
    typed as the ``RiskTolerance`` enum itself."""
    resp = api_client.post("/profiles", json=_profile_payload(risk_tolerance="extreme"))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list)
    assert any("risk_tolerance" in str(err.get("loc", "")) for err in detail)


def test_create_profile_invalid_default_mode(api_client) -> None:
    """An unrecognized default_mode is rejected at the request-validation
    boundary (422) — default_mode is typed as the ``WorkflowMode`` enum
    itself."""
    resp = api_client.post("/profiles", json=_profile_payload(default_mode="wild"))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list)
    assert any("default_mode" in str(err.get("loc", "")) for err in detail)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("income_stability", "bankrupt"),
        ("esg_preference", "extreme"),
        ("rebalance_frequency", "daily"),
    ],
)
def test_create_profile_rejects_undocumented_enum_like_values(api_client, field, value) -> None:
    """income_stability/esg_preference/rebalance_frequency each have a closed,
    documented value set (see the Angular profile form) — an undocumented
    value is rejected with 422, not silently accepted."""
    resp = api_client.post("/profiles", json=_profile_payload(**{field: value}))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any(field in str(err.get("loc", "")) for err in detail)


@pytest.mark.parametrize("field", ["max_single_position_pct", "speculative_sleeve_cap_pct"])
@pytest.mark.parametrize("value", [-1.0, 100.1])
def test_create_profile_rejects_out_of_range_percentages(api_client, field, value) -> None:
    resp = api_client.post("/profiles", json=_profile_payload(**{field: value}))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any(field in str(err.get("loc", "")) for err in detail)


@pytest.mark.parametrize("field", ["max_single_position_pct", "speculative_sleeve_cap_pct"])
def test_create_profile_accepts_in_range_percentages(api_client, field) -> None:
    resp = api_client.post("/profiles", json=_profile_payload(**{field: 50.0}))
    assert resp.status_code == 200


def test_create_profile_rejects_unknown_field(api_client) -> None:
    """CreateProfileRequest sets extra="forbid" — a stale/misspelled field
    is rejected with 422 rather than silently dropped."""
    payload = _profile_payload()
    payload["not_a_real_field"] = "x"
    resp = api_client.post("/profiles", json=payload)
    assert resp.status_code == 422


def test_create_profile_duplicate_user_id_returns_409(api_client) -> None:
    first = api_client.post("/profiles", json=_profile_payload())
    assert first.status_code == 200
    first_ips = first.json()["ips"]

    second = api_client.post("/profiles", json=_profile_payload())
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"]

    # The original profile must survive untouched — no silent overwrite.
    got = api_client.get("/profiles/u1")
    assert got.status_code == 200
    assert got.json()["ips"] == first_ips


def test_create_profile_non_dict_goal_rejected(api_client) -> None:
    # ``CreateProfileRequest.goals`` is typed ``List[Dict[str, Any]]``, so a
    # non-dict element should already be rejected by FastAPI/Pydantic request
    # validation before the handler runs — not by handler code.
    resp = api_client.post("/profiles", json=_profile_payload(goals=["not-a-dict"]))
    assert resp.status_code == 422


def test_create_profile_malformed_goal_field_returns_422(api_client) -> None:
    # A goal dict that type-checks as Dict[str, Any] at the request boundary
    # but fails UserGoal's stricter field types (target_amount: float) must
    # surface as a 422 with Pydantic-shaped detail, not an unhandled 500.
    resp = api_client.post(
        "/profiles",
        json=_profile_payload(
            goals=[
                {
                    "name": "retire",
                    "target_amount": "not-a-number",
                    "target_date": "2040-01-01T00:00:00Z",
                    "priority": "high",
                }
            ]
        ),
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list)
    assert any("target_amount" in str(err.get("loc", "")) for err in detail)


def test_get_profile_not_found_returns_found_false(api_client) -> None:
    resp = api_client.get("/profiles/no-such-user")
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is False
    assert body["ips"] is None


def _proposal_body() -> Dict[str, Any]:
    """Compliant proposal: one position within all caps (≤10% single, ≤70% equities)."""
    return {
        "prepared_by": "designer",
        "user_id": "u1",
        "objective": "balanced",
        "positions": [
            {
                "symbol": "VTI",
                "asset_class": "equities",
                "weight_pct": 5.0,
                "rationale": "core",
            }
        ],
        "assumptions": ["baseline"],
    }


def test_create_proposal_round_trip_then_validate(api_client) -> None:
    api_client.post("/profiles", json=_profile_payload())
    resp = api_client.post("/proposals/create", json=_proposal_body())
    assert resp.status_code == 200
    pid = resp.json()["proposal_id"]
    assert pid.startswith("prop-")

    # Fetch it back.
    got = api_client.get(f"/proposals/{pid}")
    assert got.status_code == 200
    assert got.json()["found"] is True

    # Validate (compliant with IPS).
    vresp = api_client.post(
        f"/proposals/{pid}/validate",
        json={"user_id": "u1"},
    )
    assert vresp.status_code == 200
    body = vresp.json()
    assert body["valid"] is True
    assert body["violations"] == []


def test_create_proposal_404_when_no_ips(api_client) -> None:
    resp = api_client.post("/proposals/create", json=_proposal_body())
    assert resp.status_code == 404
    assert "No IPS found" in resp.json()["detail"]


def test_get_proposal_not_found(api_client) -> None:
    resp = api_client.get("/proposals/nope")
    assert resp.status_code == 200
    assert resp.json()["found"] is False


def test_validate_proposal_404_when_proposal_missing(api_client) -> None:
    api_client.post("/profiles", json=_profile_payload())
    resp = api_client.post(
        "/proposals/missing/validate",
        json={"user_id": "u1"},
    )
    assert resp.status_code == 404


def test_validate_proposal_404_when_ips_missing(api_client) -> None:
    api_client.post("/profiles", json=_profile_payload())
    resp = api_client.post("/proposals/create", json=_proposal_body())
    pid = resp.json()["proposal_id"]
    # Validate against a non-existent user
    vresp = api_client.post(
        f"/proposals/{pid}/validate",
        json={"user_id": "ghost"},
    )
    assert vresp.status_code == 404


# ---------------------------------------------------------------------------
# Strategy routes
# ---------------------------------------------------------------------------


def _strategy_body() -> Dict[str, Any]:
    return {
        "authored_by": "ideation",
        "asset_class": "equities",
        "hypothesis": "momentum",
        "signal_definition": "ema crossover",
        "timeframe": "1d",
        "entry_rules": [
            {
                "kind": "entry",
                "side": "long",
                "when": {"lhs": "bar.close", "op": ">", "rhs": 100.0},
            }
        ],
        "exit_rules": [
            {
                "kind": "stop_loss",
                "pct": 0.05,
            }
        ],
        "risk_limits": {},
        "speculative": False,
    }


def test_create_strategy_returns_id_and_persists(api_client) -> None:
    resp = api_client.post("/strategies", json=_strategy_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy_id"].startswith("strat-")
    # ``equities`` is an accepted alias; StrategySpec canonicalizes it to the
    # canonical ``stocks`` label at the spec boundary.
    assert body["strategy"]["asset_class"] == "stocks"


def test_create_strategy_rejects_unknown_field(api_client) -> None:
    """``extra=forbid`` on CreateStrategyRequest: extra field → 422."""
    payload = _strategy_body()
    payload["sizing_rules"] = []  # legacy field — rejected
    resp = api_client.post("/strategies", json=payload)
    assert resp.status_code == 422


def test_create_strategy_unsupported_asset_class_returns_422(api_client) -> None:
    """An off-vocabulary asset_class trips StrategySpec's validator at
    construction inside the handler; that must surface as a 422 client error,
    not an unhandled 500, with the structured error body naming the field."""
    payload = _strategy_body()
    payload["asset_class"] = "bonds"
    resp = api_client.post("/strategies", json=payload)
    assert resp.status_code == 422
    body = resp.json()
    assert any("asset_class" in err.get("loc", []) for err in body["detail"])


def test_validate_strategy_404_when_missing(api_client) -> None:
    resp = api_client.post(
        "/strategies/nope/validate",
        json={"backtest_period": "2020-2024", "scenario_set": [], "checks": []},
    )
    assert resp.status_code == 404


def test_validate_strategy_default_checks_pass(api_client) -> None:
    create = api_client.post("/strategies", json=_strategy_body())
    sid = create.json()["strategy_id"]
    resp = api_client.post(
        f"/strategies/{sid}/validate",
        json={"backtest_period": "2020-2024", "scenario_set": ["baseline"], "checks": []},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["passed"] is True
    assert body["failures"] == []
    # Default checks list (5 entries).
    assert len(body["validation"]["checks"]) == 5


def test_validate_strategy_custom_checks_with_failure(api_client) -> None:
    create = api_client.post("/strategies", json=_strategy_body())
    sid = create.json()["strategy_id"]
    resp = api_client.post(
        f"/strategies/{sid}/validate",
        json={
            "backtest_period": "2020-2024",
            "scenario_set": ["baseline"],
            "checks": [
                {"name": "backtest", "status": "fail", "details": "shoddy Sharpe"},
                {"name": "wf", "status": "invalid_status", "details": "ok"},  # ValueError branch
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["passed"] is False
    assert "shoddy Sharpe" in body["failures"]


# ---------------------------------------------------------------------------
# Promotion + Workflow + Memo routes
# ---------------------------------------------------------------------------


def test_promotion_decision_404_strategy_missing(api_client) -> None:
    api_client.post("/profiles", json=_profile_payload())
    resp = api_client.post(
        "/promotions/decide",
        json={
            "strategy_id": "missing",
            "user_id": "u1",
            "proposer_agent_id": "p1",
            "approver_agent_id": "a1",
        },
    )
    assert resp.status_code == 404


def test_promotion_decision_400_when_validation_missing(api_client) -> None:
    api_client.post("/profiles", json=_profile_payload())
    create = api_client.post("/strategies", json=_strategy_body())
    sid = create.json()["strategy_id"]
    resp = api_client.post(
        "/promotions/decide",
        json={
            "strategy_id": sid,
            "user_id": "u1",
            "proposer_agent_id": "p1",
            "approver_agent_id": "a1",
        },
    )
    assert resp.status_code == 400


def test_promotion_decision_404_when_ips_missing(api_client) -> None:
    create = api_client.post("/strategies", json=_strategy_body())
    sid = create.json()["strategy_id"]
    # Pre-populate a validation report so we land on the IPS lookup.
    api_client.post(
        f"/strategies/{sid}/validate",
        json={"backtest_period": "2020-2024", "scenario_set": [], "checks": []},
    )
    resp = api_client.post(
        "/promotions/decide",
        json={
            "strategy_id": sid,
            "user_id": "ghost",
            "proposer_agent_id": "p1",
            "approver_agent_id": "a1",
        },
    )
    assert resp.status_code == 404


def test_promotion_decision_happy_path(api_client) -> None:
    api_client.post("/profiles", json=_profile_payload())
    create = api_client.post("/strategies", json=_strategy_body())
    sid = create.json()["strategy_id"]
    api_client.post(
        f"/strategies/{sid}/validate",
        json={"backtest_period": "2020-2024", "scenario_set": [], "checks": []},
    )
    resp = api_client.post(
        "/promotions/decide",
        json={
            "strategy_id": sid,
            "user_id": "u1",
            "proposer_agent_id": "p1",
            "approver_agent_id": "a1",
            "human_live_approval": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy_id"] == sid
    assert "decision" in body


def test_promotion_decision_updates_workflow_status_in_api_process(api_client) -> None:
    """The route applies the activity's returned audit-log/escalation delta to
    the local ``_workflow_state`` — a reject decision must show up in a
    subsequent ``/workflow/status``/``/workflow/queues`` call in this same
    process, mirroring how the activity may run in a separate worker."""
    api_client.post("/profiles", json=_profile_payload())
    create = api_client.post("/strategies", json=_strategy_body())
    sid = create.json()["strategy_id"]
    api_client.post(
        f"/strategies/{sid}/validate",
        json={"backtest_period": "2020-2024", "scenario_set": [], "checks": []},
    )
    # Self-approval (proposer == approver) triggers a "reject" outcome.
    resp = api_client.post(
        "/promotions/decide",
        json={
            "strategy_id": sid,
            "user_id": "u1",
            "proposer_agent_id": "p1",
            "approver_agent_id": "p1",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["decision"]["outcome"] == "reject"

    status = api_client.get("/workflow/status").json()
    assert any(entry.startswith(f"promotion:{sid}:reject") for entry in status["audit_log"])
    assert status["queue_counts"]["escalation"] == 1

    queues = api_client.get("/workflow/queues").json()
    assert any(item["payload_id"] == sid for item in queues["queues"]["escalation"])


def _promotion_decide_setup(api_client) -> str:
    """Create a profile, strategy, and validation report; return the strategy_id."""
    api_client.post("/profiles", json=_profile_payload())
    create = api_client.post("/strategies", json=_strategy_body())
    sid = create.json()["strategy_id"]
    api_client.post(
        f"/strategies/{sid}/validate",
        json={"backtest_period": "2020-2024", "scenario_set": [], "checks": []},
    )
    return sid


def test_promotion_decision_missing_decision_field_returns_500(api_client, monkeypatch) -> None:
    """A result missing 'decision' triggers a clean 500, not an unhandled KeyError."""
    from investment_team.api import main as api_main

    sid = _promotion_decide_setup(api_client)
    monkeypatch.setattr(api_main, "_execute_advisory", lambda op, payload, *, key: {})
    resp = api_client.post(
        "/promotions/decide",
        json={
            "strategy_id": sid,
            "user_id": "u1",
            "proposer_agent_id": "p1",
            "approver_agent_id": "a1",
        },
    )
    assert resp.status_code == 500
    assert "decision" in resp.json()["detail"]


def test_promotion_decision_invalid_decision_payload_returns_500(api_client, monkeypatch) -> None:
    """A present but schema-invalid 'decision' is a server/integration fault → 500,
    matching advisor routes' handling of malformed Temporal payloads."""
    from investment_team.api import main as api_main

    sid = _promotion_decide_setup(api_client)
    monkeypatch.setattr(
        api_main, "_execute_advisory", lambda op, payload, *, key: {"decision": {"not": "valid"}}
    )
    resp = api_client.post(
        "/promotions/decide",
        json={
            "strategy_id": sid,
            "user_id": "u1",
            "proposer_agent_id": "p1",
            "approver_agent_id": "a1",
        },
    )
    assert resp.status_code == 500
    assert "decision" in resp.json()["detail"].lower() or "invalid" in resp.json()["detail"].lower()


def test_promotion_decision_unknown_escalation_queue_returns_500(api_client, monkeypatch) -> None:
    """An escalation naming a queue outside the known set is rejected before any
    _workflow_state mutation, instead of raising an unhandled KeyError."""
    from investment_team.api import main as api_main

    sid = _promotion_decide_setup(api_client)
    decision = {"strategy_id": sid, "decided_by": "a1", "outcome": "paper", "rationale": "ok"}
    monkeypatch.setattr(
        api_main,
        "_execute_advisory",
        lambda op, payload, *, key: {
            "decision": decision,
            "escalation_enqueued": {
                "queue": "not-a-real-queue",
                "payload_id": sid,
                "priority": "high",
            },
        },
    )
    resp = api_client.post(
        "/promotions/decide",
        json={
            "strategy_id": sid,
            "user_id": "u1",
            "proposer_agent_id": "p1",
            "approver_agent_id": "a1",
        },
    )
    assert resp.status_code == 500
    assert "queue" in resp.json()["detail"].lower()
    assert api_main._workflow_state.audit_log == []
    assert all(len(q) == 0 for q in api_main._workflow_state.queues.values())


def test_promotion_decision_escalation_missing_payload_id_does_not_mutate_audit_log(
    api_client, monkeypatch
) -> None:
    """A known-queue escalation missing payload_id must 500 before any shared-state
    mutation — not extend audit_log then raise KeyError inside the lock."""
    from investment_team.api import main as api_main

    sid = _promotion_decide_setup(api_client)
    decision = {"strategy_id": sid, "decided_by": "a1", "outcome": "paper", "rationale": "ok"}
    monkeypatch.setattr(
        api_main,
        "_execute_advisory",
        lambda op, payload, *, key: {
            "decision": decision,
            "audit_log_appended": [f"promotion:{sid}:paper"],
            "escalation_enqueued": {
                "queue": "escalation",
                "priority": "high",
            },
        },
    )
    resp = api_client.post(
        "/promotions/decide",
        json={
            "strategy_id": sid,
            "user_id": "u1",
            "proposer_agent_id": "p1",
            "approver_agent_id": "a1",
        },
    )
    assert resp.status_code == 500
    assert "escalation" in resp.json()["detail"].lower()
    assert api_main._workflow_state.audit_log == []
    assert all(len(q) == 0 for q in api_main._workflow_state.queues.values())


def test_promotion_decision_non_dict_escalation_does_not_mutate_audit_log(
    api_client, monkeypatch
) -> None:
    """A truthy non-dict escalation_enqueued must 500 before mutating shared state."""
    from investment_team.api import main as api_main

    sid = _promotion_decide_setup(api_client)
    decision = {"strategy_id": sid, "decided_by": "a1", "outcome": "paper", "rationale": "ok"}
    monkeypatch.setattr(
        api_main,
        "_execute_advisory",
        lambda op, payload, *, key: {
            "decision": decision,
            "audit_log_appended": [f"promotion:{sid}:paper"],
            "escalation_enqueued": "not-a-dict",
        },
    )
    resp = api_client.post(
        "/promotions/decide",
        json={
            "strategy_id": sid,
            "user_id": "u1",
            "proposer_agent_id": "p1",
            "approver_agent_id": "a1",
        },
    )
    assert resp.status_code == 500
    assert api_main._workflow_state.audit_log == []
    assert all(len(q) == 0 for q in api_main._workflow_state.queues.values())


def test_promotion_decision_invalid_audit_log_entries_returns_500(api_client, monkeypatch) -> None:
    """Non-string audit-log entries are rejected before extending _workflow_state,
    instead of corrupting the audit log or raising an unhandled TypeError."""
    from investment_team.api import main as api_main

    sid = _promotion_decide_setup(api_client)
    decision = {"strategy_id": sid, "decided_by": "a1", "outcome": "paper", "rationale": "ok"}
    monkeypatch.setattr(
        api_main,
        "_execute_advisory",
        lambda op, payload, *, key: {
            "decision": decision,
            "audit_log_appended": [{"not": "a string"}],
        },
    )
    resp = api_client.post(
        "/promotions/decide",
        json={
            "strategy_id": sid,
            "user_id": "u1",
            "proposer_agent_id": "p1",
            "approver_agent_id": "a1",
        },
    )
    assert resp.status_code == 500
    assert api_main._workflow_state.audit_log == []


def test_workflow_status_and_queues(api_client) -> None:
    resp = api_client.get("/workflow/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "mode" in body
    assert isinstance(body["audit_log"], list)
    assert isinstance(body["queue_counts"], dict)

    resp_q = api_client.get("/workflow/queues")
    assert resp_q.status_code == 200
    assert isinstance(resp_q.json()["queues"], dict)


def test_create_memo_returns_memo(api_client) -> None:
    resp = api_client.post(
        "/memos",
        json={
            "user_id": "u1",
            "recommendation": "Buy",
            "rationale": "valuations attractive",
            "dissenting_views": ["bear case"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["memo"]["recommendation"] == "Buy"
    assert "bear case" in body["memo"]["dissenting_views"]


def test_create_memo_502_when_advisory_result_missing_memo(api_client, monkeypatch) -> None:
    """A committee_memo payload without ``memo`` must return 502, not an unhandled 500."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main, "_execute_advisory", lambda op, payload, *, key: {"unexpected": True}
    )
    resp = api_client.post(
        "/memos",
        json={
            "user_id": "u1",
            "recommendation": "Buy",
            "rationale": "valuations attractive",
            "dissenting_views": [],
        },
    )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Memo generation result is missing"


def test_create_memo_invalid_memo_payload_returns_500(api_client, monkeypatch) -> None:
    """A present but schema-invalid 'memo' is a server/integration fault → 500,
    matching advisor routes' handling of malformed Temporal payloads."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main, "_execute_advisory", lambda op, payload, *, key: {"memo": {"not": "valid"}}
    )
    resp = api_client.post(
        "/memos",
        json={
            "user_id": "u1",
            "recommendation": "Buy",
            "rationale": "valuations attractive",
            "dissenting_views": [],
        },
    )
    assert resp.status_code == 500
    assert resp.json()["detail"]


# ---------------------------------------------------------------------------
# Strategy Lab read-only routes
# ---------------------------------------------------------------------------


def test_strategy_lab_config_returns_env_bounds(api_client) -> None:
    resp = api_client.get("/strategy-lab/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["batch_count_min"] == 1
    assert body["batch_count_max"] >= 1


def test_strategy_lab_results_empty_lists(api_client) -> None:
    resp = api_client.get("/strategy-lab/results")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["count"] == 0
    assert body["winning_count"] == 0
    assert body["losing_count"] == 0


def test_strategy_lab_results_filter_by_winning(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    # Seed two records — one winning, one losing.
    cfg = BacktestConfig(start_date="2020-01-01", end_date="2024-12-31", initial_capital=100_000.0)
    strat = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )

    def _record(*, win: bool, lab_id: str) -> StrategyLabRecord:
        result = BacktestResult(
            total_return_pct=10.0 if win else -10.0,
            annualized_return_pct=12.0 if win else -2.0,
            volatility_pct=10.0,
            sharpe_ratio=1.0 if win else -0.5,
            max_drawdown_pct=5.0,
            win_rate_pct=60.0 if win else 40.0,
            profit_factor=2.0 if win else 0.5,
            calmar_ratio=0.0,
            deflated_sharpe=0.0,
            sortino_ratio=0.0,
        )
        bt = BacktestRecord(
            backtest_id=f"bt-{lab_id}",
            strategy_id="s",
            strategy=strat,
            config=cfg,
            submitted_by="x",
            submitted_at="2024-01-01T00:00:00Z",
            completed_at="2024-01-01T01:00:00Z",
            result=result,
            trades=[],
        )
        return StrategyLabRecord(
            lab_record_id=lab_id,
            strategy=strat,
            backtest=bt,
            is_winning=win,
            strategy_rationale="r",
            analysis_narrative="n",
            created_at="2024-01-01T01:00:00Z",
            strategy_code=None,
        )

    api_main._strategy_lab_records["w"] = _record(win=True, lab_id="w")
    api_main._strategy_lab_records["l"] = _record(win=False, lab_id="l")

    # No filter — both rows.
    resp = api_client.get("/strategy-lab/results")
    body = resp.json()
    assert body["winning_count"] == 1
    assert body["losing_count"] == 1
    assert body["count"] == 2

    # Filter winning=true → just the winner. Regression coverage: counts must
    # be derived from the same filtered list as `items`/`count`, not the
    # unfiltered global set -- winning_count + losing_count == count always.
    resp_w = api_client.get("/strategy-lab/results?winning=true")
    body_w = resp_w.json()
    assert [r["lab_record_id"] for r in body_w["items"]] == ["w"]
    assert body_w["count"] == 1
    assert body_w["winning_count"] == 1
    assert body_w["losing_count"] == 0

    # Filter winning=false → just the loser, with matching counts.
    resp_l = api_client.get("/strategy-lab/results?winning=false")
    body_l = resp_l.json()
    assert [r["lab_record_id"] for r in body_l["items"]] == ["l"]
    assert body_l["count"] == 1
    assert body_l["winning_count"] == 0
    assert body_l["losing_count"] == 1


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def test_list_providers(api_client) -> None:
    resp = api_client.get("/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert "providers" in body
    assert "live_paper_enabled" in body


def test_list_providers_malformed_row_returns_500(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A registry row that fails ProviderDescriptor validation -> 500, not a crash."""
    import investment_team.trading_service.providers as providers_mod

    class _BadRegistry:
        def describe_all(self):
            return [{"supports": "not-a-list"}]  # missing required `name`

    monkeypatch.setattr(providers_mod, "default_registry", lambda: _BadRegistry())

    resp = api_client.get("/providers")
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Provider registry contains invalid data"


# ---------------------------------------------------------------------------
# Paper-trade results listing
# ---------------------------------------------------------------------------


def test_paper_trading_results_empty(api_client) -> None:
    resp = api_client.get("/strategy-lab/paper-trade/results")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["count"] == 0


def _paper_trading_session(
    session_id: str,
    verdict,
    *,
    status=None,
    started_at: str = "2024-01-01T00:00:00Z",
    completed_at: str = "2024-01-01T01:00:00Z",
):
    from investment_team.models import PaperTradingSession, PaperTradingStatus, StrategySpec

    strat = StrategySpec(
        strategy_id=f"strat-{session_id}",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    return PaperTradingSession(
        session_id=session_id,
        lab_record_id=f"lab-{session_id}",
        strategy=strat,
        status=status or PaperTradingStatus.COMPLETED,
        initial_capital=100_000.0,
        current_capital=100_000.0,
        verdict=verdict,
        started_at=started_at,
        completed_at=completed_at,
    )


def test_paper_trading_results_response_counts_are_derived_from_items() -> None:
    """Constructing the response with mismatched standalone counts must not
    stick — the model derives them from ``items`` regardless of what's passed.
    """
    from investment_team.api.main import PaperTradingResultsResponse
    from investment_team.models import PaperTradingVerdict

    items = [
        _paper_trading_session("a", PaperTradingVerdict.READY_FOR_LIVE),
        _paper_trading_session("b", PaperTradingVerdict.NOT_PERFORMANT),
        _paper_trading_session("c", PaperTradingVerdict.NOT_PERFORMANT),
    ]
    resp = PaperTradingResultsResponse(
        items=items, count=999, ready_for_live_count=999, not_performant_count=999
    )
    assert resp.count == 3
    assert resp.ready_for_live_count == 1
    assert resp.not_performant_count == 2


@pytest.mark.parametrize(
    "verdict,expected_ids,expected_ready,expected_not",
    [
        ("ready_for_live", ["a"], 1, 0),
        ("not_performant", ["b", "c"], 0, 2),
    ],
)
def test_paper_trading_results_verdict_filter_counts_match_filtered_items(
    api_client, verdict, expected_ids, expected_ready, expected_not
) -> None:
    """Filtering by ``verdict`` must return counts consistent with the
    filtered ``items``, not global totals across all sessions.
    """
    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingVerdict

    api_main._paper_trading_sessions["a"] = _paper_trading_session(
        "a", PaperTradingVerdict.READY_FOR_LIVE
    )
    api_main._paper_trading_sessions["b"] = _paper_trading_session(
        "b", PaperTradingVerdict.NOT_PERFORMANT
    )
    api_main._paper_trading_sessions["c"] = _paper_trading_session(
        "c", PaperTradingVerdict.NOT_PERFORMANT
    )

    resp = api_client.get(f"/strategy-lab/paper-trade/results?verdict={verdict}")
    assert resp.status_code == 200
    body = resp.json()
    assert [i["session_id"] for i in body["items"]] == expected_ids
    assert body["count"] == len(expected_ids)
    assert body["ready_for_live_count"] == expected_ready
    assert body["not_performant_count"] == expected_not


def test_paper_trading_results_rejects_unknown_verdict(api_client) -> None:
    """An unrecognized ``verdict`` must 422, not silently match nothing."""
    resp = api_client.get("/strategy-lab/paper-trade/results?verdict=bogus")
    assert resp.status_code == 422


def test_paper_trading_results_orders_terminal_before_active_sessions(api_client) -> None:
    """Terminal (completed/failed) sessions sort first by ``completed_at``
    descending; active sessions sort after, by ``started_at`` descending.

    A single ``completed_at or started_at`` sort key would let an in-flight
    session's ``started_at`` outrank a genuinely more-recent completed
    session's ``completed_at`` — the two timestamps aren't the same kind of
    "recency". Here the active session's ``started_at`` (2024-01-03) is later
    than either completed session's ``completed_at``, so a naive single-key
    sort would put it first; the bucketed policy must still rank it last.
    """
    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingStatus, PaperTradingVerdict

    api_main._paper_trading_sessions["older-completed"] = _paper_trading_session(
        "older-completed",
        PaperTradingVerdict.READY_FOR_LIVE,
        completed_at="2024-01-01T00:00:00Z",
    )
    api_main._paper_trading_sessions["newer-completed"] = _paper_trading_session(
        "newer-completed",
        PaperTradingVerdict.NOT_PERFORMANT,
        completed_at="2024-01-02T00:00:00Z",
    )
    api_main._paper_trading_sessions["still-active"] = _paper_trading_session(
        "still-active",
        None,
        status=PaperTradingStatus.LIVE,
        started_at="2024-01-03T00:00:00Z",
        completed_at="",
    )

    resp = api_client.get("/strategy-lab/paper-trade/results")
    assert resp.status_code == 200
    body = resp.json()
    assert [i["session_id"] for i in body["items"]] == [
        "newer-completed",
        "older-completed",
        "still-active",
    ]


def test_paper_trading_results_orders_by_real_time_not_lexicographic_string(
    api_client,
) -> None:
    """A ``completed_at`` with a non-UTC timezone offset must sort by its
    actual chronological instant, not by comparing the raw ISO-8601 strings
    lexicographically — the two can disagree when the date/time digits don't
    happen to move in the same direction as the UTC-normalized instant."""
    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingVerdict

    # 2024-01-01T23:00:00+00:00 == 2024-01-01T23:00:00 UTC — the later instant.
    api_main._paper_trading_sessions["actually-newer"] = _paper_trading_session(
        "actually-newer",
        PaperTradingVerdict.READY_FOR_LIVE,
        completed_at="2024-01-01T23:00:00+00:00",
    )
    # 2024-01-02T00:30:00+02:00 == 2024-01-01T22:30:00 UTC — the earlier
    # instant, but its string sorts *after* the one above lexicographically
    # because the date digit "02" outranks "01" in plain string comparison.
    api_main._paper_trading_sessions["actually-older"] = _paper_trading_session(
        "actually-older",
        PaperTradingVerdict.NOT_PERFORMANT,
        completed_at="2024-01-02T00:30:00+02:00",
    )

    resp = api_client.get("/strategy-lab/paper-trade/results")
    assert resp.status_code == 200
    body = resp.json()
    assert [i["session_id"] for i in body["items"]] == [
        "actually-newer",
        "actually-older",
    ]


def test_paper_trading_results_skips_unparseable_session(api_client) -> None:
    """A single corrupt in-memory session record must not 500 the bulk
    endpoint — it should be logged and excluded, while parseable sessions
    are still returned (matching the recovery-pass and single-session
    lookup behavior).
    """
    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingVerdict

    api_main._paper_trading_sessions["good"] = _paper_trading_session(
        "good", PaperTradingVerdict.READY_FOR_LIVE
    )
    # Missing required fields (e.g. strategy, status) -> parse_persisted raises.
    api_main._paper_trading_sessions["corrupt"] = {"session_id": "corrupt"}

    resp = api_client.get("/strategy-lab/paper-trade/results")
    assert resp.status_code == 200
    body = resp.json()
    assert [i["session_id"] for i in body["items"]] == ["good"]
    assert body["count"] == 1


def test_paper_trading_session_get_404(api_client) -> None:
    resp = api_client.get("/strategy-lab/paper-trade/pt-missing")
    assert resp.status_code == 404


def test_paper_trading_session_get_corrupted_record_returns_500(api_client) -> None:
    """A corrupted persisted record must surface as a controlled 500 with a
    generic client-facing detail, not leak the raw parse/validation
    exception as an unhandled 500."""
    from investment_team.api import main as api_main

    # Missing required fields (e.g. strategy, status) -> parse_persisted raises.
    api_main._paper_trading_sessions["pt-corrupt"] = {"session_id": "pt-corrupt"}

    resp = api_client.get("/strategy-lab/paper-trade/pt-corrupt")
    assert resp.status_code == 500
    assert resp.json()["detail"] == (
        "Paper trading session 'pt-corrupt' is corrupted and cannot be loaded."
    )


def test_stop_live_paper_trading_disabled_returns_404(api_client) -> None:
    """When INVESTMENT_LIVE_PAPER_ENABLED is off, the stop endpoint must 404."""
    resp = api_client.post("/strategy-lab/paper-trade/anything/stop")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Strategy Lab run-tracking — read paths (no worker)
# ---------------------------------------------------------------------------


def test_list_strategy_lab_runs_empty(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_active_runs", {})

    # Stub the JobServiceClient so we don't hit the real one.
    class _Stub:
        def __init__(self, *args, **kwargs):
            pass

        def list_jobs(self, statuses=None):
            return []

        def get_job(self, job_id: str):
            return None

        def delete_job(self, job_id: str) -> bool:
            return False

    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _Stub())

    resp = api_client.get("/strategy-lab/runs")
    assert resp.status_code == 200
    assert resp.json()["runs"] == []


def test_list_strategy_lab_jobs_empty(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_active_runs", {})

    class _Stub:
        def __init__(self, *args, **kwargs):
            pass

        def list_jobs(self, statuses=None):
            return []

    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _Stub())

    resp = api_client.get("/strategy-lab/jobs")
    assert resp.status_code == 200
    assert resp.json()["jobs"] == []


def test_get_strategy_lab_run_status_404(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state as _run_state

    monkeypatch.setattr(api_main, "_active_runs", {})

    class _Stub:
        def __init__(self, *args, **kwargs):
            pass

        def get_job(self, job_id: str):
            return None

    # Patch both: the status endpoint's own reconciliation client
    # (api_main._get_lab_run_job_client) and the job-service fallback
    # run_state.load_run_from_job_service builds its client from
    # (run_state.get_lab_run_job_client) -- distinct module-level functions.
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _Stub())
    monkeypatch.setattr(_run_state, "get_lab_run_job_client", lambda: _Stub())

    resp = api_client.get("/strategy-lab/runs/no-such/status")
    assert resp.status_code == 404


def test_delete_strategy_lab_run_404(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_active_runs", {})

    class _Stub:
        def __init__(self, *args, **kwargs):
            pass

        def delete_job(self, job_id: str) -> bool:
            return False

    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _Stub())

    resp = api_client.delete("/strategy-lab/runs/no-such")
    assert resp.status_code == 404


def test_delete_strategy_lab_record_404(api_client) -> None:
    resp = api_client.delete("/strategy-lab/records/no-such")
    assert resp.status_code == 404


def test_run_paper_trading_404_when_lab_record_missing(api_client) -> None:
    resp = api_client.post(
        "/strategy-lab/paper-trade",
        json={"lab_record_id": "missing"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Backtest job lifecycle
# ---------------------------------------------------------------------------


def test_get_backtest_job_status_404(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_bt_get_job", lambda jid: None)
    resp = api_client.get("/backtests/status/missing")
    assert resp.status_code == 404


def test_list_backtest_jobs_returns_items(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main,
        "_bt_list_jobs",
        lambda statuses=None: [
            {
                "job_id": "j1",
                "status": "running",
                "strategy_id": "s1",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:01:00Z",
            }
        ],
    )

    resp = api_client.get("/backtests/jobs?running_only=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["jobs"][0]["job_id"] == "j1"
    assert body["jobs"][0]["status"] == "running"


def test_cancel_backtest_job_404(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_bt_get_job", lambda jid: None)
    resp = api_client.post("/backtests/jobs/no-such/cancel")
    assert resp.status_code == 404


def test_cancel_backtest_job_success_and_failure(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_bt_get_job", lambda jid: {"status": "running"})
    monkeypatch.setattr(api_main, "_bt_cancel_job", lambda jid: True)
    resp = api_client.post("/backtests/jobs/j1/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["status"] == "cancelled"

    # When cancel returns False the job cannot be cancelled: 409, not 200.
    monkeypatch.setattr(api_main, "_bt_cancel_job", lambda jid: False)
    resp_no = api_client.post("/backtests/jobs/j1/cancel")
    assert resp_no.status_code == 409
    assert "Cannot cancel" in resp_no.json()["detail"]


def test_delete_backtest_job_404_when_missing(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    """Covers both a genuinely nonexistent job and a concurrent-delete race:
    since there is no separate existence check to race, either case maps to
    404 from ``_bt_delete_job``'s own return value, never a misleading 500."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_bt_delete_job", lambda jid: False)
    resp = api_client.delete("/backtests/jobs/j1")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_delete_backtest_job_success(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_bt_delete_job", lambda jid: True)
    resp = api_client.delete("/backtests/jobs/j1")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True


def test_list_backtests_empty(api_client) -> None:
    resp = api_client.get("/backtests")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["count"] == 0


# ---------------------------------------------------------------------------
# Advisor session lifecycle
# ---------------------------------------------------------------------------


def test_advisor_session_lifecycle(api_client) -> None:
    """start → message → get → complete-when-incomplete error."""
    start = api_client.post("/advisor/sessions", json={"user_id": "u1"})
    assert start.status_code == 200
    body = start.json()
    sid = body["session_id"]

    # Send a message — should bounce and return a reply.
    msg = api_client.post(f"/advisor/sessions/{sid}/messages", json={"message": "hello"})
    assert msg.status_code == 200

    # Get the session.
    got = api_client.get(f"/advisor/sessions/{sid}")
    assert got.status_code == 200
    assert got.json()["found"] is True

    # Complete it without filling required fields → 400.
    done = api_client.post(f"/advisor/sessions/{sid}/complete")
    assert done.status_code == 400


def test_start_advisor_session_id_has_full_uuid4_entropy(api_client) -> None:
    """Session ids must carry the full 128-bit UUID4, not a truncated prefix."""
    start = api_client.post("/advisor/sessions", json={"user_id": "u1"})
    sid = start.json()["session_id"]
    assert sid.startswith("adv-")
    suffix = sid[len("adv-") :]
    assert len(suffix) == 32
    uuid.UUID(hex=suffix)


def test_start_advisor_session_rejects_empty_user_id(api_client) -> None:
    """user_id has min_length=1 — an empty string is rejected with 422
    rather than creating a session tied to an invalid identifier."""
    resp = api_client.post("/advisor/sessions", json={"user_id": ""})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any("user_id" in str(err.get("loc", "")) for err in detail)


def test_advisor_session_404_for_missing_ids(api_client) -> None:
    # send_advisor_message → 404
    resp = api_client.post("/advisor/sessions/missing/messages", json={"message": "x"})
    assert resp.status_code == 404

    # complete_advisor_session → 404
    resp_c = api_client.post("/advisor/sessions/missing/complete")
    assert resp_c.status_code == 404


def test_get_advisor_session_returns_found_false_for_missing(api_client) -> None:
    resp = api_client.get("/advisor/sessions/missing")
    assert resp.status_code == 200
    assert resp.json()["found"] is False


def test_start_advisor_session_500_on_malformed_advisory_result(api_client, monkeypatch) -> None:
    """A workflow result missing expected keys surfaces as a clean 500, not a KeyError."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_execute_advisory", lambda op, payload, *, key: {"session": {}})
    resp = api_client.post("/advisor/sessions", json={"user_id": "u1"})
    assert resp.status_code == 500
    assert resp.json()["detail"]


def test_start_advisor_session_malformed_session_payload_returns_500(
    api_client, monkeypatch
) -> None:
    """A present but schema-invalid 'session' triggers a clean 500 via
    ValidationError, not an unhandled exception."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main,
        "_execute_advisory",
        lambda op, payload, *, key: {"advisor_message": "hi", "session": {"not": "valid"}},
    )
    resp = api_client.post("/advisor/sessions", json={"user_id": "u1"})
    assert resp.status_code == 500
    assert resp.json()["detail"]


def test_send_advisor_message_500_on_malformed_advisory_result(api_client, monkeypatch) -> None:
    from investment_team.api import main as api_main

    start = api_client.post("/advisor/sessions", json={"user_id": "u1"})
    sid = start.json()["session_id"]

    monkeypatch.setattr(
        api_main, "_execute_advisory", lambda op, payload, *, key: {"advisor_message": "hi"}
    )
    resp = api_client.post(f"/advisor/sessions/{sid}/messages", json={"message": "hello"})
    assert resp.status_code == 500
    assert resp.json()["detail"]


def test_send_advisor_message_404_when_session_removed_concurrently(
    api_client, monkeypatch
) -> None:
    """If the session disappears between the initial existence check and the
    re-check immediately before dispatch, the route must 404 instead of
    dispatching a workflow call against a session that no longer exists."""
    from investment_team.api import main as api_main

    start = api_client.post("/advisor/sessions", json={"user_id": "u1"})
    sid = start.json()["session_id"]

    class _DeleteOnFirstGet:
        """Mimics another request deleting the session right after this
        route's first `.get()` read, so the pre-dispatch `in` re-check
        under `_lock` must observe it as gone."""

        def __init__(self, initial: Dict[str, Any]) -> None:
            self._d = dict(initial)

        def get(self, key: str, default: Any = None) -> Any:
            value = self._d.get(key, default)
            self._d.pop(key, None)
            return value

        def __contains__(self, key: str) -> bool:
            return key in self._d

    monkeypatch.setattr(
        api_main,
        "_advisor_sessions",
        _DeleteOnFirstGet({sid: api_main._advisor_sessions.get(sid)}),
    )
    dispatched: List[bool] = []
    monkeypatch.setattr(
        api_main,
        "_execute_advisory",
        lambda op, payload, *, key: dispatched.append(True) or {},
    )

    resp = api_client.post(f"/advisor/sessions/{sid}/messages", json={"message": "hello"})
    assert resp.status_code == 404
    assert dispatched == []


def test_complete_advisor_session_500_on_malformed_advisory_result(api_client, monkeypatch) -> None:
    from investment_team.api import main as api_main

    start = api_client.post("/advisor/sessions", json={"user_id": "u1"})
    sid = start.json()["session_id"]

    # Bypass the "missing required fields" 400 branch so we reach the guard under test.
    monkeypatch.setattr(api_main._get_advisor_agent(), "missing_fields", lambda collected: [])
    monkeypatch.setattr(
        api_main, "_execute_advisory", lambda op, payload, *, key: {"user_id": "u1"}
    )
    resp = api_client.post(f"/advisor/sessions/{sid}/complete")
    assert resp.status_code == 500
    assert resp.json()["detail"]


def test_complete_advisor_session_malformed_ips_payload_returns_500(
    api_client, monkeypatch
) -> None:
    """A present but schema-invalid 'ips' triggers a clean 500 via
    ValidationError, not an unhandled exception."""
    from investment_team.api import main as api_main

    start = api_client.post("/advisor/sessions", json={"user_id": "u1"})
    sid = start.json()["session_id"]

    monkeypatch.setattr(api_main._get_advisor_agent(), "missing_fields", lambda collected: [])
    monkeypatch.setattr(
        api_main,
        "_execute_advisory",
        lambda op, payload, *, key: {"user_id": "u1", "ips": {"not": "valid"}},
    )
    resp = api_client.post(f"/advisor/sessions/{sid}/complete")
    assert resp.status_code == 500
    assert resp.json()["detail"]
