"""Reflection (rule learning) for the Agent Cognition Core.

The reflection engine reads an agent's recent memory summaries and its current
rules, then asks an LLM to propose rule changes — add / retire / amend — and
persists them as ``pending`` :class:`~agent_cognition.models.RuleProposal` rows
for human approval (HITL).

It **never activates a rule.** Activation happens only through the operator
approve path (:func:`agent_cognition.rules.store.approve_proposal`); reflection
only ever calls :func:`~agent_cognition.rules.store.create_proposal`. This is the
Milestone B counterpart to the rollup engine: where the rollup turns raw events
into summaries, reflection turns summaries into proposed guardrails.

Reflection does not run rollups itself — the caller (the central scheduler)
sequences ``ensure_rollups_current`` then :func:`reflect`. Reflection only reads
the summaries that already exist.

Robustness contract: the LLM's individual suggestions are untrusted. A malformed
item, a proposal that is incoherent, one that targets a rule that is not
currently active, or one that asks for an enforced rule without a valid predicate
is **dropped and counted**, never raised — one bad suggestion can't poison the
batch or activate anything. Pending proposals whose evidence has gone stale are
superseded before deduping so a fresh proposal can replace them.

Design by Contract: every function documents its Preconditions and
Postconditions. The module is stateless — all durable state lives in the rules
and memory stores.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from agent_cognition.memory import store as memory_store
from agent_cognition.models import (
    PeriodSummary,
    ProposalAction,
    ProposalStatus,
    Rule,
    RuleMode,
    RuleProposal,
    RuleSource,
    RuleStatus,
    Scale,
)
from agent_cognition.rules import store as rules_store
from agent_cognition.rules.predicate import is_valid_predicate
from agent_cognition.runtime_config import read_positive_int
from llm_service import compact_text, complete_validated, get_client

logger = logging.getLogger(__name__)

# Tunables (read at call time so tests/operators can override per environment).
_DEFAULT_SUMMARY_LIMIT = 6
_DEFAULT_MAX_PROPOSALS = 5
_DEFAULT_INPUT_CHARS = 8000

# Bounds of the signed 32-bit `agent_cognition_rules.priority` INTEGER column. A
# model-supplied priority outside this range parses fine but would overflow on
# approval, so reflection drops it at the source (see _build_proposed_rule).
_PG_INT32_MIN = -(2**31)
_PG_INT32_MAX = 2**31 - 1

# Scales fed to the reflector, coarse → fine so the model sees long-range
# context before recent detail. Year is omitted — yearly cadence is too slow to
# drive rule learning and the month/week/day window already spans it.
_REFLECTION_SCALES: tuple[Scale, ...] = (Scale.MONTH, Scale.WEEK, Scale.DAY)

_REFLECTION_SYSTEM_PROMPT = (
    "You are the reflection faculty of an autonomous agent. You read the "
    "agent's recent memory summaries and its current operating rules, then "
    "propose changes to those rules: ADD a new guardrail, RETIRE one that no "
    "longer serves, or AMEND one that should change. Propose a rule only when "
    "the memory shows concrete, recurring evidence for it — repeated failures, "
    "conflicting decisions, an emergent best practice, or a constraint that "
    "would have prevented a real mistake. Do not restate rules that already "
    "exist. Be conservative: an empty proposal list is the correct answer when "
    "nothing in memory warrants a change. Every proposal is reviewed by a human "
    "before it can take effect. Return only the requested JSON object."
)

_TASK_INSTRUCTION = (
    "--- TASK ---\n"
    "Return a single JSON object with one key `proposals`: a list (possibly "
    "empty) of proposed rule changes. Each proposal object has:\n"
    '- `action`: "add", "retire", or "amend".\n'
    "- `target_rule_id`: the id of an EXISTING active rule listed above "
    "(required for retire and amend; omit for add).\n"
    "- `text`: the rule's instruction text (required for add and amend; omit "
    "for retire).\n"
    '- `mode`: "advisory" (prompt guidance) or "enforced" (a hard, '
    'machine-checked pre/postcondition). For add, default "advisory"; for amend, '
    "omit to keep the existing rule's mode.\n"
    '- `predicate`: required only when mode is "enforced" — the rule\'s '
    'machine-checkable predicate as a JSON object {"phase": ..., "check": ...}. '
    "On an amend that keeps an enforced rule enforced, omit it to inherit the "
    "rule's current predicate (shown above); supply it only to change it.\n"
    "- `rationale`: a short why, citing what in the memory motivates it.\n"
    "- `priority`: integer, higher applies first (add default 0; on amend, omit "
    "to keep the existing priority).\n"
    "No other keys."
)


class _ProposedAction(BaseModel):
    """One model-authored proposed rule change (narrow LLM output schema).

    ``action`` is a plain string (not an enum) so a single out-of-range value
    from the model is dropped during materialization rather than failing
    validation of the whole batch. ``mode`` and ``priority`` are optional —
    ``None`` means "unspecified", which on an AMEND inherits the target rule's
    current value (so an ordinary text amend can't silently downgrade an enforced
    guardrail or reset priority) and on an ADD falls back to the default.
    """

    action: str = ""
    target_rule_id: str | None = None
    text: str | None = None
    mode: str | None = None
    predicate: dict[str, Any] | None = None
    rationale: str | None = None
    priority: int | None = None


class _ReflectionResult(BaseModel):
    """Narrow LLM output schema: only the fields the model authors.

    ``proposals`` is kept as a list of raw objects (not ``list[_ProposedAction]``)
    so a single malformed item — e.g. ``{"priority": "high"}`` — is dropped and
    counted on its own during materialization rather than failing validation of
    the whole batch (the untrusted-model contract: one bad suggestion must not
    poison the rest). Each item is validated individually via
    :func:`_coerce_item`. The engine builds the full :class:`RuleProposal` (id,
    agent_id, status, evidence, timestamps) around each item so the model can
    never author store-managed columns or forge its own evidence.
    """

    proposals: list[Any] = Field(default_factory=list)


class ReflectionReport(BaseModel):
    """Telemetry returned by :func:`reflect` (not persisted).

    ``proposed`` counts rows actually inserted; ``dropped_invalid`` counts model
    suggestions rejected by per-item validation or the validity guards;
    ``deduped`` counts suggestions skipped as duplicates of an existing *fresh*
    pending proposal (or of an earlier suggestion in the same batch);
    ``superseded`` counts stale-evidence pending proposals retired this run;
    ``llm_calls`` is ``0`` on the empty-history fast path, otherwise the true
    count of model calls made (the proposal call plus any compaction or
    structured-output retry calls).
    """

    agent_id: str
    proposed: int = 0
    dropped_invalid: int = 0
    deduped: int = 0
    superseded: int = 0
    llm_calls: int = 0


class _CallCountingClient:
    """Wrap an ``LLMClient`` and count every model call routed through it.

    ``compact_text`` may call the model one or more times before the proposal
    call, and ``complete_validated`` may retry on a schema miss; counting all of
    them keeps ``ReflectionReport.llm_calls`` honest. Only ``complete`` and
    ``complete_json`` are intercepted; every other attribute delegates unchanged.

    Invariant: ``calls`` equals the number of ``complete`` + ``complete_json``
    invocations made through this wrapper.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self._inner.complete(*args, **kwargs)

    def complete_json(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self._inner.complete_json(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Reached only for attributes not defined on the wrapper (e.g.
        # get_max_context_tokens); delegate them to the wrapped client.
        return getattr(self._inner, name)


def reflect(agent_id: str, now: datetime) -> ReflectionReport:
    """Propose rule changes from recent memory; persist them as ``pending``.

    Preconditions:
        * ``agent_id`` is non-empty.
        * ``now`` is timezone-aware — it stamps ``created_at`` on a TIMESTAMPTZ
          column, so a naive value (bound against the session zone) is a caller
          bug.
    Postconditions:
        * Reads the agent's recent summaries, active rules, and pending
          proposals. When the agent has no summaries yet, returns a
          zero-``llm_calls`` report having made **no LLM call and no write**.
        * Every proposal written has ``status='pending'`` with
          ``proposed_rule.source='derived'`` and ``(summary_id, version)``
          evidence refs spanning the reflection window; **no rule is created or
          activated** — reflection calls only ``create_proposal``.
        * Pending proposals already flagged ``stale_evidence`` (their evidence
          was superseded by a later summary recompute, so the approve gate
          refuses them) are **superseded** before deduping — otherwise their
          dedupe keys would block a fresh, approvable re-proposal and they would
          linger unapprovable forever.
        * Invalid suggestions (a non-object item, a field that fails per-item
          validation, an unknown action, incoherent action/target/text, a
          retire/amend target that is not an active rule, or an enforced rule
          whose predicate is absent or invalid) are dropped and counted, never
          raised — one bad suggestion cannot poison the batch. Suggestions
          duplicating an existing *fresh* pending proposal — or an earlier
          accepted suggestion this run — are skipped. At most ``_max_proposals()``
          rows are written.
        * Returns a :class:`ReflectionReport` counting the work done.

    Concurrency: the duplicate suppression is best-effort *within a single run*
    — the read of pending proposals and the inserts are not one atomic
    transaction, so two reflection runs racing for the **same** ``agent_id`` can
    each insert the same proposal. The duplicate is low-harm and self-healing (an
    operator rejects it, and once either is decided it drops out of the pending
    set), so the caller is expected to single-flight reflection per agent rather
    than this function locking — the scheduler owns run cadence, mirroring how the
    rollup engine relies on the scheduler plus idempotency. Reflection across
    *different* agents is always independent (every store read/write is
    ``agent_id``-scoped).
    """
    assert agent_id, "reflect: agent_id must be non-empty"
    assert now.tzinfo is not None, "reflect: now must be timezone-aware (UTC)"

    report = ReflectionReport(agent_id=agent_id)

    # Retire stale-evidence pending proposals first. This is recompute cleanup that
    # needs no model output or fresh memory, so it runs before *any* fast-path
    # return (all-stale history below, or an LLM outage during _propose) — an
    # unapprovable stale row must never be stranded in the HITL queue waiting on a
    # rollup or the provider. Only *fresh* pending proposals gate the dedupe.
    pending = rules_store.list_proposals(agent_id, status=ProposalStatus.PENDING)
    fresh_pending: list[RuleProposal] = []
    for proposal in pending:
        if proposal.stale_evidence:
            rules_store.supersede_proposal(agent_id, proposal.id, now=now)
            report.superseded += 1
        else:
            fresh_pending.append(proposal)

    summaries = _recent_summaries(agent_id)
    if not summaries:
        return report  # no fresh memory → cleanup done, but no LLM call / proposals

    active_rules = rules_store.list_rules(agent_id, status=RuleStatus.ACTIVE)

    # Count *every* model call (compaction can call the LLM before the proposal
    # call, and structured-output validation can retry) so the report reflects
    # true usage for schedulers/budgets/observability rather than a hard-coded 1.
    llm = _CallCountingClient(get_client("cognition"))
    graph_block = _graph_grounding_block(agent_id, summaries)
    result = _propose(summaries, active_rules, llm, graph_block=graph_block)
    report.llm_calls = llm.calls

    evidence = [{"summary_id": s.id, "version": s.version} for s in summaries]
    active_by_id = {r.id: r for r in active_rules}
    # Two distinct suppressions, deliberately separated:
    #  * ``seen`` is the full proposed-change identity (incl. priority) — an
    #    identical pending proposal is deduped, but a priority-only correction is
    #    a different change and still reaches review.
    #  * ``active_content`` suppresses an ADD that restates an *active* rule's
    #    content (text/mode/predicate, priority-independent) so approval can't
    #    activate a duplicate guardrail; re-prioritizing an existing rule is an
    #    amend, never a duplicate add.
    seen = {_dedupe_key_of(p) for p in fresh_pending}
    active_content = {_rule_content_id(r) for r in active_rules}
    cap = _max_proposals()

    for raw in result.proposals:
        if report.proposed >= cap:
            break
        item = _coerce_item(raw)
        if item is None:
            report.dropped_invalid += 1
            continue
        proposal = _materialize(agent_id, item, active_by_id, evidence, now)
        if proposal is None:
            report.dropped_invalid += 1
            continue
        key = _dedupe_key_of(proposal)
        if (
            key in seen
            or _restates_active_rule(proposal, active_content)
            or _is_noop_amend(proposal, active_by_id)
        ):
            report.deduped += 1
            continue
        rules_store.create_proposal(agent_id, proposal)
        seen.add(key)
        report.proposed += 1

    return report


def _coerce_item(raw: Any) -> _ProposedAction | None:
    """Validate one raw LLM proposal object into a :class:`_ProposedAction`.

    Postconditions: returns the parsed action, or ``None`` when ``raw`` is not an
    object or any field fails validation (e.g. a non-numeric ``priority``). Never
    raises — a single malformed item is the caller's to drop and count, so it
    cannot fail the whole batch.
    """
    if not isinstance(raw, dict):
        logger.warning("reflection: proposal item is not an object; dropping")
        return None
    try:
        return _ProposedAction.model_validate(raw)
    except ValidationError as exc:
        logger.warning("reflection: proposal item failed validation (%s); dropping", exc)
        return None


# ---------------------------------------------------------------------------
# Input gathering + LLM call
# ---------------------------------------------------------------------------
def _recent_summaries(agent_id: str) -> list[PeriodSummary]:
    """Gather the agent's most recent *fresh* summaries across reflection scales.

    Postconditions: returns up to ``_summary_limit()`` summaries per scale
    (month, then week, then day), most-recent first within each scale, **excluding
    any summary flagged ``stale``**. A stale summary's text is outdated pending the
    rollup's recompute, yet ``mark_period_stale`` has already bumped its
    ``version``; citing it as evidence would produce a proposal the post-recompute
    evidence check never flags (it flags only versions *below* the recomputed one,
    and the recompute keeps the bumped version), leaving an approvable proposal
    derived from stale memory. Staleness is filtered in the store query (before the
    limit), so ``limit`` fresh summaries are returned even when the most recent
    periods are stale but older ones are fresh. The scheduler runs
    ``ensure_rollups_current`` before reflection, so fresh summaries are normally
    available; if no fresh summary exists this returns empty and reflection defers
    to the next run.
    """
    limit = _summary_limit()
    gathered: list[PeriodSummary] = []
    for scale in _REFLECTION_SCALES:
        gathered.extend(
            memory_store.fetch_summaries(agent_id, scale, limit=limit, exclude_stale=True)
        )
    return gathered


def _propose(
    summaries: list[PeriodSummary],
    active_rules: list[Rule],
    llm: Any,
    *,
    graph_block: str = "",
) -> _ReflectionResult:
    """Render the inputs, bound them to budget, and ask the LLM to propose.

    Preconditions: ``summaries`` is non-empty.
    Postconditions: returns a validated :class:`_ReflectionResult` (whose
    proposal list may be empty); the input is compacted to the configured char
    budget before the call so a long memory window can't overflow the model. When
    ``graph_block`` is non-empty it is prepended as additional grounding context —
    additive only, it never changes what the engine writes (still only ``pending``
    proposals via ``create_proposal``).
    """
    text = f"{_render_summaries_text(summaries)}\n\n{_render_rules_text(active_rules)}"
    if graph_block:
        text = f"{graph_block}\n\n{text}"
    bounded = compact_text(
        text, _input_char_budget(), llm, content_description="agent memory and rules"
    )
    prompt = f"{bounded}\n\n{_TASK_INSTRUCTION}"
    return complete_validated(
        llm,
        prompt,
        schema=_ReflectionResult,
        system_prompt=_REFLECTION_SYSTEM_PROMPT,
        temperature=0.0,
        correction_attempts=1,
    )


def _graph_grounding_block(agent_id: str, summaries: list[PeriodSummary]) -> str:
    """Best-effort knowledge-graph grounding for reflection, or ``""``.

    Returns a ``## Related knowledge (from graph)`` block of recency-ranked facts
    related to the agent's recent memory, or ``""`` when the graph layer is
    disabled, the agent opted out of grounding, or anything fails. The graph is a
    read-only input here — it never proposes or activates a rule.

    Postconditions:
        * ``""`` unless ``is_neo4j_enabled()`` **and** the agent's manifest enables
          ``knowledge_graph.ground_rule_proposals``; never raises.
    """
    try:
        from shared_neo4j import is_neo4j_enabled  # noqa: PLC0415

        if not is_neo4j_enabled():
            return ""
        from agent_cognition.manifest_scope import ground_rule_proposals  # noqa: PLC0415

        if not ground_rule_proposals(agent_id):
            return ""
        from agent_cognition.graph.bridge import run_sync  # noqa: PLC0415
        from agent_cognition.graph.retrieval import build_graph_context  # noqa: PLC0415

        # Query the graph with the agent's recent memory text so the search
        # surfaces facts related to what reflection is reasoning over.
        query = _render_summaries_text(summaries)
        block = run_sync(build_graph_context(agent_id, query), default="")
        if not block:
            return ""
        return block.replace("## Knowledge graph", "## Related knowledge (from graph)", 1)
    except Exception:
        logger.warning("reflection: graph grounding failed; proceeding ungrounded", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# Validation + materialization (pure)
# ---------------------------------------------------------------------------
def _materialize(
    agent_id: str,
    item: _ProposedAction,
    active_by_id: dict[str, Rule],
    evidence: list[dict[str, Any]],
    now: datetime,
) -> RuleProposal | None:
    """Validate one proposed action and build a pending ``RuleProposal``.

    Postconditions: returns a coherent ``pending`` proposal, or ``None`` (the
    caller counts a drop) when the action is unknown, the action/target/text are
    incoherent, a retire/amend target is not an active rule for this agent, or an
    enforced rule's predicate is absent/invalid. Never raises on model output.
    """
    try:
        action = ProposalAction(item.action)
    except ValueError:
        logger.warning("reflection: unknown proposal action %r; dropping", item.action)
        return None

    if action == ProposalAction.RETIRE:
        if not _is_active_target(item.target_rule_id, active_by_id):
            logger.warning(
                "reflection: retire targets non-active rule %r; dropping", item.target_rule_id
            )
            return None
        return _build_proposal(agent_id, action, evidence, now, target_rule_id=item.target_rule_id)

    # An ADD that carries a target is incoherent (the model likely meant amend);
    # drop it rather than silently discarding the target and forking a new rule.
    if action == ProposalAction.ADD and item.target_rule_id:
        logger.warning(
            "reflection: add proposal carries target_rule_id %r (incoherent); dropping",
            item.target_rule_id,
        )
        return None

    # ADD or AMEND require rule text; AMEND additionally needs an active target.
    if not item.text or not item.text.strip():
        logger.warning("reflection: %s proposal missing rule text; dropping", action.value)
        return None
    if action == ProposalAction.AMEND and not _is_active_target(item.target_rule_id, active_by_id):
        logger.warning(
            "reflection: amend targets non-active rule %r; dropping", item.target_rule_id
        )
        return None

    # For an amend the target is guaranteed active (checked above); pass it so
    # _build_proposed_rule can inherit unspecified mode/predicate/priority.
    target_rule = active_by_id.get(item.target_rule_id) if action == ProposalAction.AMEND else None
    proposed_rule = _build_proposed_rule(item, target_rule)
    if proposed_rule is None:
        return None  # invalid/absent enforced predicate or out-of-range priority

    target = item.target_rule_id if action == ProposalAction.AMEND else None
    return _build_proposal(
        agent_id, action, evidence, now, target_rule_id=target, proposed_rule=proposed_rule
    )


def _is_active_target(target_rule_id: str | None, active_by_id: dict[str, Rule]) -> bool:
    """A retire/amend target must name a rule currently active for the agent."""
    return bool(target_rule_id) and target_rule_id in active_by_id


def _build_proposed_rule(
    item: _ProposedAction, target_rule: Rule | None = None
) -> dict[str, Any] | None:
    """Build the ``proposed_rule`` dict consumed by the store's approve path.

    Emits only the keys ``store._build_rule_from_spec`` reads and stamps
    ``source='derived'``. Unspecified (``None``) ``mode``/``priority`` and an
    omitted enforced predicate are **inherited from** ``target_rule`` on an amend
    (else defaulted), so an ordinary text amend can't silently downgrade an
    enforced guardrail to advisory, drop for a missing predicate, or reset
    priority. Returns ``None`` (the caller counts a drop) when the resolved rule
    is enforced but its predicate is absent or fails :func:`is_valid_predicate`,
    or the resolved ``priority`` is outside the signed 32-bit range of the
    ``agent_cognition_rules.priority`` INTEGER column — either could never be
    approved (the approve gate re-validates / the DB write would overflow), so it
    is dropped at the source rather than parked unapprovable in the queue.

    Preconditions: when ``item`` is an amend, ``target_rule`` is its active target.
    """
    raw_mode = (
        item.mode
        if item.mode is not None
        else (target_rule.mode.value if target_rule else RuleMode.ADVISORY.value)
    )
    try:
        mode = RuleMode(raw_mode)
    except ValueError:
        logger.warning("reflection: unknown rule mode %r; dropping", raw_mode)
        return None
    priority = (
        item.priority if item.priority is not None else (target_rule.priority if target_rule else 0)
    )
    if not (_PG_INT32_MIN <= priority <= _PG_INT32_MAX):
        logger.warning(
            "reflection: priority %d is outside the INT32 rule-column range; dropping",
            priority,
        )
        return None
    spec: dict[str, Any] = {
        "text": item.text,
        "mode": mode.value,
        "source": RuleSource.DERIVED.value,
        "priority": priority,
    }
    if item.rationale:
        spec["rationale"] = item.rationale
    if mode == RuleMode.ENFORCED:
        # Inherit the target's predicate when the model omits it on an amend that
        # keeps the rule enforced; otherwise the model must supply a valid one.
        predicate = item.predicate
        if predicate is None and target_rule is not None and target_rule.mode == RuleMode.ENFORCED:
            predicate = target_rule.predicate
        if not isinstance(predicate, dict) or not is_valid_predicate(predicate):
            logger.warning(
                "reflection: enforced proposal has an absent/invalid predicate; dropping"
            )
            return None
        spec["predicate"] = predicate
    return spec


def _build_proposal(
    agent_id: str,
    action: ProposalAction,
    evidence: list[dict[str, Any]],
    now: datetime,
    *,
    target_rule_id: str | None = None,
    proposed_rule: dict[str, Any] | None = None,
) -> RuleProposal:
    """Assemble a ``pending`` :class:`RuleProposal` around model-authored fields."""
    return RuleProposal(
        id=str(uuid4()),
        agent_id=agent_id,
        action=action,
        target_rule_id=target_rule_id,
        proposed_rule=proposed_rule,
        evidence=list(evidence),
        stale_evidence=False,
        status=ProposalStatus.PENDING,
        decided_by=None,
        decided_at=None,
        created_at=now,
    )


def _normalize_text(text: str) -> str:
    """Whitespace-collapsed, case-folded rule text for stable dedupe identity."""
    return " ".join(text.split()).casefold()


def _predicate_fingerprint(predicate: Any) -> str | None:
    """Stable identity of an enforced rule's predicate for dedupe.

    Returns canonical JSON (sorted keys) for a non-empty predicate dict, else
    ``None`` — advisory rules carry no predicate, so they fingerprint to ``None``.
    """
    if isinstance(predicate, dict) and predicate:
        return json.dumps(predicate, sort_keys=True, separators=(",", ":"))
    return None


# (action, target_rule_id, normalized_text, mode, predicate_fingerprint, priority)
_DedupeKey = tuple[str, str | None, str | None, str | None, str | None, int | None]
# (normalized_text, mode, predicate_fingerprint) — priority-independent rule content
_ContentId = tuple[str, str, str | None]


def _dedupe_key_of(proposal: RuleProposal) -> _DedupeKey:
    """Identity used to suppress re-proposing the same change.

    Two proposals collide when they share an action, a target rule, and — for
    add/amend — the same normalized text, ``mode``, (enforced only) predicate, and
    ``priority``. ``mode``/predicate are part of the identity so an advisory and an
    enforced rule with identical text are *not* duplicates (different runtime
    behavior); ``priority`` is included because it is persisted on approval and
    affects rule ordering, so a proposal that only corrects priority is a distinct
    change that must still reach review. Retire proposals carry no rule body, so
    they collide on ``(action, target, None, None, None, None)``.
    """
    text: str | None = None
    mode: str | None = None
    predicate_fp: str | None = None
    priority: int | None = None
    if isinstance(proposal.proposed_rule, dict):
        candidate = proposal.proposed_rule.get("text")
        if isinstance(candidate, str):
            text = _normalize_text(candidate)
        mode = proposal.proposed_rule.get("mode") or RuleMode.ADVISORY.value
        predicate_fp = _predicate_fingerprint(proposal.proposed_rule.get("predicate"))
        priority = proposal.proposed_rule.get("priority")
    return (proposal.action.value, proposal.target_rule_id, text, mode, predicate_fp, priority)


def _rule_content_id(rule: Rule) -> _ContentId:
    """Priority-independent content identity of an active rule for ADD suppression.

    An ADD whose text/mode/predicate match an active rule restates an existing
    guardrail (the untrusted model ignoring the prompt's "do not restate"
    instruction). Priority is intentionally excluded: re-prioritizing an existing
    rule is an *amend*, never a duplicate add, so an ADD at any priority is still
    suppressed. Mode/predicate are included so an *enforced* guardrail is never
    suppressed by an *advisory* rule of the same text.
    """
    return (_normalize_text(rule.text), rule.mode.value, _predicate_fingerprint(rule.predicate))


def _restates_active_rule(proposal: RuleProposal, active_content: set[_ContentId]) -> bool:
    """True when ``proposal`` is an ADD restating an active rule's content."""
    if proposal.action != ProposalAction.ADD or not isinstance(proposal.proposed_rule, dict):
        return False
    text = proposal.proposed_rule.get("text")
    if not isinstance(text, str):
        return False
    mode = proposal.proposed_rule.get("mode") or RuleMode.ADVISORY.value
    predicate_fp = _predicate_fingerprint(proposal.proposed_rule.get("predicate"))
    return (_normalize_text(text), mode, predicate_fp) in active_content


def _is_noop_amend(proposal: RuleProposal, active_by_id: dict[str, Rule]) -> bool:
    """True when an AMEND would replace its target with identical content.

    The resolved text/mode/predicate/priority all equal the active target's, so
    approving it would retire the rule and insert an identical replacement —
    needless churn and lineage/evidence change for no behavior change. Such a
    no-op is suppressed (counted ``deduped``) before it reaches the queue.
    """
    if proposal.action != ProposalAction.AMEND or not isinstance(proposal.proposed_rule, dict):
        return False
    target = active_by_id.get(proposal.target_rule_id)
    if target is None:
        return False
    pr = proposal.proposed_rule
    return (
        _normalize_text(pr.get("text") or "") == _normalize_text(target.text)
        and (pr.get("mode") or RuleMode.ADVISORY.value) == target.mode.value
        and _predicate_fingerprint(pr.get("predicate")) == _predicate_fingerprint(target.predicate)
        and pr.get("priority") == target.priority
    )


# ---------------------------------------------------------------------------
# Rendering helpers (pure)
# ---------------------------------------------------------------------------
def _render_summaries_text(summaries: list[PeriodSummary]) -> str:
    """One line per summary: scale, period start, summary, highlights."""
    lines = ["## Recent memory summaries"]
    for s in summaries:
        suffix = f" | highlights: {'; '.join(str(h) for h in s.highlights)}" if s.highlights else ""
        lines.append(f"[{s.scale.value} {s.period_start.date().isoformat()}] {s.summary}{suffix}")
    return "\n".join(lines)


def _render_rules_text(rules: list[Rule]) -> str:
    """One line per active rule, including its id so the model can target it.

    Enforced rules also render their predicate so an amend can keep (or knowingly
    change) it instead of guessing — without it the model can only see the text
    and would drop the amend for a missing predicate or downgrade it to advisory.
    """
    if not rules:
        return "## Current rules\n(none)"
    lines = ["## Current rules"]
    for r in rules:
        suffix = ""
        if r.mode == RuleMode.ENFORCED and r.predicate:
            suffix = f" predicate={json.dumps(r.predicate, sort_keys=True, separators=(',', ':'))}"
        lines.append(f"- id={r.id} [{r.mode.value}, priority={r.priority}] {r.text}{suffix}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Env-backed tunables
# ---------------------------------------------------------------------------
def _summary_limit() -> int:
    """Summaries per scale fed to the reflector (env ``AGENT_COGNITION_REFLECTION_SUMMARY_LIMIT``)."""
    return read_positive_int("AGENT_COGNITION_REFLECTION_SUMMARY_LIMIT", _DEFAULT_SUMMARY_LIMIT)


def _max_proposals() -> int:
    """Cap on proposals written per run (env ``AGENT_COGNITION_REFLECTION_MAX_PROPOSALS``)."""
    return read_positive_int("AGENT_COGNITION_REFLECTION_MAX_PROPOSALS", _DEFAULT_MAX_PROPOSALS)


def _input_char_budget() -> int:
    """Char budget for ``compact_text`` (env ``AGENT_COGNITION_REFLECTION_INPUT_CHARS``)."""
    return read_positive_int("AGENT_COGNITION_REFLECTION_INPUT_CHARS", _DEFAULT_INPUT_CHARS)
