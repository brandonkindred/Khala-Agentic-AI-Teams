"""Invoke-boundary gate — the inject-on-invoke / writeback-on-return lifecycle.

This module is the reusable orchestrator the invoke boundary runs an agent
call through, shared by the unified API's HTTP proxy
(``unified_api/routes/agents.py``) and by in-process callers that have no
HTTP hop (:func:`invoke_in_process`). It is pure orchestration over the
:mod:`agent_cognition.context` facade: no FastAPI types, no transport — every
abnormal path is a typed :class:`GateOutcome` the caller maps to its own
error surface.

Lifecycle (one invoke):

1. Derive a stable ``source_run_id``: the caller's idempotency key when
   supplied, else the request body's canonical hash (byte-identical keyless
   retries still dedup). A side-effecting agent (manifest
   ``requires_idempotency_key: true``) is rejected without a caller key —
   a keyless call is documented at-least-once, not run-once.
2. Claim the leased ``agent_cognition_runs`` ledger: a terminal row with a
   matching hash **replays the stored envelope without re-invoking** (this
   covers ``blocked`` rows too, so a retried rule block replays the same
   4xx); a matching key with a different hash, or a still-leased in-flight
   row, is a conflict; an expired lease is reclaimed in place and re-executed.
3. Lazy rollup catch-up, then context load (active rules + memory digest) —
   both best-effort: a cognition hiccup must never break the invoke.
4. Enforced **precondition** gate: a block persists a memory event, stores
   the 4xx envelope in the ledger as ``blocked``, and short-circuits.
5. Wrap the body in the marker envelope (the proxy never edits prompts —
   advisory rules ride the ``cognition`` side channel for the runtime to
   render) and re-check the *full* envelope against the payload cap.
6. On return (:func:`finalize_invoke`): enforced **postcondition** gate
   *first*, then persistence. A violation drops the model output and the
   agent-authored memory but persists the shim's trusted tool audit plus a
   blocked-run event, and stores the 4xx envelope as ``blocked``. A pass
   persists the writeback events and completes the ledger row.

Storage-outage policy: when the ledger is unreachable, a
``requires_idempotency_key`` agent fails closed (``LEDGER_UNAVAILABLE`` →
503 at the proxy) because run-once cannot be guaranteed for a side-effecting
agent; any other cognition agent degrades to an unledgered invoke with a
warning, matching the invoke path's never-break posture.

Design by Contract: every public function documents explicit Preconditions /
Postconditions and asserts its preconditions. Invariant: this module never
mints a second ``source_run_id`` for one request — prepare and finalize
operate on the same :class:`PreparedInvoke`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from agent_cognition.context import (
    ClaimState,
    PostconditionViolation,
    PreconditionBlocked,
    RunLedgerError,
    claim_run,
    complete_run,
    default_run_lease,
    enforce_postcondition,
    enforce_precondition,
    ensure_rollups_current,
    load_context,
    persist_writeback,
)
from agent_cognition.memory.store import AgentCognitionStorageUnavailable
from agent_cognition.models import (
    CognitionContext,
    CognitionWriteback,
    EventKind,
    MemoryEvent,
    ToolCall,
)
from agent_cognition.tools.envelope import wrap_request
from shared_postgres import is_postgres_enabled

logger = logging.getLogger(__name__)

__all__ = [
    "GateOutcome",
    "GateOutcomeKind",
    "PreparedInvoke",
    "FinalizeOutcome",
    "derive_source_run_id",
    "prepare_invoke",
    "finalize_invoke",
    "invoke_in_process",
]

# Salience for gate-authored events: a rule block is a notable episode the
# reflection engine should weigh; a tool-call audit row is routine.
_BLOCK_EVENT_SALIENCE = 0.8
_AUDIT_EVENT_SALIENCE = 0.5


class GateOutcomeKind(str, Enum):
    """How the caller must proceed after :func:`prepare_invoke`."""

    PROCEED = "proceed"  # run the agent with ``prepared.envelope``
    REPLAY = "replay"  # serve ``status_code``/``content`` without re-invoking
    CONFLICT = "conflict"  # 409 — same key, different body, or still leased
    MISSING_IDEMPOTENCY_KEY = "missing_idempotency_key"  # 400 — side-effecting, keyless
    LEDGER_UNAVAILABLE = "ledger_unavailable"  # 503 — run-once not guaranteeable
    BLOCKED = "blocked"  # 4xx — enforced precondition blocked the call
    ENVELOPE_TOO_LARGE = "envelope_too_large"  # 413 — body + cognition over the cap


@dataclass(frozen=True)
class PreparedInvoke:
    """Everything :func:`finalize_invoke` needs to close out a prepared run.

    Invariants:
        * ``claim_token`` is ``None`` exactly when the run is unledgered
          (Postgres off, or a tolerated storage outage) — finalize then skips
          ledger completion but still runs the gates.
        * ``context`` is ``None`` when context assembly failed or storage is
          off — finalize then has no rules to enforce and no digest was
          injected (``envelope`` is the caller body, unwrapped).
    """

    agent_id: str
    source_run_id: str
    request_hash: str
    claim_token: str | None
    context: CognitionContext | None
    envelope: Any


@dataclass(frozen=True)
class GateOutcome:
    """Result of :func:`prepare_invoke`.

    Invariants:
        * ``kind is PROCEED``  → ``prepared`` is set; all other fields unset.
        * ``kind is REPLAY``   → ``status_code`` + ``content`` carry the stored
          terminal envelope.
        * ``kind is BLOCKED``  → ``status_code`` + ``content`` carry the 4xx
          envelope (also stored in the ledger when the run was claimed).
        * every other kind     → ``reason`` carries a human-readable cause.
    """

    kind: GateOutcomeKind
    prepared: PreparedInvoke | None = None
    status_code: int | None = None
    content: Any = None
    reason: str | None = None


@dataclass(frozen=True)
class FinalizeOutcome:
    """Result of :func:`finalize_invoke`.

    Invariants:
        * ``blocked`` is ``True`` exactly when an enforced postcondition
          violated — ``status_code``/``content`` then carry the 4xx envelope
          and the model output was **not** persisted.
        * otherwise ``status_code``/``content`` echo the upstream response.
    """

    status_code: int
    content: Any
    blocked: bool = False
    persisted_events: int = 0


def derive_source_run_id(body: Any, idempotency_key: str | None = None) -> tuple[str, str]:
    """Derive the run-ledger key and the canonical request hash for a body.

    Preconditions:
        * ``body`` is JSON-representable (the proxy parsed it from JSON; an
          in-process caller passes plain data). Unserializable leaves degrade
          via ``default=str`` rather than raising.
    Postconditions:
        * Returns ``(source_run_id, request_hash)``: ``request_hash`` is the
          SHA-256 hex digest of the canonical JSON encoding (sorted keys,
          compact separators), so two key-order permutations of the same
          mapping hash identically; ``source_run_id`` is the stripped caller
          key when non-empty, else the hash (keyless byte-identical retries
          still dedup).
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    key = (idempotency_key or "").strip()
    return (key or request_hash, request_hash)


