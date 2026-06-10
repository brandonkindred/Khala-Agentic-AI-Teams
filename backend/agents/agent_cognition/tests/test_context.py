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
from datetime import datetime, timedelta, timezone, tzinfo
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
    # Non-finite must map to 0.0 (lowest), not inflate to 1.0.
    assert context._clamp01(float("nan")) == 0.0
    assert context._clamp01(float("inf")) == 0.0
    assert context._clamp01(float("-inf")) == 0.0


def test_bound_content() -> None:
    # Sub-cap strings pass through; over-cap strings get the truncation marker.
    assert context._bound_content("short") == "short"
    big = "y" * (context._MAX_CONTENT_CHARS + 50)
    out = context._bound_content(big)
    assert out.endswith("…<truncated>")
    assert len(out) == context._MAX_CONTENT_CHARS + len("…<truncated>")
    # A non-str degrades gracefully to its string form rather than crashing.
    assert context._bound_content(123) == "123"  # type: ignore[arg-type]


def test_safe_occurred_at_normalizes_and_bounds() -> None:
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    # A naive timestamp is read as UTC.
    naive = datetime(2026, 5, 1, 9, 0)
    out = context._safe_occurred_at(naive, now)
    assert out.tzinfo is not None and out == naive.replace(tzinfo=timezone.utc)
    # A future timestamp is clamped to now.
    future = datetime(2999, 1, 1, tzinfo=timezone.utc)
    assert context._safe_occurred_at(future, now) == now
    # A past timestamp passes through unchanged.
    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert context._safe_occurred_at(past, now) == past
    # A real non-UTC offset is converted to UTC (same instant, UTC tzinfo).
    plus5 = datetime(2026, 5, 1, 14, 0, tzinfo=timezone(timedelta(hours=5)))
    out5 = context._safe_occurred_at(plus5, now)
    assert out5.tzinfo == timezone.utc
    assert out5 == datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)


def _capture_append_events(monkeypatch) -> list[MemoryEvent]:
    """Patch context.append_events to record the events it would persist."""
    captured: list[MemoryEvent] = []

    def _fake(agent_id: str, events: list[MemoryEvent]) -> int:
        captured.extend(events)
        return len(events)

    monkeypatch.setattr(context, "append_events", _fake)
    return captured


def test_persist_writeback_sanitizes_clamps_and_pins(monkeypatch) -> None:
    captured = _capture_append_events(monkeypatch)
    original = _event(
        seq=0,
        salience=99.0,  # out of range -> clamp to 1.0
        data={"api_key": "shh", "nested": {"password": "p"}, "ok": "keep"},
        agent_id="forged-other",  # must be pinned to the call's agent_id
        run_id="forged-run",  # must be pinned to source_run_id
    )
    wb = CognitionWriteback(events=[original], tool_calls=[])  # tool_calls not persisted

    context.persist_writeback("a", "run-1", wb)

    # One event reached the store, sanitized (the inserted-count return value is
    # covered by the live round-trip test, not this fake).
    assert len(captured) == 1
    safe = captured[0]
    assert safe.agent_id == "a"
    assert safe.source_run_id == "run-1"
    assert safe.salience == 1.0
    assert safe.data["api_key"] == "***"
    assert safe.data["nested"]["password"] == "***"
    assert safe.data["ok"] == "keep"
    # id is regenerated (platform owns the PK), not the agent-supplied one.
    assert safe.id != original.id
    # The caller's writeback is not mutated (rebuild via model_copy).
    assert wb.events[0].agent_id == "forged-other"
    assert wb.events[0].data["api_key"] == "shh"
    assert wb.events[0].salience == 99.0
    assert wb.events[0].id == original.id


