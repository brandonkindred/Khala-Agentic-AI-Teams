"""Tests for the cognition reflection engine (Step 6).

Two layers, mirroring the rest of the package:

* **Pure** tests of validation, materialization, rendering, env parsing, and the
  ``reflect`` orchestration run with no Postgres — the stores and the LLM client
  are monkeypatched.
* **Live-Postgres** tests exercise the real rules/memory stores end-to-end and
  are skipped automatically when ``POSTGRES_HOST`` is unset, using the same
  schema-provision + truncate autouse fixture as the other store tests and a
  canned (fake) LLM client.

The load-bearing invariant under test: reflection only ever writes ``pending``
proposals — it never creates or activates a rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

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
from agent_cognition.postgres import SCHEMA
from agent_cognition.rules import reflection, store
from llm_service.interface import LLMClient
from shared.postgres import is_postgres_enabled, register_team_schemas
from shared.postgres.testing import truncate_team_tables

_UTC = timezone.utc
_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=_UTC)

# A valid enforced predicate (precondition phase) used across tests.
_GOOD_PREDICATE = {"phase": "precondition", "check": {"op": "==", "path": "input.x", "value": 1}}
_BAD_PREDICATE = {"phase": "nonsense", "check": {"op": "??", "path": "input.x"}}


def _dt(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Fake LLM client — deterministic, records calls.
# ---------------------------------------------------------------------------
class CannedLLM(LLMClient):
    """Returns a fixed proposal list; records prompts for assertions."""

    def __init__(self, proposals: list[dict[str, Any]] | None = None) -> None:
        self._proposals = proposals if proposals is not None else []
        self.json_calls: list[dict[str, Any]] = []
        self.text_calls: list[dict[str, Any]] = []

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: str | None = None,
        tools: list | None = None,
        think: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.json_calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "think": think,
                "temperature": temperature,
            }
        )
        # Returned verbatim (no per-item ``dict()`` copy) so a non-object item can
        # flow through to exercise reflection's drop-malformed-item path.
        return {"proposals": list(self._proposals)}

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        system_prompt: str | None = None,
        tools: list | None = None,
        think: bool = False,
        objective: str,
        **kwargs: object,
    ) -> str:
        # Used by compact_text when the input is over budget, and by
        # _propose's think=True reasoning pass before JSON formatting.
        self.text_calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "think": think,
                "objective": objective,
                "temperature": temperature,
            }
        )
        return "COMPACTED"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _summary(
    agent_id: str = "a",
    *,
    sid: str | None = None,
    scale: Scale = Scale.DAY,
    version: int = 1,
    summary: str = "did work; one task failed twice",
    start: datetime | None = None,
    highlights: list | None = None,
    stale: bool = False,
) -> PeriodSummary:
    start = start or _dt(2026, 5, 1)
    return PeriodSummary(
        id=sid or str(uuid4()),
        agent_id=agent_id,
        scale=scale,
        period_start=start,
        period_end=start + timedelta(days=1),
        summary=summary,
        highlights=["retry storm on task-7"] if highlights is None else highlights,
        source_count=5,
        version=version,
        stale=stale,
        created_at=start,
    )


def _rule(
    agent_id: str = "a",
    *,
    rid: str | None = None,
    text: str = "always lint before merge",
    mode: RuleMode = RuleMode.ADVISORY,
    status: RuleStatus = RuleStatus.ACTIVE,
    priority: int = 0,
    predicate: dict[str, Any] | None = None,
) -> Rule:
    return Rule(
        id=rid or str(uuid4()),
        agent_id=agent_id,
        text=text,
        mode=mode,
        status=status,
        predicate=predicate or {},
        rationale=None,
        source=RuleSource.OPERATOR,
        evidence=[],
        needs_review=False,
        priority=priority,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _pending(
    agent_id: str = "a",
    *,
    action: ProposalAction = ProposalAction.ADD,
    target_rule_id: str | None = None,
    text: str | None = "be kind",
    priority: int = 0,
    stale_evidence: bool = False,
) -> RuleProposal:
    # Mirror what _build_proposed_rule persists (incl. priority) so dedupe keys match.
    proposed = (
        {"text": text, "mode": "advisory", "source": "derived", "priority": priority}
        if text is not None
        else None
    )
    return RuleProposal(
        id=str(uuid4()),
        agent_id=agent_id,
        action=action,
        target_rule_id=target_rule_id,
        proposed_rule=proposed,
        evidence=[],
        stale_evidence=stale_evidence,
        status=ProposalStatus.PENDING,
        created_at=_NOW,
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proposals: list[dict[str, Any]],
    summaries: list[PeriodSummary] | None = None,
    rules: list[Rule] | None = None,
    pending: list[RuleProposal] | None = None,
) -> tuple[CannedLLM, list[RuleProposal]]:
    """Patch the LLM client and the memory/rules store methods used by reflect; capture ``create_proposal`` rows.

    Summaries are served for ``Scale.DAY`` only so the run's evidence refs are
    exactly the supplied list.
    """
    canned = CannedLLM(proposals)
    created: list[RuleProposal] = []
    summaries = [_summary()] if summaries is None else summaries

    def _fetch(
        agent_id: str,
        scale: Scale,
        *,
        limit: int | None = None,
        exclude_stale: bool = False,
    ) -> list[PeriodSummary]:
        # Mirror the real store: stale rows are filtered in the query (before the
        # limit), so reflection never sees them when it passes exclude_stale=True.
        rows = list(summaries) if scale is Scale.DAY else []
        if exclude_stale:
            rows = [s for s in rows if not s.stale]
        return rows

    monkeypatch.setattr(reflection, "get_client", lambda key: canned)
    monkeypatch.setattr(reflection.memory_store, "fetch_summaries", _fetch)
    monkeypatch.setattr(
        reflection.rules_store, "list_rules", lambda aid, status=None: list(rules or [])
    )
    monkeypatch.setattr(
        reflection.rules_store, "list_proposals", lambda aid, status=None: list(pending or [])
    )
    monkeypatch.setattr(reflection.rules_store, "create_proposal", lambda aid, p: created.append(p))
    # No-op by default so a stale-evidence pending row doesn't hit the real DB;
    # tests that assert supersession re-patch this with a recorder.
    monkeypatch.setattr(
        reflection.rules_store, "supersede_proposal", lambda aid, pid, now=None: None
    )
    return canned, created


def _boom(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("reflection must never create or activate a rule")


# ===========================================================================
# Pure tests (no Postgres)
# ===========================================================================
def test_empty_history_makes_no_llm_call_and_no_write(monkeypatch: pytest.MonkeyPatch) -> None:
    canned, created = _wire(monkeypatch, proposals=[{"action": "add", "text": "x"}], summaries=[])
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 0 and report.llm_calls == 0
    assert canned.json_calls == [] and created == []


def test_all_stale_summaries_make_no_llm_call_and_no_write(monkeypatch: pytest.MonkeyPatch) -> None:
    canned, created = _wire(
        monkeypatch,
        proposals=[{"action": "add", "text": "x"}],
        summaries=[_summary(sid="s1", stale=True), _summary(sid="s2", stale=True)],
    )
    report = reflection.reflect("a", _NOW)
    # Stale summaries are excluded → looks like empty history (defer to next run).
    assert report.proposed == 0 and report.llm_calls == 0
    assert canned.json_calls == [] and created == []


def test_stale_summaries_excluded_from_input_and_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = _summary(sid="fresh", version=3)
    stale = _summary(sid="stale", version=9, stale=True)
    _canned, created = _wire(
        monkeypatch,
        proposals=[{"action": "add", "text": "derived"}],
        summaries=[fresh, stale],
    )
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 1
    # Evidence cites only the fresh summary; the stale one is never referenced.
    assert created[0].evidence == [{"summary_id": "fresh", "version": 3}]


def test_add_proposal_is_materialized_pending_derived_with_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summaries = [_summary(sid="s1", version=2), _summary(sid="s2", version=1, scale=Scale.DAY)]
    canned, created = _wire(
        monkeypatch,
        proposals=[
            {"action": "add", "text": "run tests before merge", "rationale": "task-7 broke twice"}
        ],
        summaries=summaries,
    )
    report = reflection.reflect("a", _NOW)
    # The proposal call is now a reasoning pass (.complete) + a formatting
    # pass (.complete_json), both counted by _CallCountingClient — verify the
    # count actually reflects one of each, not two calls to the same method.
    assert report.proposed == 1 and report.llm_calls == 2
    assert len(canned.text_calls) == 1
    assert len(canned.json_calls) == 1
    assert canned.text_calls[0]["think"] is True
    assert canned.json_calls[0]["think"] is False
    (proposal,) = created
    assert proposal.action == ProposalAction.ADD
    assert proposal.target_rule_id is None
    assert proposal.status == ProposalStatus.PENDING
    assert proposal.proposed_rule == {
        "text": "run tests before merge",
        "mode": "advisory",
        "source": "derived",
        "priority": 0,
        "rationale": "task-7 broke twice",
    }
    assert proposal.evidence == [
        {"summary_id": "s1", "version": 2},
        {"summary_id": "s2", "version": 1},
    ]


def test_retire_known_target_builds_proposal_without_proposed_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rule = _rule(rid="r1")
    _canned, created = _wire(
        monkeypatch, proposals=[{"action": "retire", "target_rule_id": "r1"}], rules=[rule]
    )
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 1
    # 2 model calls: the proposal's reasoning pass + JSON formatting pass
    # (no compaction — the default input budget is not exceeded here).
    assert report.llm_calls == 2
    (proposal,) = created
    assert proposal.action == ProposalAction.RETIRE
    assert proposal.target_rule_id == "r1"
    assert proposal.proposed_rule is None


def test_retire_unknown_target_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _canned, created = _wire(
        monkeypatch, proposals=[{"action": "retire", "target_rule_id": "ghost"}], rules=[]
    )
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 0 and report.dropped_invalid == 1 and created == []


def test_amend_requires_active_target_and_text(monkeypatch: pytest.MonkeyPatch) -> None:
    rule = _rule(rid="r1")
    _canned, created = _wire(
        monkeypatch,
        proposals=[
            {"action": "amend", "target_rule_id": "r1", "text": "lint AND test before merge"},
            {"action": "amend", "target_rule_id": "ghost", "text": "x"},  # unknown target
            {"action": "amend", "target_rule_id": "r1"},  # missing text
        ],
        rules=[rule],
    )
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 1 and report.dropped_invalid == 2
    # 2 model calls: the proposal's reasoning pass + JSON formatting pass
    # (a single reflect() call, regardless of how many proposals it yields).
    assert report.llm_calls == 2
    (proposal,) = created
    assert proposal.action == ProposalAction.AMEND and proposal.target_rule_id == "r1"
    assert (
        proposal.proposed_rule is not None
        and proposal.proposed_rule["text"] == "lint AND test before merge"
    )


def test_amend_enforced_rule_inherits_mode_and_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    enforced = _rule(rid="r1", text="validate x", mode=RuleMode.ENFORCED, predicate=_GOOD_PREDICATE)
    _canned, created = _wire(
        monkeypatch,
        # Ordinary text amend that omits mode/predicate (the danger case).
        proposals=[{"action": "amend", "target_rule_id": "r1", "text": "validate x carefully"}],
        rules=[enforced],
    )
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 1
    # 2 model calls: the proposal's reasoning pass + JSON formatting pass.
    assert report.llm_calls == 2
    pr = created[0].proposed_rule
    # Mode/predicate/priority inherited → the enforced guardrail is preserved,
    # not silently downgraded to advisory or dropped for a missing predicate.
    assert pr["mode"] == "enforced" and pr["predicate"] == _GOOD_PREDICATE
    assert pr["text"] == "validate x carefully" and pr["priority"] == enforced.priority


def test_amend_can_change_priority_without_being_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    rule = _rule(rid="r1", text="be kind", priority=2)
    _canned, created = _wire(
        monkeypatch,
        proposals=[{"action": "amend", "target_rule_id": "r1", "text": "be kind", "priority": 7}],
        rules=[rule],
    )
    report = reflection.reflect("a", _NOW)
    # A real priority change is not a no-op → reaches review.
    assert report.proposed == 1 and report.deduped == 0
    # 2 model calls: the proposal's reasoning pass + JSON formatting pass.
    assert report.llm_calls == 2
    assert created[0].proposed_rule["priority"] == 7


def test_noop_amend_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    rule = _rule(rid="r1", text="be kind", priority=2)
    _canned, created = _wire(
        monkeypatch,
        # Restates the target verbatim (spacing/case differ, priority equal) → no change.
        proposals=[{"action": "amend", "target_rule_id": "r1", "text": "Be  Kind", "priority": 2}],
        rules=[rule],
    )
    report = reflection.reflect("a", _NOW)
    # Approving it would retire+reinsert an identical rule (churn) → suppressed.
    assert report.proposed == 0 and report.deduped == 1 and created == []


def test_noop_amend_not_falsely_matched_when_priority_omitted() -> None:
    # A stored proposed_rule that omits priority approves to priority 0 (the
    # store's _build_rule_from_spec default — it never consults the target), NOT
    # the target's current priority. Against a priority=5 target this is a real
    # (if silent) downgrade to 0, so it must NOT be treated as a no-op — a
    # target-inheriting comparison would wrongly suppress it before review.
    target = _rule(rid="r1", text="be kind", priority=5)
    proposal = reflection._build_proposal(
        "a", ProposalAction.AMEND, [], _NOW, target_rule_id="r1", proposed_rule={"text": "be kind"}
    )
    assert reflection._is_noop_amend(proposal, {"r1": target}) is False


def test_pending_amend_with_omitted_priority_not_deduped_against_differently_resolved_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pending row whose proposed_rule physically omits priority approves to
    # priority 0 (store default, no target consulted) — a materially different
    # rule than a freshly materialized proposal that inherits priority=5 from
    # the active target. They must NOT dedupe against each other: doing so would
    # silently drop the correct proposal in favor of one that approves wrong.
    target = _rule(rid="r1", text="be kind to users", mode=RuleMode.ADVISORY, priority=5)
    legacy_pending = RuleProposal(
        id=str(uuid4()),
        agent_id="a",
        action=ProposalAction.AMEND,
        target_rule_id="r1",
        proposed_rule={"text": "be kind to users, always"},  # priority omitted
        evidence=[],
        stale_evidence=False,
        status=ProposalStatus.PENDING,
        created_at=_NOW,
    )
    _canned, created = _wire(
        monkeypatch,
        proposals=[{"action": "amend", "target_rule_id": "r1", "text": "be kind to users, always"}],
        rules=[target],
        pending=[legacy_pending],
    )
    report = reflection.reflect("a", _NOW)
    # legacy_pending keys as priority=0 (store default); the fresh proposal keys
    # as priority=5 (real target inheritance, baked in by _build_proposed_rule).
    assert report.proposed == 1 and report.deduped == 0 and len(created) == 1
    assert created[0].proposed_rule["priority"] == 5


def test_pending_amend_with_omitted_mode_and_priority_not_deduped_against_differently_resolved_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pending row whose proposed_rule omits mode/priority (legacy/hand-authored
    # data) approves to advisory/0 (the store's plain default — it is handed only
    # proposed_rule, never the target). A freshly materialized amend that also
    # omits mode/priority instead inherits enforced/priority=5 from the active
    # target (real _build_proposed_rule behavior). Approving each produces a
    # materially different rule, so dedupe must not conflate them.
    target = _rule(
        rid="r1", text="validate x", mode=RuleMode.ENFORCED, priority=5, predicate=_GOOD_PREDICATE
    )
    legacy_pending = RuleProposal(
        id=str(uuid4()),
        agent_id="a",
        action=ProposalAction.AMEND,
        target_rule_id="r1",
        proposed_rule={"text": "validate x carefully", "predicate": _GOOD_PREDICATE},
        evidence=[],
        stale_evidence=False,
        status=ProposalStatus.PENDING,
        created_at=_NOW,
    )
    _canned, created = _wire(
        monkeypatch,
        proposals=[{"action": "amend", "target_rule_id": "r1", "text": "validate x carefully"}],
        rules=[target],
        pending=[legacy_pending],
    )
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 1 and report.deduped == 0 and len(created) == 1
    assert created[0].proposed_rule["mode"] == "enforced"
    assert created[0].proposed_rule["priority"] == 5


def test_dedupe_key_distinguishes_explicit_null_priority_from_omitted() -> None:
    # An explicit "priority": null in a stored proposed_rule is unapprovable
    # (store._build_rule_from_spec does int(spec.get("priority", 0)); a present
    # null makes int(None) raise, unlike a truly absent key which defaults to
    # 0) — its dedupe key must not collapse onto a real, approvable priority=0
    # proposal, or the valid one would be silently suppressed as a "duplicate".
    omitted = reflection._build_proposal(
        "a", ProposalAction.ADD, [], _NOW, proposed_rule={"text": "be kind"}
    )
    explicit_null = reflection._build_proposal(
        "a", ProposalAction.ADD, [], _NOW, proposed_rule={"text": "be kind", "priority": None}
    )
    valid_zero = reflection._build_proposal(
        "a", ProposalAction.ADD, [], _NOW, proposed_rule={"text": "be kind", "priority": 0}
    )
    assert reflection._dedupe_key_of(omitted)[-1] == 0
    assert reflection._dedupe_key_of(valid_zero)[-1] == 0
    assert reflection._dedupe_key_of(explicit_null)[-1] is None
    assert reflection._dedupe_key_of(explicit_null) != reflection._dedupe_key_of(valid_zero)


def test_pending_amend_with_explicit_null_priority_not_deduped_against_valid_zero_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pending row that stores "priority": null (as opposed to omitting the
    # key) is unapprovable, but must still not collide with — and suppress — a
    # distinct, legitimately materialized priority=0 proposal for the same text.
    target = _rule(rid="r1", text="be kind to users", mode=RuleMode.ADVISORY, priority=9)
    unapprovable_pending = RuleProposal(
        id=str(uuid4()),
        agent_id="a",
        action=ProposalAction.AMEND,
        target_rule_id="r1",
        proposed_rule={"text": "be kind to users, always", "priority": None},
        evidence=[],
        stale_evidence=False,
        status=ProposalStatus.PENDING,
        created_at=_NOW,
    )
    _canned, created = _wire(
        monkeypatch,
        proposals=[
            {
                "action": "amend",
                "target_rule_id": "r1",
                "text": "be kind to users, always",
                "priority": 0,
            }
        ],
        rules=[target],
        pending=[unapprovable_pending],
    )
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 1 and report.deduped == 0 and len(created) == 1
    assert created[0].proposed_rule["priority"] == 0


def test_noop_amend_not_falsely_matched_when_priority_explicitly_null() -> None:
    # A stored "priority": null is unapprovable and must not be compared as if
    # it equalled the target's current priority (which would wrongly classify
    # it as a no-op and drop it via a different path than an actual rejection).
    target = _rule(rid="r1", text="be kind", priority=0)
    proposal = reflection._build_proposal(
        "a",
        ProposalAction.AMEND,
        [],
        _NOW,
        target_rule_id="r1",
        proposed_rule={"text": "be kind", "priority": None},
    )
    assert reflection._is_noop_amend(proposal, {"r1": target}) is False


def test_render_rules_shows_enforced_predicate_only() -> None:
    advisory = _rule(rid="r1", text="be kind")
    enforced = _rule(rid="r2", text="validate x", mode=RuleMode.ENFORCED, predicate=_GOOD_PREDICATE)
    lines = reflection._render_rules_text([advisory, enforced]).splitlines()
    r1_line = next(line for line in lines if "id=r1" in line)
    r2_line = next(line for line in lines if "id=r2" in line)
    assert "predicate=" not in r1_line  # advisory rules render no predicate
    assert "predicate=" in r2_line  # enforced rules render their predicate for amends


def test_add_missing_text_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _canned, created = _wire(monkeypatch, proposals=[{"action": "add", "text": "   "}])
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 0 and report.dropped_invalid == 1 and created == []


def test_unknown_action_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _canned, created = _wire(monkeypatch, proposals=[{"action": "modify", "text": "x"}])
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 0 and report.dropped_invalid == 1 and created == []


def test_enforced_proposal_with_valid_predicate_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    _canned, created = _wire(
        monkeypatch,
        proposals=[
            {
                "action": "add",
                "text": "x must be 1",
                "mode": "enforced",
                "predicate": _GOOD_PREDICATE,
            }
        ],
    )
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 1
    (proposal,) = created
    assert proposal.proposed_rule is not None
    assert proposal.proposed_rule["mode"] == "enforced"
    assert proposal.proposed_rule["predicate"] == _GOOD_PREDICATE


def test_enforced_proposal_with_invalid_predicate_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _canned, created = _wire(
        monkeypatch,
        proposals=[
            {"action": "add", "text": "bad", "mode": "enforced", "predicate": _BAD_PREDICATE},
            {"action": "add", "text": "missing", "mode": "enforced"},  # no predicate at all
        ],
    )
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 0 and report.dropped_invalid == 2 and created == []


def test_unknown_mode_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _canned, created = _wire(
        monkeypatch, proposals=[{"action": "add", "text": "x", "mode": "magic"}]
    )
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 0 and report.dropped_invalid == 1


def test_duplicate_of_pending_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = _pending(action=ProposalAction.ADD, text="Be   Kind")  # different spacing/case
    _canned, created = _wire(
        monkeypatch,
        proposals=[{"action": "add", "text": "be kind"}],
        pending=[existing],
    )
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 0 and report.deduped == 1 and created == []


def test_duplicate_within_batch_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _canned, created = _wire(
        monkeypatch,
        proposals=[{"action": "add", "text": "be kind"}, {"action": "add", "text": "be kind"}],
    )
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 1 and report.deduped == 1 and len(created) == 1


def test_add_restating_an_active_rule_is_deduped(monkeypatch: pytest.MonkeyPatch) -> None:
    active = _rule(rid="r1", text="Always   LINT before merge")  # spacing/case differ
    _canned, created = _wire(
        monkeypatch,
        proposals=[{"action": "add", "text": "always lint before merge"}],
        rules=[active],
    )
    report = reflection.reflect("a", _NOW)
    # An ADD that restates an already-active rule is suppressed, not re-proposed.
    assert report.proposed == 0 and report.deduped == 1 and created == []


def test_add_with_target_is_dropped_as_incoherent(monkeypatch: pytest.MonkeyPatch) -> None:
    rule = _rule(rid="r1")
    _canned, created = _wire(
        monkeypatch,
        proposals=[{"action": "add", "target_rule_id": "r1", "text": "tied to r1"}],
        rules=[rule],
    )
    report = reflection.reflect("a", _NOW)
    # An add carrying a target is incoherent — dropped, not coerced into a new rule.
    assert report.proposed == 0 and report.dropped_invalid == 1 and created == []


def test_enforced_add_not_deduped_by_advisory_active_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    advisory = _rule(rid="r1", text="validate input", mode=RuleMode.ADVISORY)
    _canned, created = _wire(
        monkeypatch,
        proposals=[
            {
                "action": "add",
                "text": "validate input",
                "mode": "enforced",
                "predicate": _GOOD_PREDICATE,
            }
        ],
        rules=[advisory],
    )
    report = reflection.reflect("a", _NOW)
    # Same text but different mode → a real new (enforced) guardrail, not a dup.
    assert report.proposed == 1 and report.deduped == 0
    assert created[0].proposed_rule["mode"] == "enforced"


def test_stale_supersede_runs_before_llm_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale-proposal cleanup is decoupled from LLM availability."""
    stale = _pending(action=ProposalAction.ADD, text="obsolete", stale_evidence=True)
    _wire(monkeypatch, proposals=[], pending=[stale])
    superseded: list[str] = []
    monkeypatch.setattr(
        reflection.rules_store,
        "supersede_proposal",
        lambda aid, pid, now=None: superseded.append(pid),
    )

    def _boom_propose(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("LLM provider down")

    monkeypatch.setattr(reflection, "_propose", _boom_propose)
    with pytest.raises(RuntimeError):
        reflection.reflect("a", _NOW)
    # Supersession committed even though the LLM call then failed.
    assert superseded == [stale.id]


def test_stale_supersede_runs_when_history_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """All-stale memory must not strand stale pending rows on the fast path."""
    stale = _pending(action=ProposalAction.ADD, text="obsolete", stale_evidence=True)
    _canned, created = _wire(
        monkeypatch,
        proposals=[{"action": "add", "text": "x"}],
        summaries=[_summary(stale=True)],  # excluded → empty history
        pending=[stale],
    )
    superseded: list[str] = []
    monkeypatch.setattr(
        reflection.rules_store,
        "supersede_proposal",
        lambda aid, pid, now=None: superseded.append(pid),
    )
    report = reflection.reflect("a", _NOW)
    # No fresh memory → no LLM call / no proposals, but cleanup still ran.
    assert report.llm_calls == 0 and created == []
    assert report.superseded == 1 and superseded == [stale.id]


def test_same_text_different_priority_both_reach_review(monkeypatch: pytest.MonkeyPatch) -> None:
    _canned, created = _wire(
        monkeypatch,
        proposals=[
            {"action": "add", "text": "be kind", "priority": 0},
            {"action": "add", "text": "be kind", "priority": 5},
        ],
    )
    report = reflection.reflect("a", _NOW)
    # Priority is part of the change identity → a priority correction is not a dup.
    assert report.proposed == 2 and report.deduped == 0 and len(created) == 2


def test_add_restating_active_rule_suppressed_regardless_of_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _rule(rid="r1", text="be kind", priority=0)
    _canned, created = _wire(
        monkeypatch,
        proposals=[{"action": "add", "text": "be kind", "priority": 9}],  # different priority
        rules=[active],
    )
    report = reflection.reflect("a", _NOW)
    # Re-adding an active rule's content is suppressed even at a new priority —
    # re-prioritizing an existing rule is an amend, not a duplicate add.
    assert report.proposed == 0 and report.deduped == 1 and created == []


def test_llm_calls_counts_compaction(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tiny budget forces compact_text to make a model call before the proposal call.
    monkeypatch.setenv("AGENT_COGNITION_REFLECTION_INPUT_CHARS", "10")
    canned, created = _wire(monkeypatch, proposals=[{"action": "add", "text": "x"}])
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 1 and len(created) == 1
    # 1 compaction call (input over budget) + the proposal call's reasoning
    # pass (.complete) + formatting pass (.complete_json).
    # 3 total model calls: compact_text's one text (complete) call, plus the
    # proposal's reasoning pass (complete, think=True) and formatting pass
    # (complete_json, think=False) from complete_validated_via_reasoning.
    assert report.llm_calls == 3
    assert len(canned.text_calls) == 2
    assert len(canned.json_calls) == 1


def test_llm_calls_counts_complete_validated_correction_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If schema validation fails once, ``complete_validated`` retries once."""

    monkeypatch.setenv("AGENT_COGNITION_REFLECTION_INPUT_CHARS", "999999")  # no compaction

    class FlakyCannedLLM(LLMClient):
        def __init__(self) -> None:
            self.json_calls: list[dict[str, Any]] = []
            self.text_calls: list[dict[str, Any]] = []
            self.calls = 0

        def complete_json(
            self,
            prompt: str,
            *,
            objective: str,
            temperature: float = 0.0,
            system_prompt: str | None = None,
            tools: list | None = None,
            think: bool = False,
            **kwargs: Any,
        ) -> dict[str, Any]:
            self.calls += 1
            self.json_calls.append({"prompt": prompt, "objective": objective})
            if len(self.json_calls) == 1:
                # Trigger a schema validation error so ``complete_validated`` retries.
                return {"proposals": "not-a-list"}
            return {"proposals": [{"action": "add", "text": "derived rule"}]}

        def complete(
            self,
            prompt: str,
            *,
            temperature: float = 0.7,
            max_tokens: int | None = None,
            system_prompt: str | None = None,
            tools: list | None = None,
            think: bool = False,
            objective: str,
            **kwargs: object,
        ) -> str:
            # Reasoning pass from complete_validated_via_reasoning (think=True).
            # Compaction is disabled via AGENT_COGNITION_REFLECTION_INPUT_CHARS,
            # so this must only be the proposal reasoning call.
            self.text_calls.append(
                {
                    "prompt": prompt,
                    "objective": objective,
                    "think": think,
                }
            )
            assert think is True, "only the reasoning pass should call complete here"
            return "REASONING PROSE"

    canned = FlakyCannedLLM()
    created: list[RuleProposal] = []

    def _fetch(
        agent_id: str,
        scale: Scale,
        *,
        limit: int | None = None,
        exclude_stale: bool = False,
    ) -> list[PeriodSummary]:
        rows = [_summary()]
        return [s for s in rows if not s.stale] if exclude_stale else rows

    monkeypatch.setattr(reflection, "get_client", lambda key: canned)
    monkeypatch.setattr(reflection.memory_store, "fetch_summaries", _fetch)
    monkeypatch.setattr(reflection.rules_store, "list_rules", lambda aid, status=None: [])
    monkeypatch.setattr(reflection.rules_store, "list_proposals", lambda aid, status=None: [])
    monkeypatch.setattr(reflection.rules_store, "create_proposal", lambda aid, p: created.append(p))
    monkeypatch.setattr(
        reflection.rules_store, "supersede_proposal", lambda aid, pid, now=None: None
    )

    report = reflection.reflect("a", _NOW)
    assert report.proposed == 1
    # 1 reasoning complete() + 2 formatting complete_json() (initial miss + retry).
    assert report.llm_calls == 3
    assert len(canned.text_calls) == 1
    assert len(canned.json_calls) == 2
    assert len(created) == 1 and created[0].proposed_rule is not None


def test_priority_out_of_int32_range_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _canned, created = _wire(
        monkeypatch,
        proposals=[
            {"action": "add", "text": "too big", "priority": 2**31},  # > INT32 max
            {"action": "add", "text": "too small", "priority": -(2**31) - 1},  # < INT32 min
            {"action": "add", "text": "ok", "priority": 2**31 - 1},  # INT32 max → valid
        ],
    )
    report = reflection.reflect("a", _NOW)
    # Out-of-range priorities would overflow the INT32 column on approval → dropped
    # here; the boundary value is accepted.
    assert report.dropped_invalid == 2 and report.proposed == 1
    assert created[0].proposed_rule["priority"] == 2**31 - 1


def test_non_int_priority_is_dropped_without_raising() -> None:
    item = reflection._ProposedAction.model_construct(  # type: ignore[attr-defined]
        action="add",
        target_rule_id=None,
        text="ok",
        mode=None,
        predicate=None,
        rationale=None,
        priority=1.5,
    )
    assert reflection._materialize("a", item, {}, [], _NOW) is None  # type: ignore[attr-defined]


def test_non_string_text_is_dropped_without_raising() -> None:
    item = reflection._ProposedAction.model_construct(  # type: ignore[attr-defined]
        action="add",
        target_rule_id=None,
        text=123,
        mode=None,
        predicate=None,
        rationale=None,
        priority=0,
    )
    assert reflection._materialize("a", item, {}, [], _NOW) is None  # type: ignore[attr-defined]


def test_stale_pending_is_superseded_and_does_not_block_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _pending(action=ProposalAction.ADD, text="be kind", stale_evidence=True)
    _canned, created = _wire(
        monkeypatch,
        proposals=[{"action": "add", "text": "be kind"}],  # identical text to the stale one
        pending=[stale],
    )
    superseded: list[str] = []
    monkeypatch.setattr(
        reflection.rules_store,
        "supersede_proposal",
        lambda aid, pid, now=None: superseded.append(pid),
    )
    report = reflection.reflect("a", _NOW)
    # The stale proposal is retired, and its dedupe key no longer blocks the
    # fresh identical suggestion — which is created anew with fresh evidence.
    assert superseded == [stale.id] and report.superseded == 1
    assert report.proposed == 1 and report.deduped == 0 and len(created) == 1


def test_stale_pending_is_superseded_even_without_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _pending(action=ProposalAction.ADD, text="obsolete", stale_evidence=True)
    _canned, created = _wire(monkeypatch, proposals=[], pending=[stale])
    superseded: list[str] = []
    monkeypatch.setattr(
        reflection.rules_store,
        "supersede_proposal",
        lambda aid, pid, now=None: superseded.append(pid),
    )
    report = reflection.reflect("a", _NOW)
    assert superseded == [stale.id] and report.superseded == 1
    assert report.proposed == 0 and created == []


def test_fresh_pending_is_not_superseded(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = _pending(action=ProposalAction.ADD, text="keep me", stale_evidence=False)
    _canned, _created = _wire(monkeypatch, proposals=[], pending=[fresh])
    superseded: list[str] = []
    monkeypatch.setattr(
        reflection.rules_store,
        "supersede_proposal",
        lambda aid, pid, now=None: superseded.append(pid),
    )
    report = reflection.reflect("a", _NOW)
    assert superseded == [] and report.superseded == 0


def test_one_malformed_field_does_not_poison_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric priority drops just that item, not the whole response."""
    _canned, created = _wire(
        monkeypatch,
        proposals=[
            {"action": "add", "text": "good one"},
            {"action": "add", "text": "bad", "priority": "high"},  # int field given a word
        ],
    )
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 1 and report.dropped_invalid == 1
    assert len(created) == 1 and created[0].proposed_rule["text"] == "good one"


def test_non_object_item_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _canned, created = _wire(
        monkeypatch, proposals=[{"action": "add", "text": "ok"}, "garbage", 42]
    )
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 1 and report.dropped_invalid == 2 and len(created) == 1


def test_max_proposals_cap_stops_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_COGNITION_REFLECTION_MAX_PROPOSALS", "1")
    _canned, created = _wire(
        monkeypatch,
        proposals=[{"action": "add", "text": "first"}, {"action": "add", "text": "second"}],
    )
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 1 and len(created) == 1


def test_reflection_never_creates_or_activates_a_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance invariant: across a mixed run, only create_proposal is called."""
    rule = _rule(rid="r1")
    _canned, created = _wire(
        monkeypatch,
        proposals=[
            {"action": "add", "text": "new advisory"},
            {"action": "retire", "target_rule_id": "r1"},
            {"action": "amend", "target_rule_id": "r1", "text": "amended"},
        ],
        rules=[rule],
    )
    monkeypatch.setattr(reflection.rules_store, "approve_proposal", _boom)
    monkeypatch.setattr(reflection.rules_store, "create_rule", _boom)
    report = reflection.reflect("a", _NOW)
    assert report.proposed == 3
    assert all(p.status == ProposalStatus.PENDING for p in created)


def test_reflect_requires_non_empty_agent_id() -> None:
    """``reflect``'s precondition on a non-empty ``agent_id`` raises via assert."""
    with pytest.raises(AssertionError):
        reflection.reflect("", _NOW)


def test_reflect_requires_tz_aware_now() -> None:
    """``reflect``'s precondition on a tz-aware ``now`` raises via assert."""
    with pytest.raises(AssertionError):
        reflection.reflect("a", datetime(2026, 6, 1, 12, 0))  # naive


# --- helper-level unit tests -----------------------------------------------
def test_render_summaries_text_with_and_without_highlights() -> None:
    text = reflection._render_summaries_text(
        [_summary(summary="ran", highlights=[]), _summary(summary="failed", highlights=["boom"])]
    )
    assert "## Recent memory summaries" in text
    assert "[day 2026-05-01] ran" in text
    assert "failed | highlights: boom" in text


def test_render_rules_text_empty_and_populated() -> None:
    assert reflection._render_rules_text([]) == "## Current rules\n(none)"
    text = reflection._render_rules_text([_rule(rid="r9", text="t", priority=3)])
    assert "id=r9 [advisory, priority=3] t" in text


def test_dedupe_key_normalizes_text_mode_and_handles_retire() -> None:
    add = reflection._build_proposal(
        "a", ProposalAction.ADD, [], _NOW, proposed_rule={"text": "Be   KIND"}
    )
    enforced = reflection._build_proposal(
        "a",
        ProposalAction.ADD,
        [],
        _NOW,
        proposed_rule={"text": "be kind", "mode": "enforced", "predicate": _GOOD_PREDICATE},
    )
    retire = reflection._build_proposal("a", ProposalAction.RETIRE, [], _NOW, target_rule_id="r1")
    # add (omitted mode/priority default to advisory/0, matching the store's
    # _build_rule_from_spec approval default): mode/priority folded in, no
    # predicate fingerprint.
    assert reflection._dedupe_key_of(add) == ("add", None, "be kind", "advisory", None, 0)
    # same text but enforced + predicate → a distinct key (not a duplicate of add).
    assert reflection._dedupe_key_of(enforced) != reflection._dedupe_key_of(add)
    assert reflection._dedupe_key_of(enforced)[3] == "enforced"
    assert reflection._dedupe_key_of(retire) == ("retire", "r1", None, None, None, None)


def test_restates_active_rule_matches_add_content_only() -> None:
    content = {("be kind", "advisory", None)}
    retire = reflection._build_proposal("a", ProposalAction.RETIRE, [], _NOW, target_rule_id="r1")
    # proposed_rule["text"] is deliberately an int, not a str — it can never
    # textually match a set of (str, str, str|None) content tuples, so this
    # exercises the "non-str text" branch distinctly from the "wrong action"
    # branch covered by `retire` above.
    bad_text = reflection._build_proposal(
        "a", ProposalAction.ADD, [], _NOW, proposed_rule={"text": 123}
    )
    add_match = reflection._build_proposal(
        "a", ProposalAction.ADD, [], _NOW, proposed_rule={"text": "Be  Kind", "priority": 7}
    )
    # Only a content-matching ADD is a restatement; non-ADD and non-str text are not.
    assert reflection._restates_active_rule(retire, content) is False
    assert reflection._restates_active_rule(bad_text, content) is False
    assert reflection._restates_active_rule(add_match, content) is True  # priority ignored


def test_is_noop_amend_guards_non_amend_and_missing_target() -> None:
    target = _rule(rid="r1", text="be kind", priority=3)
    by_id = {"r1": target}
    add = reflection._build_proposal(
        "a", ProposalAction.ADD, [], _NOW, proposed_rule={"text": "be kind"}
    )
    ghost = reflection._build_proposal(
        "a", ProposalAction.AMEND, [], _NOW, target_rule_id="gone", proposed_rule={"text": "x"}
    )
    same = reflection._build_proposal(
        "a",
        ProposalAction.AMEND,
        [],
        _NOW,
        target_rule_id="r1",
        proposed_rule={"text": "Be Kind", "mode": "advisory", "priority": 3},
    )
    assert reflection._is_noop_amend(add, by_id) is False  # not an amend
    assert reflection._is_noop_amend(ghost, by_id) is False  # target not active
    assert reflection._is_noop_amend(same, by_id) is True  # identical content


def test_env_tunables_read_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_COGNITION_REFLECTION_SUMMARY_LIMIT", "9")
    monkeypatch.setenv("AGENT_COGNITION_REFLECTION_MAX_PROPOSALS", "2")
    monkeypatch.setenv("AGENT_COGNITION_REFLECTION_INPUT_CHARS", "1234")
    assert reflection._summary_limit() == 9
    assert reflection._max_proposals() == 2
    assert reflection._input_char_budget() == 1234


# ===========================================================================
# Live-Postgres tests — end-to-end against the real stores with a canned LLM.
# ===========================================================================
pg = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="POSTGRES_HOST not set; skipping live-Postgres reflection tests",
)


@pytest.fixture(autouse=True)
def _provision_schema() -> None:
    if not is_postgres_enabled():
        return
    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)


