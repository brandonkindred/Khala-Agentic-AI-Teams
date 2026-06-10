"""Tests for the invoke-boundary gate (prepare/finalize lifecycle + in-process helper).

Pure tests: every storage/context seam (`claim_run`, `complete_run`,
`persist_writeback`, `ensure_rollups_current`, `load_context`,
`is_postgres_enabled`) is monkeypatched on the ``invoke_gate`` module
namespace, so these run everywhere without Postgres. Rule enforcement is
exercised through the real predicate evaluators with real ``Rule`` models.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel as PydanticBaseModel

from agent_cognition import invoke_gate as gate
from agent_cognition.memory.rollup import RollupReport
from agent_cognition.memory.store import AgentCognitionStorageUnavailable
from agent_cognition.models import (
    CognitionContext,
    CognitionWriteback,
    EventKind,
    MemoryEvent,
    Rule,
    RuleMode,
    RuleSource,
    RuleStatus,
)
from agent_cognition.testing import FakeRunLedger
from agent_cognition.tools.envelope import try_unwrap_request

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
_AGENT = "team.agent"


def _run(coro):
    return asyncio.run(coro)


def _rule(predicate: dict, *, mode: RuleMode = RuleMode.ENFORCED) -> Rule:
    return Rule(
        id=f"r-{uuid4()}",
        agent_id=_AGENT,
        text="gate rule",
        mode=mode,
        status=RuleStatus.ACTIVE,
        predicate=predicate,
        source=RuleSource.OPERATOR,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _pre_rule_blocking_everything() -> Rule:
    return _rule(
        {"phase": "precondition", "check": {"op": "==", "path": "input.q", "value": "never"}}
    )


def _post_rule_requiring_ok() -> Rule:
    return _rule(
        {"phase": "postcondition", "check": {"op": "==", "path": "output.ok", "value": True}}
    )


def _event(seq: int = 0, **overrides: Any) -> MemoryEvent:
    fields: dict[str, Any] = {
        "id": str(uuid4()),
        "agent_id": _AGENT,
        "kind": EventKind.OBSERVATION,
        "content": "saw a thing",
        "occurred_at": _NOW,
        "source_run_id": "run-1",
        "source_seq": seq,
    }
    fields.update(overrides)
    return MemoryEvent(**fields)


@pytest.fixture()
def seams(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Patch every storage/context seam; return the recorders + fake ledger."""
    ledger = FakeRunLedger()
    s = SimpleNamespace(
        ledger=ledger,
        persisted=[],  # (agent_id, event_run_id, CognitionWriteback)
        rollups=[],  # (agent_id, max_periods)
        context=CognitionContext(rules=[], memory_digest="digest"),
        rollup_report=RollupReport(agent_id=_AGENT),
    )

    def _persist(agent_id, source_run_id, writeback):
        s.persisted.append((agent_id, source_run_id, writeback))
        return len(writeback.events)

    async def _load(agent_id, *, query=""):
        if isinstance(s.context, Exception):
            raise s.context
        return s.context

    def _rollups(agent_id, now, *, max_periods=None):
        s.rollups.append((agent_id, max_periods))
        return s.rollup_report

    monkeypatch.setattr(gate, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(gate, "claim_run", ledger.claim_run)
    monkeypatch.setattr(gate, "complete_run", ledger.complete_run)
    monkeypatch.setattr(gate, "abandon_run", ledger.abandon_run)
    monkeypatch.setattr(gate, "persist_writeback", _persist)
    monkeypatch.setattr(gate, "ensure_rollups_current", _rollups)
    monkeypatch.setattr(gate, "load_context", _load)
    return s


# ---------------------------------------------------------------------------
# derive_source_run_id
# ---------------------------------------------------------------------------
def test_derive_hash_is_key_order_invariant() -> None:
    a, hash_a = gate.derive_source_run_id({"b": 1, "a": "x"})
    b, hash_b = gate.derive_source_run_id({"a": "x", "b": 1})
    assert hash_a == hash_b
    assert a == hash_a and b == hash_b  # keyless: the hash IS the run id


def test_derive_different_body_different_hash() -> None:
    _, h1 = gate.derive_source_run_id({"a": 1})
    _, h2 = gate.derive_source_run_id({"a": 2})
    assert h1 != h2


def test_derive_caller_key_wins_and_blank_key_falls_back() -> None:
    srid, h = gate.derive_source_run_id({"a": 1}, "  key-7  ")
    assert srid == "key-7" and h != "key-7"
    srid, h = gate.derive_source_run_id({"a": 1}, "   ")
    assert srid == h


# ---------------------------------------------------------------------------
# prepare_invoke — idempotency / ledger
# ---------------------------------------------------------------------------
def test_prepare_requires_key_rejects_keyless(seams: SimpleNamespace) -> None:
    out = _run(gate.prepare_invoke(_AGENT, {"q": 1}, requires_idempotency_key=True))
    assert out.kind is gate.GateOutcomeKind.MISSING_IDEMPOTENCY_KEY
    assert seams.ledger.claim_calls == 0  # rejected before any ledger row


def test_prepare_requires_key_with_key_proceeds(seams: SimpleNamespace) -> None:
    out = _run(
        gate.prepare_invoke(_AGENT, {"q": 1}, requires_idempotency_key=True, idempotency_key="k1")
    )
    assert out.kind is gate.GateOutcomeKind.PROCEED
    assert out.prepared.source_run_id == "k1"
    assert out.prepared.claim_token is not None


def test_prepare_first_sight_claims_and_wraps(seams: SimpleNamespace) -> None:
    out = _run(gate.prepare_invoke(_AGENT, {"q": 1}))
    assert out.kind is gate.GateOutcomeKind.PROCEED
    prepared = out.prepared
    assert prepared.claim_token == "tok-1"
    assert prepared.attempt_token == "tok-1"  # ledgered: attempt = claim
    assert prepared.event_run_id == f"{prepared.source_run_id}#tok-1"
    # Lazy catch-up ran under the default per-invoke period budget.
    assert seams.rollups == [(_AGENT, gate._DEFAULT_INVOKE_ROLLUP_BUDGET)]
    unwrapped = try_unwrap_request(prepared.envelope)
    assert unwrapped.input == {"q": 1}  # caller body verbatim inside the envelope
    assert unwrapped.cognition["memory_digest"] == "digest"


def test_prepare_rollup_budget_env_override_and_truncation_tolerated(
    seams: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_COGNITION_INVOKE_ROLLUP_BUDGET", "3")
    seams.rollup_report = RollupReport(agent_id=_AGENT, truncated=True)
    out = _run(gate.prepare_invoke(_AGENT, {"q": 1}))
    assert seams.rollups == [(_AGENT, 3)]
    assert out.kind is gate.GateOutcomeKind.PROCEED  # a truncated pass never blocks


def test_prepare_unledgered_attempts_get_distinct_event_run_ids(
    seams: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a ledger every invoke is its own attempt — two identical calls
    must never share an event keyspace (their events would silently dedup)."""
    monkeypatch.setattr(gate, "is_postgres_enabled", lambda: False)
    first = _run(gate.prepare_invoke(_AGENT, {"q": 1}))
    second = _run(gate.prepare_invoke(_AGENT, {"q": 1}))
    assert first.prepared.event_run_id != second.prepared.event_run_id


def test_prepare_replays_terminal_row(seams: SimpleNamespace) -> None:
    srid, h = gate.derive_source_run_id({"q": 1})
    seams.ledger.rows[(_AGENT, srid)] = {
        "status": "completed",
        "hash": h,
        "token": "t",
        "response": {"status_code": 200, "content": {"output": "stored"}},
        "lease_valid": False,
    }
    out = _run(gate.prepare_invoke(_AGENT, {"q": 1}))
    assert out.kind is gate.GateOutcomeKind.REPLAY
    assert out.status_code == 200
    assert out.content == {"output": "stored"}


def test_prepare_replays_blocked_row_as_4xx(seams: SimpleNamespace) -> None:
    seams.ledger.rows[(_AGENT, "k1")] = {
        "status": "blocked",
        "hash": gate.derive_source_run_id({"q": 1})[1],
        "token": "t",
        "response": {"status_code": 422, "content": {"detail": {"phase": "precondition"}}},
        "lease_valid": False,
    }
    out = _run(gate.prepare_invoke(_AGENT, {"q": 1}, idempotency_key="k1"))
    assert out.kind is gate.GateOutcomeKind.REPLAY
    assert out.status_code == 422


def test_prepare_malformed_stored_envelope_degrades_to_conflict(seams: SimpleNamespace) -> None:
    seams.ledger.rows[(_AGENT, "k1")] = {
        "status": "completed",
        "hash": gate.derive_source_run_id({"q": 1})[1],
        "token": "t",
        "response": None,  # terminal but unreadable — must not forge a 200
        "lease_valid": False,
    }
    out = _run(gate.prepare_invoke(_AGENT, {"q": 1}, idempotency_key="k1"))
    assert out.kind is gate.GateOutcomeKind.CONFLICT


def test_prepare_same_key_different_body_conflicts(seams: SimpleNamespace) -> None:
    _run(gate.prepare_invoke(_AGENT, {"q": 1}, idempotency_key="k1"))
    out = _run(gate.prepare_invoke(_AGENT, {"q": 2}, idempotency_key="k1"))
    assert out.kind is gate.GateOutcomeKind.CONFLICT


def test_prepare_in_flight_lease_conflicts_and_expired_reclaims(seams: SimpleNamespace) -> None:
    first = _run(gate.prepare_invoke(_AGENT, {"q": 1}, idempotency_key="k1"))
    assert first.kind is gate.GateOutcomeKind.PROCEED
    # Concurrent retry while the lease is valid → conflict.
    out = _run(gate.prepare_invoke(_AGENT, {"q": 1}, idempotency_key="k1"))
    assert out.kind is gate.GateOutcomeKind.CONFLICT
    # Expired lease → reclaimed in place and re-executed.
    seams.ledger.rows[(_AGENT, "k1")]["lease_valid"] = False
    out = _run(gate.prepare_invoke(_AGENT, {"q": 1}, idempotency_key="k1"))
    assert out.kind is gate.GateOutcomeKind.PROCEED
    assert out.prepared.claim_token != first.prepared.claim_token  # ownership rotated


def test_prepare_storage_outage_fails_closed_only_for_side_effecting(
    seams: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a, **k):
        raise AgentCognitionStorageUnavailable("pg down")

    monkeypatch.setattr(gate, "claim_run", _boom)
    out = _run(
        gate.prepare_invoke(_AGENT, {"q": 1}, requires_idempotency_key=True, idempotency_key="k1")
    )
    assert out.kind is gate.GateOutcomeKind.LEDGER_UNAVAILABLE
    out = _run(gate.prepare_invoke(_AGENT, {"q": 1}))
    assert out.kind is gate.GateOutcomeKind.PROCEED  # degraded, unledgered
    assert out.prepared.claim_token is None


def test_prepare_postgres_off_is_unledgered_passthrough(
    seams: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "is_postgres_enabled", lambda: False)
    seams.context = RuntimeError("no store to load from")
    out = _run(gate.prepare_invoke(_AGENT, {"q": 1}))
    assert out.kind is gate.GateOutcomeKind.PROCEED
    assert out.prepared.claim_token is None
    assert out.prepared.context is None
    assert out.prepared.envelope == {"q": 1}  # no context → body posted unwrapped


def test_prepare_postgres_off_side_effecting_is_unavailable(
    seams: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "is_postgres_enabled", lambda: False)
    out = _run(
        gate.prepare_invoke(_AGENT, {"q": 1}, requires_idempotency_key=True, idempotency_key="k1")
    )
    assert out.kind is gate.GateOutcomeKind.LEDGER_UNAVAILABLE


def test_prepare_tolerates_rollup_and_context_failures(
    seams: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _rollup_boom(agent_id, now, *, max_periods=None):
        raise RuntimeError("rollup hiccup")

    monkeypatch.setattr(gate, "ensure_rollups_current", _rollup_boom)
    seams.context = RuntimeError("context hiccup")
    out = _run(gate.prepare_invoke(_AGENT, {"q": 1}))
    assert out.kind is gate.GateOutcomeKind.PROCEED  # cognition hiccups never break the invoke
    assert out.prepared.context is None


# ---------------------------------------------------------------------------
# prepare_invoke — precondition gate + envelope cap
# ---------------------------------------------------------------------------
def test_prepare_precondition_block_is_durable(seams: SimpleNamespace) -> None:
    seams.context = CognitionContext(rules=[_pre_rule_blocking_everything()], memory_digest="")
    out = _run(gate.prepare_invoke(_AGENT, {"q": 1}, idempotency_key="k1"))
    assert out.kind is gate.GateOutcomeKind.BLOCKED
    assert out.status_code == 422
    assert out.content["detail"]["phase"] == "precondition"

    # One ERROR memory event persisted, under the attempt-scoped run id…
    ((agent_id, srid, writeback),) = seams.persisted
    assert agent_id == _AGENT
    assert srid == "k1#tok-1"
    assert [e.kind for e in writeback.events] == [EventKind.ERROR]
    assert writeback.events[0].data["phase"] == "precondition"
    assert out.reason  # structured cause carried on the outcome

    # …and the 4xx envelope stored as blocked, so a retry replays it verbatim.
    row = seams.ledger.rows[(_AGENT, "k1")]
    assert row["status"] == "blocked"
    assert row["response"]["status_code"] == 422
    replay = _run(gate.prepare_invoke(_AGENT, {"q": 1}, idempotency_key="k1"))
    assert replay.kind is gate.GateOutcomeKind.REPLAY
    assert replay.status_code == 422
    assert replay.content == out.content


def test_prepare_precondition_block_unledgered_still_records_event(
    seams: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a, **k):
        raise AgentCognitionStorageUnavailable("pg down")

    monkeypatch.setattr(gate, "claim_run", _boom)
    seams.context = CognitionContext(rules=[_pre_rule_blocking_everything()], memory_digest="")
    out = _run(gate.prepare_invoke(_AGENT, {"q": 1}))
    assert out.kind is gate.GateOutcomeKind.BLOCKED
    assert len(seams.persisted) == 1  # event still attempted
    assert seams.ledger.rows == {}  # no claim → nothing completed


def test_prepare_block_response_survives_persistence_failure(
    seams: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _persist_boom(*a, **k):
        raise AgentCognitionStorageUnavailable("pg down mid-flight")

    monkeypatch.setattr(gate, "persist_writeback", _persist_boom)
    seams.context = CognitionContext(rules=[_pre_rule_blocking_everything()], memory_digest="")
    out = _run(gate.prepare_invoke(_AGENT, {"q": 1}))
    assert out.kind is gate.GateOutcomeKind.BLOCKED  # the 4xx is never masked


def test_prepare_envelope_cap_applies_to_wrapped_envelope_and_abandons(
    seams: SimpleNamespace,
) -> None:
    seams.context = CognitionContext(rules=[], memory_digest="D" * 512)
    body = {"q": "x" * 256}
    # The body alone fits the cap, but body + digest does not.
    out = _run(gate.prepare_invoke(_AGENT, body, max_envelope_bytes=600))
    assert out.kind is gate.GateOutcomeKind.ENVELOPE_TOO_LARGE
    assert "envelope cap" in out.reason
    # The agent provably never ran, so the claim was abandoned — an IMMEDIATE
    # retry under a generous cap re-executes instead of 409-ing until lease
    # expiry.
    assert seams.ledger.rows == {}
    out = _run(gate.prepare_invoke(_AGENT, body, max_envelope_bytes=100_000))
    assert out.kind is gate.GateOutcomeKind.PROCEED


# ---------------------------------------------------------------------------
# finalize_invoke
# ---------------------------------------------------------------------------
def _prepared(seams: SimpleNamespace, body: dict | None = None, **kwargs) -> gate.PreparedInvoke:
    out = _run(gate.prepare_invoke(_AGENT, body or {"q": 1}, **kwargs))
    assert out.kind is gate.GateOutcomeKind.PROCEED
    return out.prepared


def test_finalize_success_persists_writeback_and_completes(seams: SimpleNamespace) -> None:
    prepared = _prepared(seams, idempotency_key="k1")
    envelope = {
        "output": {"ok": True},
        "memory_events": [
            _event(0).model_dump(mode="json"),
            {"garbage": "not an event"},  # malformed → dropped, never fails the call
            _event(1).model_dump(mode="json"),
        ],
        "tool_audit": [],
    }
    fin = _run(gate.finalize_invoke(prepared, 200, envelope))
    assert not fin.blocked
    assert fin.status_code == 200
    assert fin.persisted_events == 2
    ((_, _, writeback),) = seams.persisted
    assert [e.source_seq for e in writeback.events] == [0, 1]
    row = seams.ledger.rows[(_AGENT, "k1")]
    assert row["status"] == "completed"
    assert row["response"] == {"status_code": 200, "content": envelope}


def test_finalize_completed_row_replays_without_reinvoking(seams: SimpleNamespace) -> None:
    prepared = _prepared(seams, idempotency_key="k1")
    _run(gate.finalize_invoke(prepared, 200, {"output": {"ok": True}}))
    out = _run(gate.prepare_invoke(_AGENT, {"q": 1}, idempotency_key="k1"))
    assert out.kind is gate.GateOutcomeKind.REPLAY
    assert out.content == {"output": {"ok": True}}


def test_finalize_postcondition_violation_drops_output_keeps_audit(seams: SimpleNamespace) -> None:
    seams.context = CognitionContext(rules=[_post_rule_requiring_ok()], memory_digest="")
    prepared = _prepared(seams, idempotency_key="k1")
    envelope = {
        "output": {"ok": False, "secret_result": "must not persist"},
        "memory_events": [_event(0, content="agent-authored memory").model_dump(mode="json")],
        "tool_audit": [{"tool_id": "git", "function": "git_push", "ok": True}],
    }
    fin = _run(gate.finalize_invoke(prepared, 200, envelope))
    assert fin.blocked
    assert fin.status_code == 422
    assert fin.content["detail"]["phase"] == "postcondition"

    # Persisted: the trusted audit + the block event — NOT the agent's memory.
    ((_, _, writeback),) = seams.persisted
    kinds = [e.kind for e in writeback.events]
    assert kinds == [EventKind.TOOL_CALL, EventKind.ERROR]
    assert writeback.events[0].data["tool_id"] == "git"
    assert all("agent-authored" not in e.content for e in writeback.events)

    # Ledger: blocked with the 4xx envelope (a retried violation replays it).
    row = seams.ledger.rows[(_AGENT, "k1")]
    assert row["status"] == "blocked"
    assert "must not persist" not in str(row["response"])
    replay = _run(gate.prepare_invoke(_AGENT, {"q": 1}, idempotency_key="k1"))
    assert replay.kind is gate.GateOutcomeKind.REPLAY
    assert replay.status_code == 422


def test_finalize_non_2xx_keeps_audit_and_leaves_lease(seams: SimpleNamespace) -> None:
    prepared = _prepared(seams, idempotency_key="k1")
    envelope = {  # shim error envelopes ride inside `detail`
        "detail": {
            "output": None,
            "error": "AgentExecutionTimeout: exceeded 60.0s",
            "tool_audit": [{"tool_id": "web_search", "ok": True}],
            "memory_events": [],
        }
    }
    fin = _run(gate.finalize_invoke(prepared, 504, envelope))
    assert fin.status_code == 504 and not fin.blocked
    ((_, _, writeback),) = seams.persisted
    assert [e.kind for e in writeback.events] == [EventKind.TOOL_CALL]
    # Not a terminal state: the lease is left to expire so a retry re-executes.
    assert seams.ledger.rows[(_AGENT, "k1")]["status"] == "in_progress"


def test_finalize_unledgered_run_skips_completion(seams: SimpleNamespace, monkeypatch) -> None:
    monkeypatch.setattr(gate, "is_postgres_enabled", lambda: False)
    prepared = _prepared(seams)
    fin = _run(
        gate.finalize_invoke(
            prepared,
            200,
            {"output": {"ok": True}, "memory_events": [_event(0).model_dump(mode="json")]},
        )
    )
    assert fin.persisted_events == 1  # writeback still persisted (best-effort)
    assert seams.ledger.rows == {}


def test_finalize_completion_failure_never_masks_response(
    seams: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(seams, idempotency_key="k1")

    def _boom(*a, **k):
        raise AgentCognitionStorageUnavailable("pg down mid-flight")

    monkeypatch.setattr(gate, "complete_run", _boom)
    fin = _run(gate.finalize_invoke(prepared, 200, {"output": {"ok": True}}))
    assert fin.status_code == 200 and not fin.blocked


def test_finalize_malformed_audit_entry_is_dropped(seams: SimpleNamespace) -> None:
    seams.context = CognitionContext(rules=[_post_rule_requiring_ok()], memory_digest="")
    prepared = _prepared(seams, idempotency_key="k1")
    envelope = {
        "output": {"ok": False},
        "tool_audit": [{"no_tool_id": True}, {"tool_id": "git", "ok": False, "error": "denied"}],
    }
    fin = _run(gate.finalize_invoke(prepared, 200, envelope))
    assert fin.blocked
    ((_, _, writeback),) = seams.persisted
    assert [e.kind for e in writeback.events] == [EventKind.TOOL_CALL, EventKind.ERROR]
    assert "failed" in writeback.events[0].content


# ---------------------------------------------------------------------------
# invoke_in_process
# ---------------------------------------------------------------------------
def test_in_process_full_lifecycle_and_replay(seams: SimpleNamespace) -> None:
    calls: list[Any] = []

    def runner(body, ctx):
        calls.append(body)
        assert ctx is not None and ctx.memory_digest == "digest"
        return {"ok": True}, CognitionWriteback(events=[_event(0)])

    fin = _run(gate.invoke_in_process(_AGENT, {"q": 1}, runner, idempotency_key="k1"))
    assert fin.status_code == 200
    assert fin.content["output"] == {"ok": True}
    assert fin.persisted_events == 1

    # Same key + body: replayed from the ledger — the runner is NOT called again.
    fin2 = _run(gate.invoke_in_process(_AGENT, {"q": 1}, runner, idempotency_key="k1"))
    assert fin2.status_code == 200
    assert fin2.content == {"status_code": 200, "content": fin.content}["content"]
    assert len(calls) == 1


def test_in_process_async_runner_and_bare_output(seams: SimpleNamespace) -> None:
    async def runner(body, ctx):
        return {"answer": 42}

    fin = _run(gate.invoke_in_process(_AGENT, {"q": 1}, runner))
    assert fin.status_code == 200
    assert fin.content["output"] == {"answer": 42}


def test_in_process_maps_gate_outcomes_to_statuses(seams: SimpleNamespace) -> None:
    def runner(body, ctx):  # pragma: no cover — must not run
        raise AssertionError("runner must not run on a non-PROCEED outcome")

    fin = _run(gate.invoke_in_process(_AGENT, {"q": 1}, runner, requires_idempotency_key=True))
    assert fin.status_code == 400
    _run(gate.invoke_in_process(_AGENT, {"q": 1}, lambda b, c: {"ok": 1}, idempotency_key="k1"))
    fin = _run(gate.invoke_in_process(_AGENT, {"q": 2}, runner, idempotency_key="k1"))
    assert fin.status_code == 409


def test_in_process_precondition_block(seams: SimpleNamespace) -> None:
    seams.context = CognitionContext(rules=[_pre_rule_blocking_everything()], memory_digest="")

    def runner(body, ctx):  # pragma: no cover — must not run
        raise AssertionError("runner must not run when the precondition blocks")

    fin = _run(gate.invoke_in_process(_AGENT, {"q": 1}, runner))
    assert fin.blocked and fin.status_code == 422


def test_in_process_postcondition_block(seams: SimpleNamespace) -> None:
    seams.context = CognitionContext(rules=[_post_rule_requiring_ok()], memory_digest="")
    fin = _run(gate.invoke_in_process(_AGENT, {"q": 1}, lambda b, c: {"ok": False}))
    assert fin.blocked and fin.status_code == 422


def test_in_process_runner_exception_propagates_and_lease_holds(seams: SimpleNamespace) -> None:
    def runner(body, ctx):
        raise ValueError("team bug")

    with pytest.raises(ValueError, match="team bug"):
        _run(gate.invoke_in_process(_AGENT, {"q": 1}, runner, idempotency_key="k1"))
    # The claim is left in flight: a concurrent retry conflicts until the lease expires.
    assert seams.ledger.rows[(_AGENT, "k1")]["status"] == "in_progress"


def test_in_process_jsonable_output(seams: SimpleNamespace) -> None:
    fin = _run(gate.invoke_in_process(_AGENT, {"q": 1}, lambda b, c: {"at": _NOW}))
    assert fin.content["output"] == {"at": str(_NOW)}  # datetime stringified, not crashed

    class Nested(PydanticBaseModel):
        n: int

    # A model nested INSIDE a plain container is also dumped, not stringified.
    fin = _run(gate.invoke_in_process(_AGENT, {"q": 2}, lambda b, c: {"inner": Nested(n=7)}))
    assert fin.content["output"] == {"inner": {"n": 7}}


def test_in_process_pydantic_output_stays_structured_and_gated(seams: SimpleNamespace) -> None:
    """A runner returning a Pydantic model must yield a structured ``output``
    dict — NOT a stringified repr — so enforced postconditions still apply."""

    class TeamResult(PydanticBaseModel):
        ok: bool
        detail: str = "fine"

    seams.context = CognitionContext(rules=[_post_rule_requiring_ok()], memory_digest="")
    fin = _run(gate.invoke_in_process(_AGENT, {"q": 1}, lambda b, c: TeamResult(ok=True)))
    assert not fin.blocked
    assert fin.content["output"] == {"ok": True, "detail": "fine"}  # dict, not a string

    # And a violating model output is caught by the gate, not silently skipped.
    seams.ledger.rows.clear()
    seams.persisted.clear()
    fin = _run(gate.invoke_in_process(_AGENT, {"q": 2}, lambda b, c: TeamResult(ok=False)))
    assert fin.blocked and fin.status_code == 422


def test_finalize_fails_closed_on_missing_or_non_object_output(seams: SimpleNamespace) -> None:
    """An agent carrying enforced postcondition rules whose 2xx response has no
    mapping ``output`` is BLOCKED (rules evaluate against {}), never skipped."""
    seams.context = CognitionContext(rules=[_post_rule_requiring_ok()], memory_digest="")
    for envelope in ({"raw": "not-json"}, {"output": "plain string"}, {"output": None}):
        seams.ledger.rows.clear()
        prepared = _prepared(seams, body={"q": str(envelope)}, idempotency_key=str(envelope))
        fin = _run(gate.finalize_invoke(prepared, 200, envelope))
        assert fin.blocked, f"envelope {envelope!r} must fail closed"
        assert fin.block_phase == "postcondition"
    # Without enforced postcondition rules the same envelopes pass untouched.
    seams.context = CognitionContext(rules=[], memory_digest="")
    seams.ledger.rows.clear()
    prepared = _prepared(seams, idempotency_key="k-norules")
    fin = _run(gate.finalize_invoke(prepared, 200, {"output": "plain string"}))
    assert not fin.blocked


def test_retry_attempt_events_do_not_collide_with_failed_attempt(
    seams: SimpleNamespace,
) -> None:
    """An expired-lease re-execution persists its events under a DIFFERENT
    attempt-scoped run id, so the retry's real side effects can never be
    silently dropped against the failed attempt's rows by the
    (agent_id, run_id, seq) ON CONFLICT dedup."""
    first = _prepared(seams, idempotency_key="k1")
    fail_envelope = {
        "detail": {"tool_audit": [{"tool_id": "git", "ok": True}], "memory_events": []}
    }
    _run(gate.finalize_invoke(first, 504, fail_envelope))

    seams.ledger.rows[(_AGENT, "k1")]["lease_valid"] = False  # lease expires
    second = _prepared(seams, idempotency_key="k1")
    assert second.event_run_id != first.event_run_id
    _run(gate.finalize_invoke(second, 504, fail_envelope))

    (a_run, b_run) = (p[1] for p in seams.persisted)
    assert a_run != b_run  # disjoint keyspaces — same seq values cannot collide
    assert {e.source_seq for p in seams.persisted for e in p[2].events} == {0}


def test_finalize_blocked_outcome_carries_structured_cause(seams: SimpleNamespace) -> None:
    seams.context = CognitionContext(rules=[_post_rule_requiring_ok()], memory_digest="")
    prepared = _prepared(seams, idempotency_key="k1")
    fin = _run(gate.finalize_invoke(prepared, 200, {"output": {"ok": False}}))
    assert fin.blocked
    assert fin.block_phase == "postcondition"
    assert fin.block_reason  # callers read these, not the content shape


def test_outcome_as_finalized_uses_shared_status_map(seams: SimpleNamespace) -> None:
    """The in-process status mapping IS the exported map the HTTP route uses —
    a drift between the two transports is structurally impossible."""
    for kind, status in gate.GATE_ERROR_HTTP_STATUS.items():
        fin = gate._outcome_as_finalized(gate.GateOutcome(kind=kind, reason="x"))
        assert fin.status_code == status
    blocked = gate._outcome_as_finalized(
        gate.GateOutcome(kind=gate.GateOutcomeKind.BLOCKED, status_code=422, reason="r")
    )
    assert blocked.blocked and blocked.block_phase == "precondition" and blocked.block_reason == "r"


def test_abandon_invoke_paths(seams: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    # Unledgered: nothing to abandon.
    monkeypatch.setattr(gate, "is_postgres_enabled", lambda: False)
    unledgered = _prepared(seams)
    assert _run(gate.abandon_invoke(unledgered)) is False
    monkeypatch.setattr(gate, "is_postgres_enabled", lambda: True)

    # Ledgered: the owned in-progress row is released; a second abandon (or one
    # against a terminal/reclaimed row) is a no-op False.
    prepared = _prepared(seams, idempotency_key="k1")
    assert _run(gate.abandon_invoke(prepared)) is True
    assert seams.ledger.rows == {}
    assert _run(gate.abandon_invoke(prepared)) is False

    # Storage failure: best-effort False, never raises.
    def _boom(*a, **k):
        raise AgentCognitionStorageUnavailable("pg down")

    prepared = _prepared(seams, idempotency_key="k2")
    monkeypatch.setattr(gate, "abandon_run", _boom)
    assert _run(gate.abandon_invoke(prepared)) is False


# ---------------------------------------------------------------------------
# Preconditions (DbC asserts)
# ---------------------------------------------------------------------------
def test_prepare_asserts_agent_id_and_cap() -> None:
    with pytest.raises(AssertionError):
        _run(gate.prepare_invoke("", {"q": 1}))
    with pytest.raises(AssertionError):
        _run(gate.prepare_invoke(_AGENT, {"q": 1}, max_envelope_bytes=0))


def test_in_process_asserts_runner_callable() -> None:
    with pytest.raises(AssertionError):
        _run(gate.invoke_in_process(_AGENT, {"q": 1}, runner=None))  # type: ignore[arg-type]
