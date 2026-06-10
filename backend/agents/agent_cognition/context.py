"""The CognitiveContext facade — the single seam wiring memory + rules + tools.

This module is the one place the invoke boundary (the proxy and, in-process,
team helpers) talks to the Agent Cognition Core. It assembles the side channel
folded onto an invoke (:func:`load_context`), keeps rollups current for the
read (:func:`ensure_rollups_current`), enforces the rule gates
(:func:`enforce_precondition` / :func:`enforce_postcondition`), persists what an
agent reports back (:func:`persist_writeback`), and owns the invoke idempotency
ledger (:func:`claim_run` / :func:`complete_run` / :func:`replay_run`).

The marker-wrapped wire envelope itself lives in
:mod:`agent_cognition.tools.envelope` (``wrap_request`` /
``try_unwrap_request`` / ``ENVELOPE_MARKER``): ``{__khala_cognition_envelope__,
input, cognition}`` on the way in, ``{output, cognition_writeback}`` on the way
out. The heavy memory/rollup/graph subsystems are imported lazily inside the two
functions that need them so importing the facade for just the ledger or the
writeback path stays cheap (it pulls in no LLM/graph/tool machinery).

Idempotency ledger state machine (``agent_cognition_runs``, keyed
``(agent_id, source_run_id)``): a first-sight :func:`claim_run` inserts an
``in_progress`` row with a lease; a retry whose ``request_hash`` matches a
terminal (``completed``/``blocked``) row replays the stored envelope; a retry
with a *different* hash, or one made while a valid lease is held, is a conflict;
an expired-lease row is reclaimed **in place with its original ``request_hash``
retained**, so a post-expiry retry with a different body still conflicts (the
reclaim never overwrites the hash).

Design by Contract: every public function documents explicit Preconditions /
Postconditions and asserts its preconditions. Invariant: every statement is
filtered by ``agent_id`` — the ledger never reads or writes another agent's row.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Json

from agent_cognition.memory.store import AgentCognitionStorageUnavailable, append_events
from agent_cognition.models import CognitionContext, CognitionWriteback, Rule
from agent_cognition.redaction import sanitize_for_memory
from agent_cognition.rules.enforcement import evaluate_postcondition, evaluate_precondition
from agent_cognition.runtime_config import read_int_with_floor
from shared_postgres import get_conn, is_postgres_enabled

if TYPE_CHECKING:
    from agent_cognition.memory.rollup import RollupReport

logger = logging.getLogger(__name__)

# Ledger statuses (mirror DESIGN §10 / the agent_cognition_runs.status column).
_IN_PROGRESS = "in_progress"
_TERMINAL_STATUSES = frozenset({"completed", "blocked"})

# Default lease for a claimed run, in seconds (floored so a misconfiguration
# can't make leases so short that legitimate runs constantly self-evict).
_DEFAULT_LEASE_S = 120
_MIN_LEASE_S = 30

# Generous cap on an event's free-text ``content`` at the writeback boundary —
# large enough for a legitimate multi-sentence summary, bounded enough that a
# hostile/oversized string can't bloat a row. (Distinct from the redactor's
# tighter per-value cap used for nested ``data`` structures.)
_MAX_CONTENT_CHARS = 8192

__all__ = [
    "ClaimState",
    "ClaimResult",
    "CognitionBlocked",
    "PreconditionBlocked",
    "PostconditionViolation",
    "RunLedgerError",
    "default_run_lease",
    "ensure_rollups_current",
    "load_context",
    "persist_writeback",
    "enforce_precondition",
    "enforce_postcondition",
    "claim_run",
    "complete_run",
    "replay_run",
]


class RunLedgerError(RuntimeError):
    """A run-ledger operation found the ledger in a state its caller's contract forbids.

    Distinct from :class:`~agent_cognition.memory.store.AgentCognitionStorageUnavailable`
    (an infrastructure outage): this is a logical violation, e.g. completing a
    run that was never claimed.
    """


# ---------------------------------------------------------------------------
# Enforced rule gates — raise on block (wrappers over the pure evaluators).
# ---------------------------------------------------------------------------
class CognitionBlocked(Exception):
    """An enforced rule blocked the call. Carries ``reason`` and ``phase``.

    Subclasses :class:`Exception` (not ``ValueError``) so a broad
    ``except ValueError`` in a caller can't accidentally swallow a guardrail
    block.
    """

    phase: str = ""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PreconditionBlocked(CognitionBlocked):
    """Raised by :func:`enforce_precondition` when an enforced precondition blocks."""

    phase = "precondition"


class PostconditionViolation(CognitionBlocked):
    """Raised by :func:`enforce_postcondition` when an enforced postcondition blocks."""

    phase = "postcondition"


def enforce_precondition(agent_id: str, input_body: Any, rules: list[Rule]) -> None:
    """Enforced precondition gate — raises on block.

    Preconditions:
        * ``agent_id`` is non-empty; ``rules`` are the agent's candidate rules
          (any phase/mode — the evaluator filters to active enforced
          preconditions).
    Postconditions:
        * Returns ``None`` when every active enforced precondition rule holds
          for the namespaced root ``{"input": input_body, "agent_id":
          agent_id}``.
        * Raises :class:`PreconditionBlocked` carrying the first failing rule's
          reason otherwise. A malformed enforced predicate fails closed (blocks).
    """
    assert agent_id, "enforce_precondition: agent_id must be non-empty"
    allowed, reason = evaluate_precondition({"input": input_body, "agent_id": agent_id}, rules)
    if not allowed:
        raise PreconditionBlocked(reason or "blocked by enforced precondition")


def enforce_postcondition(output: Mapping[str, Any], rules: list[Rule]) -> None:
    """Enforced postcondition gate — raises on block.

    Preconditions:
        * ``output`` is the agent's raw result mapping (the evaluator wraps it
          as ``{"output": output}`` internally — pass it unwrapped); ``rules``
          are the agent's candidate rules.
    Postconditions:
        * Returns ``None`` when every active enforced postcondition rule holds.
        * Raises :class:`PostconditionViolation` carrying the first failing
          rule's reason otherwise. A malformed enforced predicate fails closed.
    """
    allowed, reason = evaluate_postcondition(output, rules)
    if not allowed:
        raise PostconditionViolation(reason or "blocked by enforced postcondition")


# ---------------------------------------------------------------------------
# Context assembly / rollups — thin facades over the substrate (lazy imports
# keep the LLM/graph deps off the facade's module-import path).
# ---------------------------------------------------------------------------
def ensure_rollups_current(agent_id: str, now: datetime) -> RollupReport:
    """Lazy rollup catch-up for the invoke path (facade entry).

    Thin wrapper over :func:`agent_cognition.memory.rollup.ensure_rollups_current`
    so callers reach the single cognition seam rather than the rollup engine
    directly.

    Preconditions:
        * ``agent_id`` is non-empty; ``now`` is timezone-aware (the engine
          asserts the latter).
    Postconditions:
        * Returns the engine's ``RollupReport`` unchanged.
    """
    assert agent_id, "ensure_rollups_current: agent_id must be non-empty"
    from agent_cognition.memory.rollup import ensure_rollups_current as _engine

    return _engine(agent_id, now)


async def load_context(agent_id: str, *, query: str = "") -> CognitionContext:
    """Assemble the ``CognitionContext`` injected on an agent invoke (facade entry).

    Delegates to :func:`agent_cognition.invoke_context.build_cognition_context`,
    which gathers the agent's active rules + memory digest + best-effort
    knowledge-graph context concurrently and never raises on a graph hiccup.

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * Returns ``CognitionContext{rules, memory_digest}``; either field may be
          empty for a brand-new agent. Never raises on a graph failure.
    """
    assert agent_id, "load_context: agent_id must be non-empty"
    from agent_cognition.invoke_context import build_cognition_context

    return await build_cognition_context(agent_id, query=query)


# ---------------------------------------------------------------------------
# Writeback persistence — append events, strip secrets, bound salience.
# ---------------------------------------------------------------------------
def persist_writeback(agent_id: str, source_run_id: str, writeback: CognitionWriteback) -> int:
    """Persist an agent's cognition writeback events, defensively re-sanitized.

    Appends ``writeback.events`` to the episodic store in **one transaction**
    (atomic — see :func:`agent_cognition.memory.store.append_events`). The
    writeback rode the wire back from an untrusted sandboxed agent, so this is a
    trust boundary; every field that could harm the platform is normalized
    rather than trusted:

    * ``id`` is **regenerated** (a fresh UUID) — the agent does not control the
      episodic-event primary key. The ``ON CONFLICT`` idempotency target is the
      ``(agent_id, source_run_id, source_seq)`` triple, so a forged or colliding
      agent-supplied id could otherwise raise a ``UniqueViolation`` and abort
      the whole batch; regenerating it removes that vector while keeping the
      triple-based dedup intact (a re-persist of the same writeback gets new ids
      but the same triples, so ``DO NOTHING`` still no-ops).
    * ``data`` is re-run through the secret stripper and ``content`` is bounded
      to a generous char cap (an oversized content string can't bloat the row).
    * ``salience`` is clamped to ``[0, 1]`` (non-finite → ``0.0``).
    * ``occurred_at`` is normalized to tz-aware UTC and never allowed into the
      future (a future timestamp would land the event in a calendar period that
      never closes, wedging rollups).
    * ``agent_id`` / ``source_run_id`` are pinned to the caller's authoritative
      values (a writeback cannot forge another agent's id or scribble into a
      different run's idempotency key).

    ``writeback.tool_calls`` is **not** separately persisted: the tool broker
    already emits per-call ``tool_call`` / ``outcome`` events into
    ``writeback.events`` (with contiguous ``source_seq``), and there is no
    tool-calls table — ``tool_calls`` is a one-per-call audit mirror the proxy
    reconciles, not a second set of rows.

    Preconditions:
        * ``agent_id`` and ``source_run_id`` are non-empty.
    Postconditions:
        * Events are appended atomically and idempotently on ``(agent_id,
          source_run_id, source_seq)`` (a duplicated writeback is a no-op).
          Events are rebuilt via ``model_copy`` — the caller's ``writeback`` is
          never mutated.
        * Returns the number of events **actually inserted** (``0`` when an
          idempotent re-persist hits every row's ``ON CONFLICT DO NOTHING``).
    """
    assert agent_id, "persist_writeback: agent_id must be non-empty"
    assert source_run_id, "persist_writeback: source_run_id must be non-empty"
    now = _now()
    safe_events = [
        event.model_copy(
            update={
                "id": str(uuid4()),
                "agent_id": agent_id,
                "source_run_id": source_run_id,
                "content": _bound_content(event.content),
                "data": sanitize_for_memory(event.data),
                "salience": _clamp01(event.salience),
                "occurred_at": _safe_occurred_at(event.occurred_at, now),
            }
        )
        for event in writeback.events
    ]
    return append_events(agent_id, safe_events)


# ---------------------------------------------------------------------------
# Run ledger — invoke idempotency + leasing.
# ---------------------------------------------------------------------------
class ClaimState(str, Enum):
    """Outcome of a :func:`claim_run` attempt against the run ledger."""

    CLAIMED = "claimed"  # first-sight insert OR in-place reclaim of an expired lease
    REPLAY = "replay"  # terminal row, request_hash matches -> serve stored response
    CONFLICT = "conflict"  # different body, or in-flight with a still-valid lease


@dataclass(frozen=True)
class ClaimResult:
    """The result of :func:`claim_run`.

    Invariants:
        * ``state is CLAIMED``  -> ``response is None`` and ``claim_token`` is the
          per-claim fencing nonce the caller must pass back to
          :func:`complete_run` (the executor proves it still owns the claim).
        * ``state is REPLAY``   -> ``response`` is the stored terminal envelope;
          ``claim_token is None``.
        * ``state is CONFLICT`` -> ``response is None`` and ``claim_token is None``.
    """

    state: ClaimState
    response: dict[str, Any] | None = None
    claim_token: str | None = None


# A single statement claims (first sight) or reclaims an expired lease in place.
# RETURNING is non-empty ONLY when a row was inserted or reclaimed. The DO UPDATE
# deliberately omits request_hash from the SET (reclaim retains the original) and
# its WHERE requires request_hash = EXCLUDED.request_hash, so an expired-lease
# retry with a *different* body does not reclaim — it leaves the row untouched
# (hash intact) and falls through to the classify read, which returns CONFLICT.
# Both the insert and the reclaim stamp a fresh ``claim_token`` so a reclaim
# rotates ownership away from the previous (now-zombie) claimer.
_CLAIM_SQL = """
INSERT INTO agent_cognition_runs
    (agent_id, source_run_id, status, request_hash, lease_expires_at, claim_token, created_at)
VALUES (%(agent_id)s, %(srid)s, 'in_progress', %(hash)s, %(now)s + %(lease)s, %(token)s, %(now)s)
ON CONFLICT (agent_id, source_run_id) DO UPDATE
    SET status = 'in_progress',
        lease_expires_at = %(now)s + %(lease)s,
        claim_token = EXCLUDED.claim_token,
        response = NULL,
        completed_at = NULL
    WHERE agent_cognition_runs.status = 'in_progress'
      AND agent_cognition_runs.lease_expires_at IS NOT NULL
      AND agent_cognition_runs.lease_expires_at <= %(now)s
      AND agent_cognition_runs.request_hash = EXCLUDED.request_hash
RETURNING agent_id
"""

_CLASSIFY_SQL = """
SELECT status, request_hash, response
FROM agent_cognition_runs
WHERE agent_id = %(agent_id)s AND source_run_id = %(srid)s
"""


def claim_run(
    agent_id: str, source_run_id: str, request_hash: str, lease: timedelta
) -> ClaimResult:
    """Atomically claim, reclaim, replay, or reject a run-ledger key.

    Preconditions:
        * ``agent_id``, ``source_run_id``, ``request_hash`` are non-empty;
          ``lease`` is a positive duration.
    Postconditions:
        * first sight -> insert ``in_progress(request_hash, lease)`` ->
          ``CLAIMED`` with a fresh ``claim_token``.
        * terminal (``completed``/``blocked``) row, ``request_hash`` matches ->
          ``REPLAY`` carrying the stored ``response``.
        * any existing row whose ``request_hash`` differs -> ``CONFLICT``; the
          stored row is left untouched (the reclaim never overwrites the hash).
        * ``in_progress`` row with a still-valid lease -> ``CONFLICT``.
        * ``in_progress`` row with an expired lease and matching hash ->
          reclaimed in place (fresh lease + fresh ``claim_token``,
          response/completed_at cleared, ``request_hash`` retained) -> ``CLAIMED``.
        * The stored lease is ``max(lease, _MIN_LEASE_S)`` (a hardcoded 30s
          floor, distinct from the ``AGENT_COGNITION_RUN_LEASE_S`` default lease)
          — a sub-floor caller lease is raised to the floor so a too-short lease
          can't let a retry reclaim a still-in-flight run.
    Invariant:
        * The claim/reclaim decision and any mutation happen in one transaction,
          so concurrent claimers cannot both observe "first sight" or both
          reclaim. A reclaim rotates ``claim_token``, fencing the prior claimer.
    """
    assert agent_id, "claim_run: agent_id must be non-empty"
    assert source_run_id, "claim_run: source_run_id must be non-empty"
    assert request_hash, "claim_run: request_hash must be non-empty"
    assert lease > timedelta(0), "claim_run: lease must be a positive duration"

    # Enforce the lease floor here rather than trusting the caller: a too-short
    # lease (with the inclusive `lease_expires_at <= now` reclaim test) would let
    # a concurrent retry reclaim a run that is still genuinely in flight.
    effective_lease = max(lease, timedelta(seconds=_MIN_LEASE_S))
    token = str(uuid4())
    params = {
        "agent_id": agent_id,
        "srid": source_run_id,
        "hash": request_hash,
        "now": _now(),
        "lease": effective_lease,
        "token": token,
    }
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_CLAIM_SQL, params)
        if cur.fetchone() is not None:
            # Inserted (first sight) or reclaimed an expired lease (matching hash).
            return ClaimResult(ClaimState.CLAIMED, claim_token=token)
        # A conflicting row exists but was not reclaimable; classify it.
        cur.execute(_CLASSIFY_SQL, {"agent_id": agent_id, "srid": source_run_id})
        row = cur.fetchone()
        # The upsert conflicted, so the row must exist within this transaction.
        assert row is not None, "claim_run: conflicting run row vanished mid-transaction"
        # Hash mismatch first: a different body is a conflict for ANY status, so a
        # terminal row is never replayed with stale output for a changed request.
        if row["request_hash"] != request_hash:
            return ClaimResult(ClaimState.CONFLICT)
        if row["status"] in _TERMINAL_STATUSES:
            return ClaimResult(ClaimState.REPLAY, response=row["response"])
        # in_progress with a still-valid lease (an expired+matching one reclaims above).
        return ClaimResult(ClaimState.CONFLICT)


def complete_run(
    agent_id: str,
    source_run_id: str,
    *,
    status: str,
    response: Mapping[str, Any],
    claim_token: str,
) -> None:
    """Write the terminal ledger row for a previously claimed run.

    The UPDATE matches only an ``in_progress`` row **whose ``claim_token``
    matches the caller's** — the nonce :func:`claim_run` returned on ``CLAIMED``.
    This fences two ways:

    * An **already-terminal** row is not matched, so a retried ``complete_run``
      is an idempotent no-op rather than overwriting a stored (possibly
      already-replayed) envelope.
    * A **zombie** completer whose lease was reclaimed by another worker is not
      matched either: the reclaim rotated ``claim_token``, so the original
      claimer's token no longer fences the row — it can't clobber the new
      claimer's in-flight run.

    Preconditions:
        * ``agent_id`` / ``source_run_id`` are non-empty; ``status`` is
          ``"completed"`` or ``"blocked"``; ``claim_token`` is the token a prior
          :func:`claim_run` returned ``CLAIMED`` for this key.
    Postconditions:
        * When the caller still owns the in-progress claim: its ``status`` /
          ``response`` / ``completed_at`` are set, ``lease_expires_at`` is
          cleared, ``request_hash`` is untouched; a later same-hash
          :func:`claim_run` replays ``response``.
        * When the row is already terminal, or held by a different (reclaimed)
          claim, or the caller's token doesn't match: a no-op (the stored state
          is preserved). A terminal re-complete whose terminal ``status``
          **differs** from what is stored (e.g. ``blocked`` after ``completed``)
          is logged at WARNING (a likely double-completion bug); every other
          unmatched case is a DEBUG no-op. (The stored ``response`` is not
          compared — a JSONB round-trip vs the in-memory dict would false-positive
          on benign retries.)
        * Raises :class:`RunLedgerError` when no row exists at all (the run was
          never claimed) — a real branch, not an ``assert`` (which ``python -O``
          would strip, silently losing the run).
    """
    assert agent_id, "complete_run: agent_id must be non-empty"
    assert source_run_id, "complete_run: source_run_id must be non-empty"
    # A missing token is enforced with a real raise (not an ``assert``, which
    # ``python -O`` strips): under -O a None/empty token would otherwise make the
    # UPDATE's ``claim_token = NULL`` match nothing and silently lose the run.
    if not claim_token:
        raise ValueError("complete_run: claim_token must be non-empty")
    if status not in _TERMINAL_STATUSES:
        raise ValueError(
            f"complete_run: status must be one of {sorted(_TERMINAL_STATUSES)}, got {status!r}"
        )
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """UPDATE agent_cognition_runs
                  SET status = %s, response = %s, completed_at = %s, lease_expires_at = NULL
                WHERE agent_id = %s AND source_run_id = %s
                      AND status = %s AND claim_token = %s
            RETURNING source_run_id""",
            (
                status,
                Json(dict(response)),
                _now(),
                agent_id,
                source_run_id,
                _IN_PROGRESS,
                claim_token,
            ),
        )
        if cur.fetchone() is not None:
            return
        # No owned in_progress row matched: the key is terminal, reclaimed by a
        # different claim, or absent. Read the current state to decide.
        cur.execute(
            "SELECT status FROM agent_cognition_runs WHERE agent_id = %s AND source_run_id = %s",
            (agent_id, source_run_id),
        )
        existing = cur.fetchone()
    if existing is None:
        raise RunLedgerError(
            "complete_run: no claimed run to complete for "
            f"agent_id={agent_id!r} source_run_id={source_run_id!r}"
        )
    if existing["status"] in _TERMINAL_STATUSES and existing["status"] != status:
        # A second terminal completion with a *different terminal status* (e.g.
        # blocked after completed) — a likely double-completion bug. We key the
        # warning on the status alone, not the response: comparing a JSONB
        # round-tripped envelope against the in-memory dict would false-positive
        # on benign retries (tuples decode as lists, etc.).
        logger.warning(
            "complete_run: run %s/%s already terminal (%s); discarding conflicting %s completion",
            agent_id,
            source_run_id,
            existing["status"],
            status,
        )
    else:
        # Already terminal with the same status (benign idempotent re-complete),
        # or in_progress under a different/reclaimed claim_token (zombie fenced).
        logger.debug(
            "complete_run: run %s/%s already terminal or reclaimed (state=%s); no-op",
            agent_id,
            source_run_id,
            existing["status"],
        )


def replay_run(agent_id: str, source_run_id: str) -> dict[str, Any] | None:
    """Return the stored response envelope for a terminal run, else ``None``.

    Preconditions:
        * ``agent_id`` / ``source_run_id`` are non-empty.
    Postconditions:
        * The stored ``response`` dict when a row exists with a terminal status
          and a non-null response; ``None`` otherwise (including an
          ``in_progress`` row, which has no terminal envelope to serve).
    """
    assert agent_id, "replay_run: agent_id must be non-empty"
    assert source_run_id, "replay_run: source_run_id must be non-empty"
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT status, response FROM agent_cognition_runs
                WHERE agent_id = %s AND source_run_id = %s""",
            (agent_id, source_run_id),
        )
        row = cur.fetchone()
    if row is None or row["status"] not in _TERMINAL_STATUSES or row["response"] is None:
        return None
    return row["response"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def default_run_lease() -> timedelta:
    """The default run lease as a ``timedelta`` (env ``AGENT_COGNITION_RUN_LEASE_S``).

    Postconditions: a positive duration of at least ``_MIN_LEASE_S`` seconds.
    """
    return timedelta(
        seconds=read_int_with_floor("AGENT_COGNITION_RUN_LEASE_S", _DEFAULT_LEASE_S, _MIN_LEASE_S)
    )


def _clamp01(value: float) -> float:
    """Clamp a salience weight to ``[0.0, 1.0]``; map non-finite values to ``0.0``.

    ``max(0.0, min(1.0, nan))`` would return ``1.0`` (all NaN comparisons are
    False), inflating a forged non-finite salience to the *maximum* — the inverse
    of the defensive intent. So reject NaN/±inf to the lowest weight first.
    """
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _bound_content(content: str) -> str:
    """Bound an event's free-text ``content`` to ``_MAX_CONTENT_CHARS`` characters.

    A generous cap *in characters* (vs the per-value redactor cap used for nested
    ``data``) so a legitimate multi-sentence summary survives intact while a
    hostile/oversized content string can't grow a row without bound. Untyped/empty
    input degrades gracefully.
    """
    if not isinstance(content, str):
        content = str(content)
    if len(content) > _MAX_CONTENT_CHARS:
        return content[:_MAX_CONTENT_CHARS] + "…<truncated>"
    return content


def _safe_occurred_at(value: datetime, now: datetime) -> datetime:
    """Normalize an event timestamp to tz-aware UTC and bound it to ``now``.

    * A naive timestamp — or one carrying a degenerate ``tzinfo`` whose
      ``utcoffset()`` is ``None`` (which ``min`` would treat as offset-naive and
      refuse to compare against an aware ``now``, raising ``TypeError``) — is
      read as UTC, matching how Postgres reads a naive value into a
      ``TIMESTAMPTZ`` column under a UTC session.
    * A real offset-aware timestamp is **converted** to UTC (not merely passed
      through with its original offset), so the returned value is genuinely UTC
      regardless of the agent's zone.
    * The result is clamped to ``now`` so an untrusted writeback can't place an
      event in a calendar period that never closes (which would wedge rollups).

    Agents are expected to emit tz-aware UTC timestamps.
    """
    if value.utcoffset() is None:
        aware = value.replace(tzinfo=timezone.utc)
    else:
        aware = value.astimezone(timezone.utc)
    return min(aware, now)


@contextmanager
def _conn():
    """Yield a pooled connection, translating *acquisition* failures only.

    Preconditions:
        * Postgres is configured (``POSTGRES_HOST`` set).
    Postconditions:
        * Errors raised while *acquiring* the connection surface as
          :class:`AgentCognitionStorageUnavailable`; errors raised inside the
          ``with`` body propagate unchanged, so a genuine query bug is never
          masked as an infrastructure outage. Commit-on-success and
          rollback-on-error are delegated to the underlying ``shared_postgres``
          pool context.
    """
    if not is_postgres_enabled():
        raise AgentCognitionStorageUnavailable(
            "POSTGRES_HOST is not configured; Agent Cognition storage is unavailable."
        )
    pool_ctx = get_conn()
    try:
        conn = pool_ctx.__enter__()
    except Exception as exc:  # pragma: no cover — pool/connection failure path
        raise AgentCognitionStorageUnavailable(str(exc)) from exc
    try:
        yield conn
    except BaseException as exc:
        if not pool_ctx.__exit__(type(exc), exc, exc.__traceback__):
            raise
    else:
        pool_ctx.__exit__(None, None, None)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)