def _seed_summary(agent_id: str, sid: str) -> PeriodSummary:
    from agent_cognition.memory import store as memory_store

    summary = _summary(agent_id, sid=sid)
    memory_store.upsert_summary(agent_id, summary, computed_at=summary.period_end)
    return summary


@pg
def test_reflect_persists_pending_proposal_and_leaves_rules_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed_summary("a", "sum-1")
    store.create_rule("a", _rule("a", rid="r-keep", text="seed rule"))
    monkeypatch.setattr(
        reflection, "get_client", lambda key: CannedLLM([{"action": "add", "text": "derived rule"}])
    )

    report = reflection.reflect("a", _NOW)

    assert report.proposed == 1
    assert report.llm_calls == 2  # reasoning pass + formatting pass
    pending = store.list_proposals("a", status=ProposalStatus.PENDING)
    assert len(pending) == 1
    assert pending[0].action == ProposalAction.ADD
    assert (
        pending[0].proposed_rule is not None and pending[0].proposed_rule["text"] == "derived rule"
    )
    assert {"summary_id": seeded.id, "version": seeded.version} in pending[0].evidence
    # Nothing was activated: the only active rule is the one we seeded.
    active = store.list_rules("a", status=RuleStatus.ACTIVE)
    assert [r.id for r in active] == ["r-keep"]


