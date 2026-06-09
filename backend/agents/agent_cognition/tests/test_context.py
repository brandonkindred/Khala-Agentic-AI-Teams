"""Tests for the CognitiveContext facade (Step 8).

Two halves:

* **Pure** tests (no Postgres) cover the enforced rule-gate hooks, salience
  clamping, defensive writeback sanitization (with ``append_event``
  monkeypatched), the ``load_context`` / ``ensure_rollups_current`` delegations,
  result types, and precondition asserts. They run everywhere.
* **Live-Postgres** tests (decorated with ``_PG``, skipped when ``POSTGRES_HOST``
  is unset — matching ``test_store.py``) cover the run-ledger state machine and
  the writeback round-trip. The autouse fixture only registers/truncates when
  Postgres is configured, so the pure tests stay runnable without a DB.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from agent_cognition import context
from agent_cognition.context import (
    ClaimState,
    PostconditionViolation,
    PreconditionBlocked,
)
from agent_cognition.models import (
    CognitionContext,
    CognitionWriteback,
    EventKind,
    MemoryEvent,
    Rule,
    RuleMode,
    RuleSource,
    RuleStatus,
    ToolCall,
)
from agent_cognition.postgres import SCHEMA
from agent_cognition.redaction import sanitize_for_memory
from shared_postgres import is_postgres_enabled, register_team_schemas
from shared_postgres.testing import truncate_team_tables

_PG = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="POSTGRES_HOST not set; skipping live-Postgres ledger/writeback tests",
)

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
_DAY = (datetime(2026, 6, 1, tzinfo=timezone.utc), datetime(2026, 6, 2, tzinfo=timezone.utc))


@pytest.fixture(autouse=True)
def _provision_schema() -> None:
    """Register + truncate the cognition tables, but only with a live Postgres.

    A no-op without ``POSTGRES_HOST`` so the pure tests in this module still run
    (``truncate_team_tables`` would otherwise raise when Postgres is unset).
    """
    if is_postgres_enabled():
        register_team_schemas(SCHEMA)
        truncate_team_tables(SCHEMA)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _enforced_precondition_rule(*, blocks_when_present: str) -> Rule:
    """An enforced precondition rule that blocks when ``input.<key>`` exists.

    ``not(exists(input.<key>))`` allows when the key is absent and blocks when
    present (``exists`` returns ALLOW when present, BLOCK when absent; ``not``
    inverts).
    """
    return Rule(
        id="r-pre",
        agent_id="a",
        text=f"input must not contain {blocks_when_present}",
        mode=RuleMode.ENFORCED,
        status=RuleStatus.ACTIVE,
        predicate={
            "phase": "precondition",
            "check": {
                "op": "not",
                "of": [{"op": "exists", "path": f"input.{blocks_when_present}"}],
            },
        },
        source=RuleSource.OPERATOR,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _enforced_postcondition_rule(*, forbid_key: str) -> Rule:
    """An enforced postcondition rule that blocks when ``output.<key>`` exists."""
    return Rule(
        id="r-post",
        agent_id="a",
        text=f"output must not contain {forbid_key}",
        mode=RuleMode.ENFORCED,
        status=RuleStatus.ACTIVE,
        predicate={
            "phase": "postcondition",
            "check": {"op": "not", "of": [{"op": "exists", "path": f"output.{forbid_key}"}]},
        },
        source=RuleSource.OPERATOR,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _event(
    *,
    seq: int = 0,
    salience: float = 0.3,
    data: dict | None = None,
    agent_id: str = "a",
    run_id: str = "run-1",
    kind: EventKind = EventKind.OUTCOME,
    content: str = "did a thing",
) -> MemoryEvent:
    return MemoryEvent(
        id=str(uuid4()),
        agent_id=agent_id,
        kind=kind,
        content=content,
        data=data or {},
        salience=salience,
        occurred_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        source_run_id=run_id,
        source_seq=seq,
    )


# ===========================================================================
# Pure tests — no Postgres
# ===========================================================================
def test_enforce_precondition_allows_when_no_rule_blocks() -> None:
    rule = _enforced_precondition_rule(blocks_when_present="danger")
    # No "danger" key -> allowed -> returns None.
    assert context.enforce_precondition("a", {"safe": 1}, [rule]) is None


def test_enforce_precondition_raises_on_block() -> None:
    rule = _enforced_precondition_rule(blocks_when_present="danger")
    with pytest.raises(PreconditionBlocked) as exc:
        context.enforce_precondition("a", {"danger": True}, [rule])
    assert exc.value.phase == "precondition"
    assert "input must not contain danger" in exc.value.reason


def test_enforce_precondition_requires_agent_id() -> None:
    with pytest.raises(AssertionError):
        context.enforce_precondition("", {"x": 1}, [])


def test_enforce_postcondition_allows_and_blocks() -> None:
    rule = _enforced_postcondition_rule(forbid_key="leak")
    assert context.enforce_postcondition({"ok": True}, [rule]) is None
    with pytest.raises(PostconditionViolation) as exc:
        context.enforce_postcondition({"leak": "secret"}, [rule])
    assert exc.value.phase == "postcondition"
    assert "output must not contain leak" in exc.value.reason


def test_clamp01_bounds() -> None:
    assert context._clamp01(-3.0) == 0.0
    assert context._clamp01(42.0) == 1.0
    assert context._clamp01(0.5) == 0.5


def test_persist_writeback_sanitizes_clamps_and_pins(monkeypatch) -> None:
    captured: list[MemoryEvent] = []
    monkeypatch.setattr(context, "append_event", lambda agent_id, ev: captured.append(ev))

    wb = CognitionWriteback(
        events=[
            _event(
                seq=0,
                salience=99.0,  # out of range -> clamp to 1.0
                data={"api_key": "shh", "nested": {"password": "p"}, "ok": "keep"},
                agent_id="forged-other",  # must be pinned to the call's agent_id
                run_id="forged-run",  # must be pinned to source_run_id
            )
        ],
        # tool_calls must NOT be separately persisted.
        tool_calls=[],
    )

    count = context.persist_writeback("a", "run-1", wb)

    assert count == 1
    assert len(captured) == 1
    safe = captured[0]
    assert safe.agent_id == "a"
    assert safe.source_run_id == "run-1"
    assert safe.salience == 1.0
    assert safe.data["api_key"] == "***"
    assert safe.data["nested"]["password"] == "***"
    assert safe.data["ok"] == "keep"
    # The caller's writeback is not mutated (rebuild via model_copy).
    assert wb.events[0].agent_id == "forged-other"
    assert wb.events[0].data["api_key"] == "shh"
    assert wb.events[0].salience == 99.0


def test_persist_writeback_ignores_tool_calls_and_requires_ids(monkeypatch) -> None:
    calls: list[MemoryEvent] = []
    monkeypatch.setattr(context, "append_event", lambda agent_id, ev: calls.append(ev))
    wb = CognitionWriteback(
        events=[_event(seq=0), _event(seq=1)],
        tool_calls=[ToolCall(tool_id="git")],
    )
    assert context.persist_writeback("a", "run-1", wb) == 2
    assert len(calls) == 2  # tool_calls did not add rows
    with pytest.raises(AssertionError):
        context.persist_writeback("", "run-1", wb)
    with pytest.raises(AssertionError):
        context.persist_writeback("a", "", wb)


def test_load_context_delegates(monkeypatch) -> None:
    seen: dict = {}

    async def _fake(agent_id: str, *, query: str) -> CognitionContext:
        seen["agent_id"] = agent_id
        seen["query"] = query
        return CognitionContext(rules=[], memory_digest="DIGEST")

    monkeypatch.setattr("agent_cognition.invoke_context.build_cognition_context", _fake)
    ctx = asyncio.run(context.load_context("a", query="hello"))
    assert ctx.memory_digest == "DIGEST"
    assert seen == {"agent_id": "a", "query": "hello"}


def test_load_context_requires_agent_id() -> None:
    with pytest.raises(AssertionError):
        asyncio.run(context.load_context("", query="x"))


def test_ensure_rollups_current_delegates(monkeypatch) -> None:
    sentinel = object()
    seen: dict = {}

    def _fake(agent_id: str, now: datetime):
        seen["agent_id"] = agent_id
        seen["now"] = now
        return sentinel

    monkeypatch.setattr("agent_cognition.memory.rollup.ensure_rollups_current", _fake)
    out = context.ensure_rollups_current("a", _NOW)
    assert out is sentinel
    assert seen == {"agent_id": "a", "now": _NOW}
    with pytest.raises(AssertionError):
        context.ensure_rollups_current("", _NOW)


def test_complete_run_rejects_bad_status() -> None:
    # The status assertion fires before any DB access, so no Postgres needed.
    with pytest.raises(AssertionError):
        context.complete_run("a", "run-1", status="weird", response={})


def test_claim_run_precondition_asserts() -> None:
    lease = timedelta(seconds=60)
    with pytest.raises(AssertionError):
        context.claim_run("", "run-1", "h", lease)
    with pytest.raises(AssertionError):
        context.claim_run("a", "", "h", lease)
    with pytest.raises(AssertionError):
        context.claim_run("a", "run-1", "", lease)
    with pytest.raises(AssertionError):
        context.claim_run("a", "run-1", "h", timedelta(0))


def test_replay_run_precondition_asserts() -> None:
    with pytest.raises(AssertionError):
        context.replay_run("", "run-1")
    with pytest.raises(AssertionError):
        context.replay_run("a", "")


def test_ledger_raises_when_postgres_disabled(monkeypatch) -> None:
    # The _conn guard surfaces a clean storage-unavailable error rather than a
    # raw pool failure when POSTGRES_HOST is unset.
    from agent_cognition.memory.store import AgentCognitionStorageUnavailable

    monkeypatch.setattr(context, "is_postgres_enabled", lambda: False)
    with pytest.raises(AgentCognitionStorageUnavailable):
        context.claim_run("a", "run-x", "H1", timedelta(seconds=60))


def test_default_run_lease(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_COGNITION_RUN_LEASE_S", raising=False)
    assert context.default_run_lease() == timedelta(seconds=120)
    monkeypatch.setenv("AGENT_COGNITION_RUN_LEASE_S", "5")  # below floor -> clamped to 30
    assert context.default_run_lease() == timedelta(seconds=30)
    monkeypatch.setenv("AGENT_COGNITION_RUN_LEASE_S", "600")
    assert context.default_run_lease() == timedelta(seconds=600)


def test_claim_result_invariants() -> None:
    claimed = context.ClaimResult(ClaimState.CLAIMED)
    assert claimed.response is None
    replay = context.ClaimResult(ClaimState.REPLAY, response={"output": 1})
    assert replay.response == {"output": 1}


def test_sanitize_for_memory_direct() -> None:
    out = sanitize_for_memory({"token": "x", "deep": {"a": {"b": {"c": {"d": 1}}}}})
    assert out["token"] == "***"
    # depth cap kicks in below the 5th level.
    assert out["deep"]["a"]["b"]["c"] == "<truncated:depth>"
    # large collections are bounded.
    assert len(sanitize_for_memory(list(range(500)))) == 50
    assert len(sanitize_for_memory({f"k{i}": i for i in range(500)})) == 50
    # long strings truncated; unknown objects stringified.
    assert sanitize_for_memory("z" * 1000).endswith("…<truncated>")
    assert isinstance(sanitize_for_memory(object()), str)


# ===========================================================================
# Live-Postgres tests — the run ledger + writeback round-trip
# ===========================================================================
def _read_run(agent_id: str, source_run_id: str) -> dict | None:
    from psycopg.rows import dict_row

    from shared_postgres import get_conn

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT status, request_hash, response, lease_expires_at "
            "FROM agent_cognition_runs WHERE agent_id=%s AND source_run_id=%s",
            (agent_id, source_run_id),
        )
        return cur.fetchone()


def _expire_lease(agent_id: str, source_run_id: str) -> None:
    from shared_postgres import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_cognition_runs SET lease_expires_at = NOW() - INTERVAL '1 hour' "
            "WHERE agent_id=%s AND source_run_id=%s",
            (agent_id, source_run_id),
        )


_LEASE = timedelta(seconds=300)


@_PG
def test_claim_first_sight_then_complete_then_replay() -> None:
    res = context.claim_run("a", "run-1", "H1", _LEASE)
    assert res.state is ClaimState.CLAIMED
    assert res.response is None
    row = _read_run("a", "run-1")
    assert row["status"] == "in_progress"
    assert row["request_hash"] == "H1"
    assert row["lease_expires_at"] is not None

    envelope = {"output": {"answer": 42}, "cognition_writeback": {"events": []}}
    context.complete_run("a", "run-1", status="completed", response=envelope)

    row = _read_run("a", "run-1")
    assert row["status"] == "completed"
    assert row["lease_expires_at"] is None
    assert context.replay_run("a", "run-1") == envelope

    # Second claim with the same hash signals replay (no re-execution).
    again = context.claim_run("a", "run-1", "H1", _LEASE)
    assert again.state is ClaimState.REPLAY
    assert again.response == envelope


@_PG
def test_blocked_run_replays_its_4xx_envelope() -> None:
    context.claim_run("a", "run-b", "H1", _LEASE)
    blocked = {"error": "blocked by enforced precondition", "status_code": 422}
    context.complete_run("a", "run-b", status="blocked", response=blocked)
    res = context.claim_run("a", "run-b", "H1", _LEASE)
    assert res.state is ClaimState.REPLAY
    assert res.response == blocked


@_PG
def test_claim_different_body_on_terminal_row_conflicts() -> None:
    context.claim_run("a", "run-2", "H1", _LEASE)
    context.complete_run("a", "run-2", status="completed", response={"output": 1})
    res = context.claim_run("a", "run-2", "H2", _LEASE)
    assert res.state is ClaimState.CONFLICT
    assert res.response is None
    # The stored row keeps its original hash and response.
    row = _read_run("a", "run-2")
    assert row["request_hash"] == "H1"


@_PG
def test_claim_while_valid_lease_conflicts() -> None:
    context.claim_run("a", "run-3", "H1", _LEASE)
    res = context.claim_run("a", "run-3", "H1", _LEASE)  # still in_progress, lease valid
    assert res.state is ClaimState.CONFLICT


@_PG
def test_expired_lease_same_body_reclaims_in_place() -> None:
    context.claim_run("a", "run-4", "H1", _LEASE)
    _expire_lease("a", "run-4")
    res = context.claim_run("a", "run-4", "H1", _LEASE)
    assert res.state is ClaimState.CLAIMED
    row = _read_run("a", "run-4")
    assert row["status"] == "in_progress"
    assert row["request_hash"] == "H1"  # hash retained on reclaim
    assert row["lease_expires_at"] is not None  # lease refreshed into the future


@_PG
def test_expired_lease_different_body_conflicts_and_retains_hash() -> None:
    # The headline acceptance bullet: reclaim never drops the request_hash.
    context.claim_run("a", "run-5", "H1", _LEASE)
    _expire_lease("a", "run-5")

    # A post-expiry retry with a DIFFERENT body must 409 and leave the hash intact.
    res = context.claim_run("a", "run-5", "H2", _LEASE)
    assert res.state is ClaimState.CONFLICT
    row = _read_run("a", "run-5")
    assert row["request_hash"] == "H1"
    assert row["status"] == "in_progress"

    # The original-body retry can still reclaim it afterwards.
    res2 = context.claim_run("a", "run-5", "H1", _LEASE)
    assert res2.state is ClaimState.CLAIMED


@_PG
def test_replay_run_returns_none_for_in_progress_and_missing() -> None:
    context.claim_run("a", "run-6", "H1", _LEASE)
    assert context.replay_run("a", "run-6") is None  # in_progress -> no terminal envelope
    assert context.replay_run("a", "never") is None  # missing key


@_PG
def test_complete_run_without_claim_raises() -> None:
    with pytest.raises(AssertionError):
        context.complete_run("a", "ghost", status="completed", response={"x": 1})


@_PG
def test_persist_writeback_round_trip_and_idempotent() -> None:
    from agent_cognition.memory import store

    wb = CognitionWriteback(
        events=[
            _event(seq=0, salience=0.5, data={"token": "secret", "kept": 1}, run_id="run-7"),
            _event(
                seq=1, salience=2.0, data={"plain": "value"}, run_id="run-7", kind=EventKind.ERROR
            ),
        ]
    )
    assert context.persist_writeback("a", "run-7", wb) == 2

    got = store.fetch_events_for_period("a", _DAY[0], _DAY[1])
    by_seq = {e.source_seq: e for e in got}
    assert by_seq[0].data["token"] == "***"  # secret stripped on persist
    assert by_seq[0].data["kept"] == 1
    assert by_seq[0].source_run_id == "run-7"
    assert by_seq[1].salience == 1.0  # clamped

    # Re-persisting the same writeback is a no-op (idempotent on the triple).
    assert context.persist_writeback("a", "run-7", wb) == 2
    assert len(store.fetch_events_for_period("a", _DAY[0], _DAY[1])) == 2