async def prepare_invoke(
    agent_id: str,
    body: Any,
    *,
    requires_idempotency_key: bool = False,
    idempotency_key: str | None = None,
    max_envelope_bytes: int | None = None,
) -> GateOutcome:
    """Run the pre-flight half of the invoke lifecycle (steps 1–5 above).

    Preconditions:
        * ``agent_id`` is non-empty; the caller verified the agent is
          cognition-enabled (manifest carries a ``cognition`` block).
        * ``max_envelope_bytes``, when given, is positive — the transport's
          payload cap to re-apply to the *wrapped* envelope.
    Postconditions:
        * Returns a :class:`GateOutcome` per its invariants. ``PROCEED`` means
          the ledger row (when claimed) is ``in_progress`` under
          ``prepared.claim_token`` and the caller **must** eventually reach
          :func:`finalize_invoke` (or let the lease expire on failure).
        * ``BLOCKED`` has already persisted the block memory event and (when
          claimed) stored the 4xx envelope as ``blocked`` — a retry replays it.
        * No ledger row is written for ``MISSING_IDEMPOTENCY_KEY`` or
          ``LEDGER_UNAVAILABLE``.
    """
    assert agent_id, "prepare_invoke: agent_id must be non-empty"
    assert max_envelope_bytes is None or max_envelope_bytes > 0, (
        "prepare_invoke: max_envelope_bytes must be positive when given"
    )

    key = (idempotency_key or "").strip() or None
    if requires_idempotency_key and key is None:
        return GateOutcome(
            kind=GateOutcomeKind.MISSING_IDEMPOTENCY_KEY,
            reason=(
                f"Agent {agent_id!r} is side-effecting (requires_idempotency_key) and "
                "must be invoked with an Idempotency-Key header; without one the call "
                "would be at-least-once, not run-once."
            ),
        )
    source_run_id, request_hash = derive_source_run_id(body, key)

    claim_token: str | None = None
    if is_postgres_enabled():
        try:
            claim = await asyncio.to_thread(
                claim_run, agent_id, source_run_id, request_hash, default_run_lease()
            )
        except AgentCognitionStorageUnavailable as exc:
            outage = _storage_outage_outcome(agent_id, requires_idempotency_key, exc)
            if outage is not None:
                return outage
        else:
            if claim.state is ClaimState.REPLAY:
                return _replay_outcome(agent_id, source_run_id, claim.response)
            if claim.state is ClaimState.CONFLICT:
                return GateOutcome(
                    kind=GateOutcomeKind.CONFLICT,
                    reason=(
                        f"Run {source_run_id!r} for agent {agent_id!r} conflicts: the key is "
                        "bound to a different request body, or the run is still in flight."
                    ),
                )
            claim_token = claim.claim_token
    elif requires_idempotency_key:
        return GateOutcome(
            kind=GateOutcomeKind.LEDGER_UNAVAILABLE,
            reason=(
                f"Agent {agent_id!r} requires run-once semantics but the cognition run "
                "ledger is unavailable (Postgres is not configured)."
            ),
        )

    # Lazy catch-up + context load — both best-effort; a cognition subsystem
    # hiccup must never break the invoke (matching the proxy's prior posture).
    try:
        await asyncio.to_thread(ensure_rollups_current, agent_id, _now())
    except Exception:
        logger.warning(
            "cognition: rollup catch-up failed for %s; continuing", agent_id, exc_info=True
        )
    context: CognitionContext | None = None
    try:
        from agent_cognition.invoke_context import extract_query_text

        context = await load_context(agent_id, query=extract_query_text(body))
    except Exception:
        logger.warning(
            "cognition: context build failed for %s; proceeding without injection",
            agent_id,
            exc_info=True,
        )

    post_body: Any = body
    if context is not None:
        try:
            enforce_precondition(agent_id, body, context.rules)
        except PreconditionBlocked as exc:
            content = _blocked_content("precondition", exc.reason)
            await asyncio.to_thread(
                _record_block,
                agent_id,
                source_run_id,
                claim_token,
                phase="precondition",
                reason=exc.reason,
                content=content,
            )
            return GateOutcome(kind=GateOutcomeKind.BLOCKED, status_code=422, content=content)
        post_body = wrap_request(body, context.model_dump(mode="json"))

        if max_envelope_bytes is not None:
            size = len(json.dumps(post_body, default=str).encode("utf-8"))
            if size > max_envelope_bytes:
                # Not stored as a terminal row: the digest half of the envelope
                # varies over time, so this is not a stable property of the key.
                # The claim's lease simply expires and a later retry re-executes.
                return GateOutcome(
                    kind=GateOutcomeKind.ENVELOPE_TOO_LARGE,
                    reason=(
                        f"Request plus injected cognition context is {size} bytes, over the "
                        f"{max_envelope_bytes}-byte envelope cap. Trim the request body — the "
                        "cognition side channel shares the payload budget."
                    ),
                )

    return GateOutcome(
        kind=GateOutcomeKind.PROCEED,
        prepared=PreparedInvoke(
            agent_id=agent_id,
            source_run_id=source_run_id,
            request_hash=request_hash,
            claim_token=claim_token,
            context=context,
            envelope=post_body,
        ),
    )