@pg
def test_reflected_proposal_activates_only_after_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_summary("a", "sum-1")
    monkeypatch.setattr(
        reflection,
        "get_client",
        lambda key: CannedLLM([{"action": "add", "text": "approved later"}]),
    )
    report = reflection.reflect("a", _NOW)
    assert report.llm_calls == 2  # reasoning pass + formatting pass
    (proposal,) = store.list_proposals("a", status=ProposalStatus.PENDING)

    # Before approval: no active rules at all.
    assert store.list_rules("a", status=RuleStatus.ACTIVE) == []

    rule = store.approve_proposal("a", proposal.id, decided_by="operator")
    assert rule is not None and rule.status == RuleStatus.ACTIVE and rule.text == "approved later"
    assert rule.source == RuleSource.DERIVED
    assert [r.id for r in store.list_rules("a", status=RuleStatus.ACTIVE)] == [rule.id]


@pg
def test_reflect_supersedes_stale_pending_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_summary("a", "sum-1")
    stale = _pending(action=ProposalAction.ADD, text="stale one", stale_evidence=True)
    store.create_proposal("a", stale)
    monkeypatch.setattr(reflection, "get_client", lambda key: CannedLLM([]))

    report = reflection.reflect("a", _NOW)

    assert report.superseded == 1
    assert report.llm_calls == 2  # reasoning pass + formatting pass
    assert store.get_proposal("a", stale.id).status == ProposalStatus.SUPERSEDED  # type: ignore[union-attr]
    assert store.list_proposals("a", status=ProposalStatus.PENDING) == []
