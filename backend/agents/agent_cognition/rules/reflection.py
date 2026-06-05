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

import logging
import os
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
from llm_service import compact_text, complete_validated, get_client

logger = logging.getLogger(__name__)

# Tunables (read at call time so tests/operators can override per environment).
_DEFAULT_SUMMARY_LIMIT = 6
_DEFAULT_MAX_PROPOSALS = 5
_DEFAULT_INPUT_CHARS = 8000

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
    'machine-checked pre/postcondition); default "advisory".\n'
    '- `predicate`: required only when mode is "enforced" — the rule\'s '
    'machine-checkable predicate as a JSON object {"phase": ..., "check": ...}.\n'
    "- `rationale`: a short why, citing what in the memory motivates it.\n"
    "- `priority`: integer, higher applies first (default 0).\n"
    "No other keys."
)


class _ProposedAction(BaseModel):
    """One model-authored proposed rule change (narrow LLM output schema).

    ``action``/``mode`` are plain strings (not enums) so a single out-of-range
    value from the model is dropped during materialization rather than failing
    validation of the whole batch.
    """

    action: str = ""
    target_rule_id: str | None = None
    text: str | None = None
    mode: str = RuleMode.ADVISORY.value
    predicate: dict[str, Any] | None = None
    rationale: str | None = None
    priority: int = 0


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
    ``llm_calls`` is ``0`` on the empty-history fast path and ``1`` otherwise.
    """

    agent_id: str
    proposed: int = 0
    dropped_invalid: int = 0
    deduped: int = 0
    superseded: int = 0
    llm_calls: int = 0


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
    """
    assert agent_id, "reflect: agent_id must be non-empty"
    assert now.tzinfo is not None, "reflect: now must be timezone-aware (UTC)"

    report = ReflectionReport(agent_id=agent_id)

    summaries = _recent_summaries(agent_id)
    if not summaries:
        return report  # empty history → no LLM call, no proposals

    active_rules = rules_store.list_rules(agent_id, status=RuleStatus.ACTIVE)
    pending = rules_store.list_proposals(agent_id, status=ProposalStatus.PENDING)

    llm = get_client("cognition")
    result = _propose(summaries, active_rules, llm)
    report.llm_calls = 1

    # Retire stale-evidence pending proposals so a fresh proposal can replace
    # them; only *fresh* pending proposals gate the dedupe below.
    fresh_pending: list[RuleProposal] = []
    for proposal in pending:
        if proposal.stale_evidence:
            rules_store.supersede_proposal(agent_id, proposal.id, now=now)
            report.superseded += 1
        else:
            fresh_pending.append(proposal)

    evidence = [{"summary_id": s.id, "version": s.version} for s in summaries]
    active_by_id = {r.id: r for r in active_rules}
    seen = {_dedupe_key_of(p) for p in fresh_pending}
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
        if key in seen:
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
    """Gather the agent's most recent summaries across the reflection scales.

    Postconditions: returns up to ``_summary_limit()`` summaries per scale
    (month, then week, then day), most-recent first within each scale; empty
    when the agent has no summaries yet.
    """
    limit = _summary_limit()
    gathered: list[PeriodSummary] = []
    for scale in _REFLECTION_SCALES:
        gathered.extend(memory_store.fetch_summaries(agent_id, scale, limit=limit))
    return gathered


def _propose(
    summaries: list[PeriodSummary], active_rules: list[Rule], llm: Any
) -> _ReflectionResult:
    """Render the inputs, bound them to budget, and ask the LLM to propose.

    Preconditions: ``summaries`` is non-empty.
    Postconditions: returns a validated :class:`_ReflectionResult` (whose
    proposal list may be empty); the input is compacted to the configured char
    budget before the call so a long memory window can't overflow the model.
    """
    text = f"{_render_summaries_text(summaries)}\n\n{_render_rules_text(active_rules)}"
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

    # ADD or AMEND require rule text; AMEND additionally needs an active target.
    if not item.text or not item.text.strip():
        logger.warning("reflection: %s proposal missing rule text; dropping", action.value)
        return None
    if action == ProposalAction.AMEND and not _is_active_target(item.target_rule_id, active_by_id):
        logger.warning(
            "reflection: amend targets non-active rule %r; dropping", item.target_rule_id
        )
        return None

    proposed_rule = _build_proposed_rule(item)
    if proposed_rule is None:
        return None  # invalid/absent enforced predicate (already logged)

    target = item.target_rule_id if action == ProposalAction.AMEND else None
    return _build_proposal(
        agent_id, action, evidence, now, target_rule_id=target, proposed_rule=proposed_rule
    )


