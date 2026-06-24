"""Record quality-gate outcomes as events + learnings.

A single hook the orchestrator/coding pipeline calls after each quality gate
(code review, QA, security, accessibility, acceptance). On rejection it writes a
``gate_rejected`` event (DORA signal) and upserts a learning (closed loop). The
gate result models are non-uniform — ``approved`` vs ``all_satisfied``, and
``issues`` / ``bugs_found`` / ``vulnerabilities`` / ``per_criterion`` — so this
module normalizes them in one place.

Best-effort and a no-op without Postgres; never raises into the pipeline.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Result attributes that hold the list of findings, in priority order.
_ISSUE_LIST_ATTRS = ("issues", "bugs_found", "vulnerabilities", "per_criterion")
# Per-issue attributes that hold a human-readable description / fix.
_ISSUE_DESC_ATTRS = ("description", "issue", "criterion", "summary")
_ISSUE_FIX_ATTRS = ("recommendation", "suggestion", "remediation")


def is_rejected(result: Any) -> Optional[bool]:
    """Return ``True`` if the gate rejected, ``False`` if it passed, ``None`` if unknown.

    Postconditions: reads ``approved`` (pass=True) or ``all_satisfied``
        (pass=True); returns ``None`` when neither attribute is present.
    """
    approved = getattr(result, "approved", None)
    if isinstance(approved, bool):
        return not approved
    satisfied = getattr(result, "all_satisfied", None)
    if isinstance(satisfied, bool):
        return not satisfied
    return None


def _first_issue(result: Any) -> Optional[Any]:
    """Return the finding that best explains a rejection, or ``None``.

    Gate-shape assumption (best-effort): an item is treated as an acceptance
    *criterion* iff it exposes a boolean ``satisfied`` attribute — the shape used
    by ``AcceptanceVerifierOutput.per_criterion`` today. Items without it are
    treated as plain findings (code review / QA / security). A future gate that
    signals pass/fail via a *different* attribute (e.g. ``passed``) would be read
    as a plain finding, so its first entry could be surfaced even if it passed;
    add such an attribute to this contract before relying on it.

    Postconditions: prefers the first *failing* criterion; else the first plain
        finding; else ``None`` — never labels a passing criterion as the rejection.
    """
    for attr in _ISSUE_LIST_ATTRS:
        items = getattr(result, attr, None)
        if not items:
            continue
        try:
            # A list may hold per_criterion items (which expose ``satisfied``),
            # plain issues (code review / QA / security — no ``satisfied``), or a
            # mix. Prefer the first *failing* criterion; otherwise the first plain
            # issue (so a real finding listed alongside passing criteria isn't
            # dropped); otherwise None — never label a passing criterion as the
            # rejection.
            failing_criteria = []
            plain_issues = []
            for item in items:
                if hasattr(item, "satisfied"):
                    if item.satisfied is False:
                        failing_criteria.append(item)
                else:
                    plain_issues.append(item)
            if failing_criteria:
                return failing_criteria[0]
            if plain_issues:
                return plain_issues[0]
            return None
        except TypeError:
            return None
    return None


def _pick(obj: Any, attrs: tuple[str, ...]) -> str:
    for attr in attrs:
        value = getattr(obj, attr, None)
        if value:
            return str(value)
    return ""


def record_gate_outcome(
    gate: str,
    result: Any,
    *,
    job_id: str = "",
    task_id: str = "",
    phase: str = "execution",
) -> bool:
    """Record a ``gate_rejected`` event + learning when ``result`` is a rejection.

    Preconditions:
        - ``gate`` is a non-empty string (e.g. ``"code_review"``, ``"security"``).
    Postconditions:
        - Returns ``True`` when a rejection was recorded; ``False`` when the gate
          passed, the verdict is unknown, or Postgres is disabled. Never raises.
    """
    if not gate:
        return False
    try:
        if is_rejected(result) is not True:
            return False
        summary = _pick(result, ("summary",))
        issue = _first_issue(result)
        trigger = (_pick(issue, _ISSUE_DESC_ATTRS) if issue is not None else "") or summary
        counter_measure = _pick(issue, _ISSUE_FIX_ATTRS) if issue is not None else ""

        from software_engineering_team.shared import se_events
        from software_engineering_team.shared.learnings_store import upsert_learning

        se_events.record_event(
            se_events.GATE_REJECTED,
            job_id=job_id,
            task_id=task_id,
            phase=phase,
            gate=gate,
            detail={"summary": summary[:500], "trigger": trigger[:500]},
        )
        upsert_learning(
            pattern=f"{gate} rejection",
            trigger=trigger[:500] or summary[:500] or f"{gate} gate rejected output",
            counter_measure=counter_measure[:500],
            source="gate_rejection",
            category=gate,
        )
        return True
    except Exception:
        logger.debug("failed to record gate outcome for %s", gate, exc_info=True)
        return False


__all__ = ["record_gate_outcome", "is_rejected"]