async def finalize_invoke(
    prepared: PreparedInvoke, upstream_status: int, content: Any
) -> FinalizeOutcome:
    """Run the return half of the lifecycle: postcondition gate, then persistence.

    Preconditions:
        * ``prepared`` came from a ``PROCEED`` outcome of :func:`prepare_invoke`
          for this same request; ``upstream_status`` is the agent's HTTP-shaped
          status (an in-process success passes ``200``); ``content`` is the
          response body (the shim's envelope, or an envelope-shaped dict).
    Postconditions:
        * **Postcondition violation** (2xx upstream, enforced rule fails on the
          envelope's mapping ``output``): the model output and agent-authored
          memory events are **not** persisted; the shim's trusted ``tool_audit``
          *is* (as ``tool_call`` events) plus one blocked-run event; the 4xx
          envelope is stored as ``blocked`` when the run was claimed. Returns
          ``blocked=True`` with the 4xx envelope.
        * **2xx pass**: the envelope's ``memory_events`` are validated
          (malformed entries dropped with a warning, never failing the call)
          and persisted; the ledger row completes as ``completed`` storing
          ``{status_code, content}`` for replay. Returns the upstream response.
        * **Non-2xx**: only the trusted ``tool_audit`` is persisted (side
          effects that ran are recorded even on failure); the ledger row is
          left to its lease — a transient failure is not a replayable terminal
          state, so an expired-lease retry re-executes. Returns the upstream
          response unchanged.
        * All persistence is best-effort: storage failures are logged and never
          mask the response.
    """
    assert prepared.agent_id, "finalize_invoke: prepared.agent_id must be non-empty"
    inner = _inner_envelope(content, upstream_status)
    success = 200 <= upstream_status < 300

    if not success:
        events = _audit_events(inner, prepared.agent_id, prepared.source_run_id)
        if events:
            await asyncio.to_thread(
                _persist_events_best_effort, prepared.agent_id, prepared.source_run_id, events
            )
        return FinalizeOutcome(status_code=upstream_status, content=content)

    rules = prepared.context.rules if prepared.context is not None else []
    output = inner.get("output") if isinstance(inner, Mapping) else None
    if isinstance(output, Mapping) and rules:
        try:
            enforce_postcondition(output, rules)
        except PostconditionViolation as exc:
            blocked = _blocked_content("postcondition", exc.reason)
            events = _audit_events(inner, prepared.agent_id, prepared.source_run_id)
            await asyncio.to_thread(
                _record_block,
                prepared.agent_id,
                prepared.source_run_id,
                prepared.claim_token,
                phase="postcondition",
                reason=exc.reason,
                content=blocked,
                audit_events=events,
            )
            return FinalizeOutcome(status_code=422, content=blocked, blocked=True)

    persisted = 0
    events = _writeback_events(inner, prepared.agent_id)
    if events:
        persisted = await asyncio.to_thread(
            _persist_events_best_effort, prepared.agent_id, prepared.source_run_id, events
        )
    if prepared.claim_token is not None:
        await asyncio.to_thread(
            _complete_best_effort,
            prepared.agent_id,
            prepared.source_run_id,
            prepared.claim_token,
            status="completed",
            response={"status_code": upstream_status, "content": content},
        )
    return FinalizeOutcome(status_code=upstream_status, content=content, persisted_events=persisted)