def _is_active_target(target_rule_id: str | None, active_by_id: dict[str, Rule]) -> bool:
    """A retire/amend target must name a rule currently active for the agent."""
    return bool(target_rule_id) and target_rule_id in active_by_id


def _build_proposed_rule(item: _ProposedAction) -> dict[str, Any] | None:
    """Build the ``proposed_rule`` dict consumed by the store's approve path.

    Emits only the keys ``store._build_rule_from_spec`` reads and stamps
    ``source='derived'``. Returns ``None`` when the model proposes an enforced
    rule whose predicate is absent or fails :func:`is_valid_predicate` — such a
    proposal could never be approved (the approve gate re-validates), so it is
    dropped at the source rather than parked unapprovable in the queue.
    """
    try:
        mode = RuleMode(item.mode)
    except ValueError:
        logger.warning("reflection: unknown rule mode %r; dropping", item.mode)
        return None
    spec: dict[str, Any] = {
        "text": item.text,
        "mode": mode.value,
        "source": RuleSource.DERIVED.value,
        "priority": item.priority,
    }
    if item.rationale:
        spec["rationale"] = item.rationale
    if mode == RuleMode.ENFORCED:
        if not isinstance(item.predicate, dict) or not is_valid_predicate(item.predicate):
            logger.warning(
                "reflection: enforced proposal has an absent/invalid predicate; dropping"
            )
            return None
        spec["predicate"] = item.predicate
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


def _dedupe_key_of(proposal: RuleProposal) -> tuple[str, str | None, str | None]:
    """Identity used to suppress re-proposing the same change.

    Two proposals collide when they share an action, a target rule, and (for
    add/amend) the same whitespace-normalized, case-folded rule text. Retire
    proposals carry no text, so they collide on ``(action, target, None)``.
    """
    text: str | None = None
    if isinstance(proposal.proposed_rule, dict):
        candidate = proposal.proposed_rule.get("text")
        if isinstance(candidate, str):
            text = " ".join(candidate.split()).casefold()
    return (proposal.action.value, proposal.target_rule_id, text)


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
    """One line per active rule, including its id so the model can target it."""
    if not rules:
        return "## Current rules\n(none)"
    lines = ["## Current rules"]
    for r in rules:
        lines.append(f"- id={r.id} [{r.mode.value}, priority={r.priority}] {r.text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Env-backed tunables
# ---------------------------------------------------------------------------
def _summary_limit() -> int:
    """Summaries per scale fed to the reflector (env ``AGENT_COGNITION_REFLECTION_SUMMARY_LIMIT``)."""
    return _read_positive_int("AGENT_COGNITION_REFLECTION_SUMMARY_LIMIT", _DEFAULT_SUMMARY_LIMIT)


def _max_proposals() -> int:
    """Cap on proposals written per run (env ``AGENT_COGNITION_REFLECTION_MAX_PROPOSALS``)."""
    return _read_positive_int("AGENT_COGNITION_REFLECTION_MAX_PROPOSALS", _DEFAULT_MAX_PROPOSALS)


def _input_char_budget() -> int:
    """Char budget for ``compact_text`` (env ``AGENT_COGNITION_REFLECTION_INPUT_CHARS``)."""
    return _read_positive_int("AGENT_COGNITION_REFLECTION_INPUT_CHARS", _DEFAULT_INPUT_CHARS)


def _read_positive_int(name: str, default: int) -> int:
    """Parse a positive int env var, falling back to ``default``.

    Postconditions: returns the parsed value when ``>= 1``; unset/garbage/
    non-positive values fall back to ``default``.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default
