"""Unit tests for the design-review critique ledger.

These lock in the building blocks of the regression guard and within-loop
stall detection, independent of the orchestrator:

* ``compute_issue_id`` — deterministic, stable under trivial rewording.
* ``CritiqueIssue.issue_id`` — auto-filled on every construction site.
* ``SpecCritique.open_issue_ids`` — blocking issues only (info excluded).
* ``CritiqueLedger`` — resolved / persisted / new / regressed deltas and the
  stall predicate.
"""

from __future__ import annotations

from investment_team.strategy_lab.agents.design_review import (
    CritiqueIssue,
    CritiqueLedger,
    SpecCritique,
    compute_issue_id,
)


def _issue(field: str, description: str, severity: str = "warning") -> CritiqueIssue:
    return CritiqueIssue(field=field, description=description, severity=severity)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# compute_issue_id / issue_id auto-fill
# ---------------------------------------------------------------------------


def test_compute_issue_id_is_deterministic_and_field_prefixed() -> None:
    iid = compute_issue_id("exit_rules", "Add a take-profit rule")
    assert iid == compute_issue_id("exit_rules", "Add a take-profit rule")
    assert iid.startswith("exit_rules:")


def test_compute_issue_id_stable_under_rewording() -> None:
    """Case, punctuation, and whitespace differences normalise to one id."""
    a = compute_issue_id("sizing", "Position is TOO large!!!")
    b = compute_issue_id("sizing", "position   is too large")
    assert a == b


def test_compute_issue_id_distinguishes_field_and_content() -> None:
    base = compute_issue_id("sizing", "too large")
    assert base != compute_issue_id("risk_limits", "too large")  # different field
    assert base != compute_issue_id("sizing", "too small")  # different content


def test_issue_id_autofilled_when_blank() -> None:
    issue = _issue("hypothesis", "thesis is vague")
    assert issue.issue_id == compute_issue_id("hypothesis", "thesis is vague")


def test_explicit_issue_id_preserved() -> None:
    """A supplied id round-trips unchanged (legacy/persisted rows)."""
    issue = CritiqueIssue(field="hypothesis", description="x", issue_id="legacy:abc123")
    assert issue.issue_id == "legacy:abc123"


# ---------------------------------------------------------------------------
# open_issue_ids
# ---------------------------------------------------------------------------


def test_open_issue_ids_excludes_info_severity() -> None:
    critique = SpecCritique(
        ready=False,
        issues=[
            _issue("exit_rules", "add tp", "critical"),
            _issue("sizing", "minor nit", "info"),
            _issue("timeframe", "mismatch", "warning"),
        ],
    )
    open_ids = critique.open_issue_ids
    assert compute_issue_id("exit_rules", "add tp") in open_ids
    assert compute_issue_id("timeframe", "mismatch") in open_ids
    assert compute_issue_id("sizing", "minor nit") not in open_ids


def test_open_issue_ids_empty_when_no_blocking_issues() -> None:
    critique = SpecCritique(ready=True, issues=[_issue("sizing", "nit", "info")])
    assert critique.open_issue_ids == set()


# ---------------------------------------------------------------------------
# CritiqueLedger deltas
# ---------------------------------------------------------------------------


def test_first_round_is_all_new() -> None:
    led = CritiqueLedger()
    c = SpecCritique(ready=False, issues=[_issue("exit_rules", "add tp")])
    delta = led.record_round(c)
    assert delta.new == {compute_issue_id("exit_rules", "add tp")}
    assert delta.resolved == set()
    assert delta.regressed == set()
    assert delta.round == 0


def test_resolved_and_persisted_split() -> None:
    led = CritiqueLedger()
    x = compute_issue_id("exit_rules", "add tp")
    y = compute_issue_id("sizing", "too big")
    led.record_round(
        SpecCritique(
            ready=False,
            issues=[_issue("exit_rules", "add tp"), _issue("sizing", "too big")],
        )
    )
    # Round 1: x resolved, y persists.
    delta = led.record_round(SpecCritique(ready=False, issues=[_issue("sizing", "too big")]))
    assert delta.resolved == {x}
    assert delta.persisted == {y}
    assert delta.new == set()
    assert delta.regressed == set()


def test_regression_detected_when_resolved_issue_reappears() -> None:
    led = CritiqueLedger()
    x = compute_issue_id("exit_rules", "add tp")
    led.record_round(SpecCritique(ready=False, issues=[_issue("exit_rules", "add tp")]))
    led.record_round(SpecCritique(ready=False, issues=[_issue("sizing", "too big")]))  # x resolved
    delta = led.record_round(SpecCritique(ready=False, issues=[_issue("exit_rules", "add tp")]))
    assert delta.regressed == {x}
    assert delta.new == set()  # x is a regression, not genuinely new
    assert led.total_regressed == 1


def test_ever_resolved_is_monotonic() -> None:
    led = CritiqueLedger()
    x = compute_issue_id("exit_rules", "add tp")
    led.record_round(SpecCritique(ready=False, issues=[_issue("exit_rules", "add tp")]))
    led.record_round(SpecCritique(ready=True, issues=[]))  # x resolved
    assert x in led.ever_resolved
    # Re-raising it does not remove it from ever_resolved.
    led.record_round(SpecCritique(ready=False, issues=[_issue("exit_rules", "add tp")]))
    assert x in led.ever_resolved


# ---------------------------------------------------------------------------
# Stall predicate
# ---------------------------------------------------------------------------


def test_not_stalled_with_insufficient_history() -> None:
    led = CritiqueLedger()
    led.record_round(SpecCritique(ready=False, issues=[_issue("hypothesis", "vague")]))
    led.record_round(SpecCritique(ready=False, issues=[_issue("hypothesis", "vague")]))
    # Only 2 identical rounds; threshold of 3 → not yet stalled.
    assert led.is_stalled(3) is False


def test_stalled_when_open_set_unchanged_for_n_rounds() -> None:
    led = CritiqueLedger()
    for _ in range(3):
        led.record_round(SpecCritique(ready=False, issues=[_issue("hypothesis", "vague")]))
    assert led.is_stalled(3) is True


def test_changing_open_set_breaks_stall() -> None:
    led = CritiqueLedger()
    led.record_round(SpecCritique(ready=False, issues=[_issue("hypothesis", "vague-a")]))
    led.record_round(SpecCritique(ready=False, issues=[_issue("hypothesis", "vague-b")]))
    led.record_round(SpecCritique(ready=False, issues=[_issue("hypothesis", "vague-c")]))
    assert led.is_stalled(3) is False


def test_empty_open_set_is_never_a_stall() -> None:
    led = CritiqueLedger()
    for _ in range(3):
        led.record_round(SpecCritique(ready=True, issues=[]))
    # An empty open set means convergence, not oscillation.
    assert led.is_stalled(3) is False


def test_stall_threshold_floored_to_one() -> None:
    led = CritiqueLedger()
    led.record_round(SpecCritique(ready=False, issues=[_issue("hypothesis", "vague")]))
    # n<1 floors to 1 → a single non-empty round counts as stalled.
    assert led.is_stalled(0) is True


def test_current_open_returns_copy() -> None:
    led = CritiqueLedger()
    led.record_round(SpecCritique(ready=False, issues=[_issue("hypothesis", "vague")]))
    snapshot = led.current_open
    snapshot.add("mutation")
    assert "mutation" not in led.current_open