async def invoke_in_process(
    agent_id: str,
    body: Any,
    runner: Callable[[Any, CognitionContext | None], Any | Awaitable[Any]],
    *,
    requires_idempotency_key: bool = False,
    idempotency_key: str | None = None,
) -> FinalizeOutcome:
    """Run the full lifecycle around a direct in-process call (no HTTP hop).

    ``runner(input_body, context)`` is the team's callable (sync or async). It
    may return the output alone, or an ``(output, CognitionWriteback)`` pair
    when it has memory events to report.

    Preconditions:
        * ``agent_id`` is non-empty; ``runner`` is callable.
    Postconditions:
        * Returns a :class:`FinalizeOutcome` whose ``status_code``/``content``
          mirror the HTTP proxy's mapping (replay/409/400/503/blocked/200), so
          in-process callers get the identical contract without a transport.
        * An exception from ``runner`` propagates unchanged; the claimed ledger
          row is left to its lease, so a retry after expiry re-executes (the
          run is documented at-least-once on a crash, same as the proxy path).
    """
    assert agent_id, "invoke_in_process: agent_id must be non-empty"
    assert callable(runner), "invoke_in_process: runner must be callable"

    outcome = await prepare_invoke(
        agent_id,
        body,
        requires_idempotency_key=requires_idempotency_key,
        idempotency_key=idempotency_key,
    )
    if outcome.kind is not GateOutcomeKind.PROCEED:
        return _outcome_as_finalized(outcome)
    prepared = outcome.prepared
    assert prepared is not None, "invoke_in_process: PROCEED outcome must carry prepared"

    result = runner(body, prepared.context)
    if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
        result = await result

    writeback: CognitionWriteback | None = None
    output = result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], CognitionWriteback):
        output, writeback = result

    envelope = {
        "output": _jsonable(output),
        "memory_events": [e.model_dump(mode="json") for e in writeback.events] if writeback else [],
        "tool_audit": [],
    }
    return await finalize_invoke(prepared, 200, envelope)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _storage_outage_outcome(
    agent_id: str, requires_idempotency_key: bool, exc: Exception
) -> GateOutcome | None:
    """Map a claim-time storage outage to an outcome, or ``None`` to degrade.

    Postconditions: ``LEDGER_UNAVAILABLE`` for a side-effecting agent (run-once
    cannot be guaranteed, fail closed); ``None`` otherwise — the caller
    proceeds unledgered with a warning logged.
    """
    if requires_idempotency_key:
        return GateOutcome(
            kind=GateOutcomeKind.LEDGER_UNAVAILABLE,
            reason=(
                f"Agent {agent_id!r} requires run-once semantics but the cognition run "
                f"ledger is unavailable: {exc}"
            ),
        )
    logger.warning(
        "cognition: run ledger unavailable for %s; proceeding unledgered (%s)", agent_id, exc
    )
    return None