def test_persist_writeback_bounds_content_and_clamps_future_and_nonfinite(monkeypatch) -> None:
    captured = _capture_append_events(monkeypatch)
    future = datetime(2999, 1, 1, tzinfo=timezone.utc)
    big = "x" * (context._MAX_CONTENT_CHARS + 100)
    wb = CognitionWriteback(
        events=[
            _event(seq=0, content=big, salience=float("nan"), kind=EventKind.ERROR),
            _event(seq=1, content="ok", salience=float("inf")),
        ]
    )
    # Pin a future occurred_at on the first event to exercise the clamp.
    wb.events[0].occurred_at = future

    context.persist_writeback("a", "run-1", wb)

    assert len(captured) == 2
    long_ev, ok_ev = captured[0], captured[1]
    # content bounded to the generous cap with a truncation marker.
    assert long_ev.content.endswith("…<truncated>")
    assert len(long_ev.content) <= context._MAX_CONTENT_CHARS + len("…<truncated>")
    # a sub-cap content passes through unchanged.
    assert ok_ev.content == "ok"
    # non-finite salience -> 0.0 (lowest), not inflated to 1.0.
    assert long_ev.salience == 0.0
    assert ok_ev.salience == 0.0
    # future occurred_at clamped to <= now.
    assert long_ev.occurred_at <= datetime.now(timezone.utc)


def test_persist_writeback_ignores_tool_calls_and_requires_ids(monkeypatch) -> None:
    captured = _capture_append_events(monkeypatch)
    wb = CognitionWriteback(
        events=[_event(seq=0), _event(seq=1)],
        tool_calls=[ToolCall(tool_id="git")],
    )
    context.persist_writeback("a", "run-1", wb)
    assert len(captured) == 2  # tool_calls did not add rows
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
    # The status check fires before any DB access, so no Postgres needed. It
    # raises ValueError (not assert) so it survives `python -O`.
    with pytest.raises(ValueError):
        context.complete_run("a", "run-1", status="weird", response={}, claim_token="t")


def test_complete_run_requires_claim_token() -> None:
    # A real raise (not assert) so an empty token can't silently no-op under -O.
    with pytest.raises(ValueError):
        context.complete_run("a", "run-1", status="completed", response={}, claim_token="")


def test_safe_occurred_at_handles_degenerate_tzinfo() -> None:
    # A tzinfo whose utcoffset() is None is offset-naive for comparison; it must
    # be normalized to UTC rather than raising TypeError in the min() clamp.
    class _NoOffsetTz(tzinfo):
        def utcoffset(self, dt):
            return None

        def tzname(self, dt):
            return "X"

        def dst(self, dt):
            return None

    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    value = datetime(2026, 5, 1, 9, 0, tzinfo=_NoOffsetTz())
    out = context._safe_occurred_at(value, now)
    assert out == datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)


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
    assert res.claim_token  # a fencing token is minted on claim
    row = _read_run("a", "run-1")
    assert row["status"] == "in_progress"
    assert row["request_hash"] == "H1"
    assert row["lease_expires_at"] is not None

    envelope = {"output": {"answer": 42}, "cognition_writeback": {"events": []}}
    context.complete_run(
        "a", "run-1", status="completed", response=envelope, claim_token=res.claim_token
    )

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
    claimed = context.claim_run("a", "run-b", "H1", _LEASE)
    blocked = {"error": "blocked by enforced precondition", "status_code": 422}
    context.complete_run(
        "a", "run-b", status="blocked", response=blocked, claim_token=claimed.claim_token
    )
    res = context.claim_run("a", "run-b", "H1", _LEASE)
    assert res.state is ClaimState.REPLAY
    assert res.response == blocked


@_PG
def test_claim_different_body_on_terminal_row_conflicts() -> None:
    claimed = context.claim_run("a", "run-2", "H1", _LEASE)
    context.complete_run(
        "a", "run-2", status="completed", response={"output": 1}, claim_token=claimed.claim_token
    )
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
    with pytest.raises(context.RunLedgerError):
        context.complete_run("a", "ghost", status="completed", response={"x": 1}, claim_token="t")


