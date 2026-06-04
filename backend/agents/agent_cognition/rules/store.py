"""Postgres data-access layer for cognition rules and the HITL proposal queue.

Follows the same idiom as :mod:`agent_cognition.memory.store`:

  * stateless module-level functions (the pool lives in ``shared_postgres``)
  * one public function per operation, decorated with ``@timed_query``
  * synchronous psycopg v3; ``with _conn()`` commits on clean exit and rolls
    back on exception, so there are no explicit ``commit()`` calls
  * ``%s`` positional params; ``Json(...)`` for JSONB columns; rows rebuilt into
    pydantic models via ``model_validate``
  * free functions taking ``agent_id`` first; *every* statement is
    ``agent_id``-filtered

This layer owns the rules CRUD, the deterministic approve/reject of proposals,
and seed-pack install. It is distinct from the rollup→rules handoff helpers
(``flag_rules_needing_review`` / ``flag_stale_proposals``) which live in the
memory store. The shared ``AgentCognitionStorageUnavailable`` is imported from
there so callers catch one exception type across both stores.

Design by Contract:

* **Preconditions** — every call asserts a non-empty ``agent_id``; mutating
  calls additionally assert the row's ``agent_id`` matches and that proposal
  action/target/proposed_rule are coherent.
* **Postconditions** — approve/reject transition exactly one proposal and apply
  the proposal's rule mutation(s) atomically (one connection/transaction): a
  failure rolls the whole thing back, so a rejected approval never half-applies.
  ``install_seed_pack`` is idempotent per ``(agent_id, pack, seed_key)``.
* **Invariant** — no query reads or writes another agent's rows.

When ``POSTGRES_HOST`` is unset, ``_conn`` raises
:class:`AgentCognitionStorageUnavailable` for the API layer to translate into a
clean 503.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4, uuid5

from psycopg.rows import dict_row
from psycopg.types.json import Json
from pydantic import ValidationError

from agent_cognition.memory.store import AgentCognitionStorageUnavailable
from agent_cognition.models import (
    ProposalAction,
    ProposalStatus,
    Rule,
    RuleMode,
    RuleProposal,
    RuleSource,
    RuleStatus,
)
from agent_cognition.rules.predicate import PredicateError, validate_predicate
from agent_cognition.rules.seed_packs import SEED_PACKS
from shared_postgres import get_conn, is_postgres_enabled
from shared_postgres.metrics import timed_query

logger = logging.getLogger(__name__)
_STORE = "agent_cognition"

# Column lists so SELECT results round-trip straight into the models.
_RULE_COLS = (
    "id, agent_id, text, mode, status, predicate, rationale, source, "
    "evidence, needs_review, priority, created_at, updated_at"
)
_PROPOSAL_COLS = (
    "id, agent_id, action, target_rule_id, proposed_rule, evidence, "
    "stale_evidence, status, decided_by, decided_at, created_at"
)


class RuleStoreError(RuntimeError):
    """A rules-store operation was attempted in an invalid state.

    Distinct from :class:`AgentCognitionStorageUnavailable` (infra down): this
    signals a bad-state caller request — approving/rejecting a non-pending
    proposal, a missing retire/amend target, an invalid enforced predicate, or an
    unknown seed pack — that the HTTP layer (a later step) maps to 404/409/422.
    """


# Fixed namespace for deterministic seed-rule ids so install is idempotent and
# concurrency-safe via the primary key (no check-then-insert race).
_SEED_NAMESPACE = UUID("6f3b9e7a-1c2d-4e8f-9a0b-5d6c7e8f9a0b")


def _require_aware(now: datetime | None) -> None:
    """A caller-supplied ``now`` must be timezone-aware (TIMESTAMPTZ columns).

    A naive datetime would be bound against the Postgres session time zone, not
    UTC, silently shifting every stored instant relative to ``_now()`` rows.
    """
    assert now is None or now.tzinfo is not None, "now must be timezone-aware (UTC)"


def _assert_storable_rule(rule: Rule) -> None:
    """An ``enforced`` rule must carry a valid, enforceable predicate.

    The enforcement gate only applies an enforced rule whose predicate declares
    the matching ``phase``; a malformed/missing-phase predicate would be silently
    inert (fail open). Reject it at the write boundary, mirroring the approve gate.
    Advisory rules carry no enforced predicate, so they are not validated.
    """
    if rule.mode == RuleMode.ENFORCED:
        try:
            validate_predicate(rule.predicate)
        except PredicateError as exc:
            raise RuleStoreError(f"enforced rule has an invalid predicate: {exc}") from exc


def _seed_rule_id(agent_id: str, pack_name: str, seed_key: str) -> str:
    """Stable rule id for a seed rule, so re-install collides on the primary key."""
    return str(uuid5(_SEED_NAMESPACE, f"{agent_id}\x00{pack_name}\x00{seed_key}"))


# ---------------------------------------------------------------------------
# Rules — reads/writes
# ---------------------------------------------------------------------------
@timed_query(store=_STORE, op="create_rule")
def create_rule(agent_id: str, rule: Rule) -> None:
    """Insert one rule row.

    Preconditions:
        * ``agent_id`` is non-empty and ``rule.agent_id == agent_id``.
        * ``rule.id`` is caller-minted (the store never mints it).
        * an ``enforced`` rule carries a valid predicate (else
          :class:`RuleStoreError` — an inert enforced rule would fail open).
    Postconditions:
        * A row is inserted with all columns; ``predicate``/``evidence`` persist
          as JSONB and enums as their ``.value``. A duplicate ``id`` surfaces the
          underlying unique violation (callers mint fresh ids).
    """
    assert agent_id, "create_rule: agent_id must be non-empty"
    assert rule.agent_id == agent_id, "create_rule: rule.agent_id must match agent_id"
    _assert_storable_rule(rule)
    with _conn() as conn:
        _insert_rule(conn, rule)


@timed_query(store=_STORE, op="get_rule")
def get_rule(agent_id: str, rule_id: str) -> Rule | None:
    """Return the agent's rule ``rule_id`` or ``None``.

    Preconditions: ``agent_id`` non-empty.
    Postconditions: returns the unique ``(agent_id, id)`` row, or ``None`` (a rule
    owned by another agent is never returned).
    """
    assert agent_id, "get_rule: agent_id must be non-empty"
    with _conn() as conn:
        return _fetch_rule(conn, agent_id, rule_id)


@timed_query(store=_STORE, op="list_rules")
def list_rules(
    agent_id: str,
    status: RuleStatus | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[Rule]:
    """List the agent's rules, newest-and-highest-priority first.

    Preconditions: ``agent_id`` non-empty; ``offset >= 0``; ``limit`` is ``None``
    or ``>= 0``.
    Postconditions: rows owned by ``agent_id``, optionally filtered by ``status``,
    ordered ``(priority DESC, created_at DESC, id ASC)``.
    """
    assert agent_id, "list_rules: agent_id must be non-empty"
    assert offset >= 0, "list_rules: offset must be non-negative"
    assert limit is None or limit >= 0, "list_rules: limit must be non-negative"
    sql = f"SELECT {_RULE_COLS} FROM agent_cognition_rules WHERE agent_id = %s"
    params: list[Any] = [agent_id]
    if status is not None:
        sql += " AND status = %s"
        params.append(status.value)
    sql += " ORDER BY priority DESC, created_at DESC, id ASC"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    if offset:
        sql += " OFFSET %s"
        params.append(offset)
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [Rule.model_validate(row) for row in cur.fetchall()]


@timed_query(store=_STORE, op="list_active_enforced_rules")
def list_active_enforced_rules(agent_id: str) -> list[Rule]:
    """Active enforced rules for ``agent_id`` (the read the enforcement hooks use).

    Preconditions: ``agent_id`` non-empty.
    Postconditions: rows with ``status='active' AND mode='enforced'``, ordered
    ``(priority DESC, id ASC)``.
    """
    assert agent_id, "list_active_enforced_rules: agent_id must be non-empty"
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""SELECT {_RULE_COLS} FROM agent_cognition_rules
                WHERE agent_id = %s AND status = 'active' AND mode = 'enforced'
                ORDER BY priority DESC, id ASC""",
            (agent_id,),
        )
        return [Rule.model_validate(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Proposals — reads/writes
# ---------------------------------------------------------------------------
@timed_query(store=_STORE, op="create_proposal")
def create_proposal(agent_id: str, proposal: RuleProposal) -> None:
    """Insert one rule proposal.

    Preconditions:
        * ``agent_id`` non-empty and ``proposal.agent_id == agent_id``.
        * action/target/proposed_rule are coherent: ``ADD`` ⇒ ``proposed_rule``
          set and ``target_rule_id`` ``None``; ``RETIRE`` ⇒ ``target_rule_id`` set
          and ``proposed_rule`` ``None``; ``AMEND`` ⇒ both set.
    Postconditions:
        * One row is inserted; JSONB columns persist via ``Json(...)`` (a ``None``
          ``proposed_rule`` is SQL ``NULL``, not JSON ``null``).
    """
    assert agent_id, "create_proposal: agent_id must be non-empty"
    assert proposal.agent_id == agent_id, "create_proposal: proposal.agent_id must match agent_id"
    _assert_proposal_coherent(proposal)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO agent_cognition_rule_proposals ({_PROPOSAL_COLS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                proposal.id,
                proposal.agent_id,
                proposal.action.value,
                proposal.target_rule_id,
                Json(proposal.proposed_rule) if proposal.proposed_rule is not None else None,
                Json(proposal.evidence),
                proposal.stale_evidence,
                proposal.status.value,
                proposal.decided_by,
                proposal.decided_at,
                proposal.created_at,
            ),
        )


@timed_query(store=_STORE, op="get_proposal")
def get_proposal(agent_id: str, proposal_id: str) -> RuleProposal | None:
    """Return the agent's proposal ``proposal_id`` or ``None`` (agent-scoped)."""
    assert agent_id, "get_proposal: agent_id must be non-empty"
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_PROPOSAL_COLS} FROM agent_cognition_rule_proposals WHERE agent_id = %s AND id = %s",
            (agent_id, proposal_id),
        )
        row = cur.fetchone()
        return RuleProposal.model_validate(row) if row else None