def _replay_outcome(agent_id: str, source_run_id: str, response: Any) -> GateOutcome:
    """Map a terminal ledger row's stored envelope to a ``REPLAY`` outcome.

    Postconditions: a well-formed stored envelope (``{status_code, content}``)
    replays verbatim; a malformed/missing one degrades to ``CONFLICT`` (never a
    forged 200) with the anomaly logged.
    """
    if isinstance(response, Mapping) and isinstance(response.get("status_code"), int):
        return GateOutcome(
            kind=GateOutcomeKind.REPLAY,
            status_code=response["status_code"],
            content=response.get("content"),
        )
    logger.error(
        "cognition: terminal run %s/%s has no replayable envelope; refusing replay",
        agent_id,
        source_run_id,
    )
    return GateOutcome(
        kind=GateOutcomeKind.CONFLICT,
        reason=(
            f"Run {source_run_id!r} for agent {agent_id!r} already completed but its stored "
            "envelope is unreadable; retry with a new Idempotency-Key."
        ),
    )


def _blocked_content(phase: str, reason: str) -> dict[str, Any]:
    """The 4xx response body for an enforced rule block (FastAPI ``detail`` shape)."""
    return {
        "detail": {
            "message": f"Blocked by cognition {phase}",
            "reason": reason,
            "phase": phase,
        }
    }


def _record_block(
    agent_id: str,
    source_run_id: str,
    claim_token: str | None,
    *,
    phase: str,
    reason: str,
    content: Mapping[str, Any],
    audit_events: list[MemoryEvent] | None = None,
) -> None:
    """Persist a rule block durably: memory event(s) + ``blocked`` ledger envelope.

    Preconditions: ``agent_id``/``source_run_id`` non-empty; ``phase`` is
    ``precondition`` or ``postcondition``.
    Postconditions: best-effort — the trusted audit events (if any) and one
    block event are appended, and the 4xx envelope is stored as ``blocked``
    when the run was claimed; storage failures are logged, never raised (the
    block response must not be masked by a persistence error).
    """
    assert phase in ("precondition", "postcondition"), f"_record_block: bad phase {phase!r}"
    events = list(audit_events or [])
    events.append(
        _gate_event(
            agent_id,
            source_run_id,
            kind=EventKind.ERROR,
            content=f"Invoke blocked by enforced {phase}: {reason}",
            data={"phase": phase, "reason": reason},
            salience=_BLOCK_EVENT_SALIENCE,
            seq=len(events),
        )
    )
    _persist_events_best_effort(agent_id, source_run_id, events)
    if claim_token is not None:
        _complete_best_effort(
            agent_id,
            source_run_id,
            claim_token,
            status="blocked",
            response={"status_code": 422, "content": dict(content)},
        )


def _persist_events_best_effort(
    agent_id: str, source_run_id: str, events: list[MemoryEvent]
) -> int:
    """Persist events via the facade, returning the inserted count (0 on failure)."""
    try:
        return persist_writeback(agent_id, source_run_id, CognitionWriteback(events=events))
    except Exception:
        logger.warning(
            "cognition: writeback persistence failed for %s/%s",
            agent_id,
            source_run_id,
            exc_info=True,
        )
        return 0


def _complete_best_effort(
    agent_id: str,
    source_run_id: str,
    claim_token: str,
    *,
    status: str,
    response: Mapping[str, Any],
) -> None:
    """Complete the ledger row, logging (never raising) on storage/ledger errors."""
    try:
        complete_run(
            agent_id,
            source_run_id,
            status=status,
            response=_jsonable(dict(response)),
            claim_token=claim_token,
        )
    except (AgentCognitionStorageUnavailable, RunLedgerError):
        logger.warning(
            "cognition: ledger completion (%s) failed for %s/%s",
            status,
            agent_id,
            source_run_id,
            exc_info=True,
        )