@_PG
def test_complete_run_is_idempotent_and_does_not_overwrite_terminal() -> None:
    claimed = context.claim_run("a", "run-8", "H1", _LEASE)
    first = {"output": 1}
    context.complete_run(
        "a", "run-8", status="completed", response=first, claim_token=claimed.claim_token
    )
    # A second complete with a DIFFERENT response must NOT overwrite the stored
    # (and possibly already-replayed) terminal envelope — it is a no-op.
    context.complete_run(
        "a",
        "run-8",
        status="blocked",
        response={"output": "OVERWRITE"},
        claim_token=claimed.claim_token,
    )
    row = _read_run("a", "run-8")
    assert row["status"] == "completed"
    assert context.replay_run("a", "run-8") == first


@_PG
def test_complete_run_fences_zombie_after_reclaim() -> None:
    # Proxy-1 claims, lease expires, Proxy-2 reclaims (token rotates). Proxy-1's
    # late complete_run with its STALE token must not overwrite Proxy-2's run.
    p1 = context.claim_run("a", "run-z", "H1", _LEASE)
    _expire_lease("a", "run-z")
    p2 = context.claim_run("a", "run-z", "H1", _LEASE)
    assert p2.state is ClaimState.CLAIMED
    assert p2.claim_token != p1.claim_token  # reclaim rotated the token

    # Zombie completion with the stale token is fenced (no-op): the row stays
    # in_progress, owned by Proxy-2.
    context.complete_run(
        "a", "run-z", status="completed", response={"loser": True}, claim_token=p1.claim_token
    )
    row = _read_run("a", "run-z")
    assert row["status"] == "in_progress"
    assert context.replay_run("a", "run-z") is None

    # Proxy-2 completes with its own token and wins.
    context.complete_run(
        "a", "run-z", status="completed", response={"winner": True}, claim_token=p2.claim_token
    )
    assert context.replay_run("a", "run-z") == {"winner": True}


@_PG
def test_complete_run_propagates_db_error_through_conn() -> None:
    # An error raised inside the `with _conn()` body (here: a non-JSON-serializable
    # response) propagates out via _conn's re-raise path, rather than being masked.
    claimed = context.claim_run("a", "run-err", "H1", _LEASE)
    with pytest.raises(Exception):
        context.complete_run(
            "a",
            "run-err",
            status="completed",
            response={"bad": object()},
            claim_token=claimed.claim_token,
        )


@_PG
def test_claim_run_enforces_lease_floor() -> None:
    # A sub-floor caller lease is bumped to the floor, so an immediate re-claim
    # still sees a valid lease (CONFLICT) rather than reclaiming an in-flight run.
    res = context.claim_run("a", "run-9", "H1", timedelta(milliseconds=1))
    assert res.state is ClaimState.CLAIMED
    again = context.claim_run("a", "run-9", "H1", timedelta(milliseconds=1))
    assert again.state is ClaimState.CONFLICT
    row = _read_run("a", "run-9")
    # Lease was floored to at least _MIN_LEASE_S into the future.
    floor = context._MIN_LEASE_S
    assert row["lease_expires_at"] > datetime.now(timezone.utc) + timedelta(seconds=floor - 5)


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
    # First persist inserts both rows.
    assert context.persist_writeback("a", "run-7", wb) == 2

    got = store.fetch_events_for_period("a", _DAY[0], _DAY[1])
    by_seq = {e.source_seq: e for e in got}
    assert by_seq[0].data["token"] == "***"  # secret stripped on persist
    assert by_seq[0].data["kept"] == 1
    assert by_seq[0].source_run_id == "run-7"
    assert by_seq[1].salience == 1.0  # clamped

    # Re-persisting the same writeback inserts nothing (idempotent on the triple);
    # the honest inserted-count is 0.
    assert context.persist_writeback("a", "run-7", wb) == 0
    assert len(store.fetch_events_for_period("a", _DAY[0], _DAY[1])) == 2