@timed_query(store=_STORE, op="list_proposals")
def list_proposals(
    agent_id: str,
    status: ProposalStatus | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[RuleProposal]:
    """List the agent's proposals, newest first.

    Preconditions: ``agent_id`` non-empty; ``offset >= 0``; ``limit`` ``None`` or
    ``>= 0``.
    Postconditions: rows owned by ``agent_id``, optionally filtered by ``status``
    (every member is filterable, including the system-only ``superseded``),
    ordered ``(created_at DESC, id ASC)``.
    """
    assert agent_id, "list_proposals: agent_id must be non-empty"
    assert offset >= 0, "list_proposals: offset must be non-negative"
    assert limit is None or limit >= 0, "list_proposals: limit must be non-negative"
    sql = f"SELECT {_PROPOSAL_COLS} FROM agent_cognition_rule_proposals WHERE agent_id = %s"
    params: list[Any] = [agent_id]
    if status is not None:
        sql += " AND status = %s"
        params.append(status.value)
    sql += " ORDER BY created_at DESC, id ASC"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    if offset:
        sql += " OFFSET %s"
        params.append(offset)
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [RuleProposal.model_validate(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Deterministic approve / reject (the HITL decision gate)
# ---------------------------------------------------------------------------
@timed_query(store=_STORE, op="approve_proposal")
def approve_proposal(
    agent_id: str,
    proposal_id: str,
    *,
    decided_by: str,
    now: datetime | None = None,
    new_rule_id: str | None = None,
) -> Rule | None:
    """Approve a pending proposal, applying its action deterministically.

    Preconditions:
        * ``agent_id`` and ``decided_by`` are non-empty.
    Postconditions:
        * Returns ``None`` iff the proposal does not exist for ``agent_id``.
        * Raises :class:`RuleStoreError` if the proposal is not ``pending`` (no
          double-apply), if a retire/amend target is missing/foreign, or if an
          ``enforced`` add/amend has an invalid ``predicate`` — and in every error
          case **nothing is mutated** (the whole transaction rolls back).
        * On success, applies the action in one transaction — ``add`` inserts a
          new ``active`` rule; ``retire`` retires ``target_rule_id``; ``amend``
          retires the target and inserts the replacement ``active`` with lineage
          recorded in ``rationale`` — then sets the proposal ``approved`` with
          ``decided_by``/``decided_at``. Returns the new active rule (add/amend)
          or the retired target (retire).
    """
    assert agent_id, "approve_proposal: agent_id must be non-empty"
    assert decided_by, "approve_proposal: decided_by must be non-empty"
    _require_aware(now)
    decided_at = now or _now()
    with _conn() as conn:
        proposal = _fetch_proposal_for_update(conn, agent_id, proposal_id)
        if proposal is None:
            return None
        if proposal.status != ProposalStatus.PENDING:
            raise RuleStoreError(
                f"proposal {proposal_id!r} is {proposal.status.value}, not pending"
            )
        result = _apply_approval(
            conn, agent_id, proposal, decided_at=decided_at, new_rule_id=new_rule_id
        )
        _decide_proposal(
            conn, agent_id, proposal_id, ProposalStatus.APPROVED, decided_by, decided_at
        )
        return result


@timed_query(store=_STORE, op="reject_proposal")
def reject_proposal(
    agent_id: str,
    proposal_id: str,
    *,
    decided_by: str,
    now: datetime | None = None,
) -> RuleProposal | None:
    """Reject a pending proposal (terminal status ``rejected``).

    Preconditions: ``agent_id`` and ``decided_by`` non-empty.
    Postconditions:
        * Returns ``None`` iff the proposal does not exist for ``agent_id``.
        * Raises :class:`RuleStoreError` if the proposal is not ``pending``.
        * On success sets ``status='rejected'`` (the modeled terminal state — not
          ``archived``) with ``decided_by``/``decided_at``; **no rule rows are
          touched**. Returns the updated proposal.
    """
    assert agent_id, "reject_proposal: agent_id must be non-empty"
    assert decided_by, "reject_proposal: decided_by must be non-empty"
    _require_aware(now)
    decided_at = now or _now()
    with _conn() as conn:
        proposal = _fetch_proposal_for_update(conn, agent_id, proposal_id)
        if proposal is None:
            return None
        if proposal.status != ProposalStatus.PENDING:
            raise RuleStoreError(
                f"proposal {proposal_id!r} is {proposal.status.value}, not pending"
            )
        _decide_proposal(
            conn, agent_id, proposal_id, ProposalStatus.REJECTED, decided_by, decided_at
        )
        return proposal.model_copy(
            update={
                "status": ProposalStatus.REJECTED,
                "decided_by": decided_by,
                "decided_at": decided_at,
            }
        )


def _apply_approval(
    conn: Any,
    agent_id: str,
    proposal: RuleProposal,
    *,
    decided_at: datetime,
    new_rule_id: str | None,
) -> Rule:
    """Apply a proposal's action and return the resulting rule (in-transaction)."""
    if proposal.action == ProposalAction.RETIRE:
        target = _require_target(conn, agent_id, proposal.target_rule_id)
        _set_rule_status(conn, agent_id, target.id, RuleStatus.RETIRED, decided_at)
        retired = _fetch_rule(conn, agent_id, target.id)
        assert retired is not None, "retire target vanished mid-transaction"
        return retired

    # ADD or AMEND: build the replacement rule and validate enforced predicates
    # *before* any write so an invalid predicate leaves everything untouched.
    new_rule = _build_rule_from_spec(
        agent_id, proposal.proposed_rule, rule_id=new_rule_id, now=decided_at
    )
    _assert_storable_rule(new_rule)

    if proposal.action == ProposalAction.AMEND:
        target = _require_target(conn, agent_id, proposal.target_rule_id)
        if new_rule.id == target.id:
            raise RuleStoreError(
                f"amend replacement id {new_rule.id!r} collides with the target rule id"
            )
        _set_rule_status(conn, agent_id, target.id, RuleStatus.RETIRED, decided_at)
        lineage = f"amends {target.id}"
        rationale = f"{lineage}: {new_rule.rationale}" if new_rule.rationale else lineage
        new_rule = new_rule.model_copy(update={"rationale": rationale})

    _insert_rule(conn, new_rule)
    return new_rule


def _build_rule_from_spec(
    agent_id: str, spec: dict[str, Any] | None, *, rule_id: str | None, now: datetime
) -> Rule:
    """Build an ``active`` :class:`Rule` from a proposal's ``proposed_rule`` dict.

    Preconditions: ``spec`` is a dict with a non-empty ``text``.
    Postconditions: returns an active rule for ``agent_id`` (source from ``spec``,
    default ``derived``, timestamps ``now``); raises :class:`RuleStoreError` on a
    malformed spec.
    """
    if not isinstance(spec, dict) or not isinstance(spec.get("text"), str) or not spec.get("text"):
        raise RuleStoreError("proposed_rule must be a dict with a non-empty 'text'")
    try:
        return Rule(
            id=rule_id or str(uuid4()),
            agent_id=agent_id,
            text=spec["text"],
            mode=RuleMode(spec.get("mode", RuleMode.ADVISORY.value)),
            status=RuleStatus.ACTIVE,
            predicate=spec.get("predicate") or {},
            rationale=spec.get("rationale"),
            source=RuleSource(spec.get("source", RuleSource.DERIVED.value)),
            evidence=spec.get("evidence") or [],
            needs_review=False,
            priority=int(spec.get("priority", 0)),
            created_at=now,
            updated_at=now,
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise RuleStoreError(f"invalid proposed_rule: {exc}") from exc


def _require_target(conn: Any, agent_id: str, target_rule_id: str | None) -> Rule:
    """Return the agent-owned **active** target rule, locked for update.

    Locks the target row ``FOR UPDATE`` so two proposals targeting the same rule
    serialize, and requires it to be ``active`` so a second concurrent (or stale)
    approval cannot retire/amend an already-retired rule a second time.
    """
    if not target_rule_id:
        raise RuleStoreError("retire/amend proposal has no target_rule_id")
    target = _fetch_rule(conn, agent_id, target_rule_id, for_update=True)
    if target is None:
        raise RuleStoreError(f"target rule {target_rule_id!r} not found for agent {agent_id!r}")
    if target.status != RuleStatus.ACTIVE:
        raise RuleStoreError(f"target rule {target_rule_id!r} is {target.status.value}, not active")
    return target


# ---------------------------------------------------------------------------
# Seed-pack install
# ---------------------------------------------------------------------------
@timed_query(store=_STORE, op="install_seed_pack")
def install_seed_pack(agent_id: str, pack_name: str, *, now: datetime | None = None) -> list[str]:
    """Install a named seed pack onto an agent, idempotently.

    Preconditions:
        * ``agent_id`` non-empty; ``pack_name`` is a known pack (else
          :class:`RuleStoreError`).
    Postconditions:
        * Each seed rule is inserted ``active`` with ``source='seed'`` under a
          deterministic id derived from ``(agent_id, pack, seed_key)``, via
          ``INSERT … ON CONFLICT (id) DO NOTHING``. Returns the ids actually
          inserted — empty on a re-run. Idempotent and concurrency-safe (the
          primary key, not a check-then-insert, enforces single-install).
    """
    assert agent_id, "install_seed_pack: agent_id must be non-empty"
    _require_aware(now)
    if pack_name not in SEED_PACKS:
        raise RuleStoreError(f"unknown seed pack {pack_name!r}")
    created_at = now or _now()
    new_ids: list[str] = []
    with _conn() as conn:
        for seed in SEED_PACKS[pack_name]:
            rule = Rule(
                id=_seed_rule_id(agent_id, pack_name, seed.key),
                agent_id=agent_id,
                text=seed.text,
                mode=seed.mode,
                status=RuleStatus.ACTIVE,
                predicate=dict(seed.predicate),
                rationale=seed.rationale,
                source=RuleSource.SEED,
                evidence=[{"seed_pack": pack_name, "seed_key": seed.key}],
                needs_review=False,
                priority=seed.priority,
                created_at=created_at,
                updated_at=created_at,
            )
            if _insert_rule(conn, rule, ignore_conflict=True) == 1:
                new_ids.append(rule.id)
    return new_ids


# ---------------------------------------------------------------------------
# Shared row helpers (operate on an open connection — used inside transactions)
# ---------------------------------------------------------------------------
def _insert_rule(conn: Any, rule: Rule, *, ignore_conflict: bool = False) -> int:
    """Insert a rule row; return the number of rows inserted (0 or 1).

    With ``ignore_conflict`` an ``ON CONFLICT (id) DO NOTHING`` makes a duplicate
    primary key a no-op (returns 0) — used by seed install for race-free
    idempotency. Without it a duplicate ``id`` raises the unique violation.
    """
    conflict = " ON CONFLICT (id) DO NOTHING" if ignore_conflict else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO agent_cognition_rules ({_RULE_COLS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s){conflict}""",
            (
                rule.id,
                rule.agent_id,
                rule.text,
                rule.mode.value,
                rule.status.value,
                Json(rule.predicate),
                rule.rationale,
                rule.source.value,
                Json(rule.evidence),
                rule.needs_review,
                rule.priority,
                rule.created_at,
                rule.updated_at,
            ),
        )
        return cur.rowcount


def _fetch_rule(conn: Any, agent_id: str, rule_id: str, *, for_update: bool = False) -> Rule | None:
    sql = f"SELECT {_RULE_COLS} FROM agent_cognition_rules WHERE agent_id = %s AND id = %s"
    if for_update:
        sql += " FOR UPDATE"
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (agent_id, rule_id))
        row = cur.fetchone()
        return Rule.model_validate(row) if row else None


def _set_rule_status(
    conn: Any, agent_id: str, rule_id: str, status: RuleStatus, now: datetime
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_cognition_rules
               SET status = %s, updated_at = %s
               WHERE agent_id = %s AND id = %s""",
            (status.value, now, agent_id, rule_id),
        )
        return cur.rowcount


def _fetch_proposal_for_update(conn: Any, agent_id: str, proposal_id: str) -> RuleProposal | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""SELECT {_PROPOSAL_COLS} FROM agent_cognition_rule_proposals
                WHERE agent_id = %s AND id = %s FOR UPDATE""",
            (agent_id, proposal_id),
        )
        row = cur.fetchone()
        return RuleProposal.model_validate(row) if row else None


def _decide_proposal(
    conn: Any,
    agent_id: str,
    proposal_id: str,
    status: ProposalStatus,
    decided_by: str,
    decided_at: datetime,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_cognition_rule_proposals
               SET status = %s, decided_by = %s, decided_at = %s
               WHERE agent_id = %s AND id = %s""",
            (status.value, decided_by, decided_at, agent_id, proposal_id),
        )


def _assert_proposal_coherent(proposal: RuleProposal) -> None:
    action = proposal.action
    if action == ProposalAction.ADD:
        assert proposal.proposed_rule is not None and proposal.target_rule_id is None, (
            "ADD proposal requires proposed_rule and no target_rule_id"
        )
    elif action == ProposalAction.RETIRE:
        assert proposal.target_rule_id is not None and proposal.proposed_rule is None, (
            "RETIRE proposal requires target_rule_id and no proposed_rule"
        )
    else:  # AMEND
        assert proposal.target_rule_id is not None and proposal.proposed_rule is not None, (
            "AMEND proposal requires both target_rule_id and proposed_rule"
        )


# ---------------------------------------------------------------------------
# Connection seam — mirrors agent_cognition.memory.store._conn so this module's
# own ``is_postgres_enabled`` binding is what tests monkeypatch.
# ---------------------------------------------------------------------------
@contextmanager
def _conn():
    """Yield a pooled connection, translating *acquisition* failures only.

    Preconditions:
        * Postgres is configured (``POSTGRES_HOST`` set).
    Postconditions:
        * Errors raised while *acquiring* the connection surface as
          :class:`AgentCognitionStorageUnavailable`; errors raised inside the
          ``with`` body propagate unchanged (and roll back), so a genuine query
          bug is never masked as an infrastructure outage. Commit-on-success and
          rollback-on-error are delegated to the ``shared_postgres`` pool context.
    """
    if not is_postgres_enabled():
        raise AgentCognitionStorageUnavailable(
            "POSTGRES_HOST is not configured; Agent Cognition storage is unavailable."
        )
    pool_ctx = get_conn()
    try:
        conn = pool_ctx.__enter__()
    except Exception as exc:  # pragma: no cover - pool/connection failure path
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