def _inner_envelope(content: Any, upstream_status: int) -> Any:
    """Unwrap the shim's error framing: non-2xx envelopes ride in ``detail``."""
    if isinstance(content, Mapping) and not (200 <= upstream_status < 300):
        detail = content.get("detail")
        if isinstance(detail, Mapping):
            return detail
    return content


def _writeback_events(inner: Any, agent_id: str) -> list[MemoryEvent]:
    """Validate the envelope's ``memory_events`` dumps into models, dropping junk.

    Postconditions: malformed entries are skipped with a warning — one bad
    event can't fail the invoke or the rest of the batch. (The facade's
    ``persist_writeback`` re-pins ids and sanitizes every field after this.)
    """
    raw = inner.get("memory_events") if isinstance(inner, Mapping) else None
    events: list[MemoryEvent] = []
    for i, item in enumerate(raw if isinstance(raw, list) else []):
        try:
            events.append(MemoryEvent.model_validate(item))
        except Exception:
            logger.warning("cognition: dropping malformed writeback event %d for %s", i, agent_id)
    return events


def _audit_events(inner: Any, agent_id: str, source_run_id: str) -> list[MemoryEvent]:
    """Convert the shim's trusted ``tool_audit`` dumps into ``tool_call`` events.

    Used on the failure paths, where the agent-authored writeback is dropped
    but the out-of-band audit of side effects that actually ran must survive.
    Postconditions: malformed entries are skipped with a warning; ``source_seq``
    is the audit position, so a re-persist of the same audit stays idempotent.
    """
    raw = inner.get("tool_audit") if isinstance(inner, Mapping) else None
    now = _now()
    events: list[MemoryEvent] = []
    for i, item in enumerate(raw if isinstance(raw, list) else []):
        try:
            call = ToolCall.model_validate(item)
        except Exception:
            logger.warning("cognition: dropping malformed tool-audit entry %d for %s", i, agent_id)
            continue
        name = call.tool_id + (f".{call.function}" if call.function else "")
        events.append(
            _gate_event(
                agent_id,
                source_run_id,
                kind=EventKind.TOOL_CALL,
                content=f"tool {name} {'succeeded' if call.ok else 'failed'}",
                data=call.model_dump(mode="json"),
                salience=_AUDIT_EVENT_SALIENCE,
                seq=i,
                occurred_at=call.occurred_at or now,
            )
        )
    return events


def _gate_event(
    agent_id: str,
    source_run_id: str,
    *,
    kind: EventKind,
    content: str,
    data: dict[str, Any],
    salience: float,
    seq: int,
    occurred_at: datetime | None = None,
) -> MemoryEvent:
    """Build a gate-authored event (``persist_writeback`` re-pins id/agent/run)."""
    return MemoryEvent(
        id=str(uuid4()),
        agent_id=agent_id,
        kind=kind,
        content=content,
        data=data,
        salience=salience,
        occurred_at=occurred_at or _now(),
        source_run_id=source_run_id,
        source_seq=seq,
    )


def _outcome_as_finalized(outcome: GateOutcome) -> FinalizeOutcome:
    """Map a non-``PROCEED`` gate outcome onto the proxy's HTTP status contract."""
    if outcome.kind is GateOutcomeKind.REPLAY:
        assert outcome.status_code is not None, "_outcome_as_finalized: replay lacks status"
        return FinalizeOutcome(status_code=outcome.status_code, content=outcome.content)
    if outcome.kind is GateOutcomeKind.BLOCKED:
        return FinalizeOutcome(status_code=422, content=outcome.content, blocked=True)
    status = {
        GateOutcomeKind.CONFLICT: 409,
        GateOutcomeKind.MISSING_IDEMPOTENCY_KEY: 400,
        GateOutcomeKind.LEDGER_UNAVAILABLE: 503,
        GateOutcomeKind.ENVELOPE_TOO_LARGE: 413,
    }[outcome.kind]
    return FinalizeOutcome(status_code=status, content={"detail": outcome.reason})


def _jsonable(value: Any) -> Any:
    """Force ``value`` into plain JSON data (stringifying unknown leaves)."""
    return json.loads(json.dumps(value, default=str))


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)
