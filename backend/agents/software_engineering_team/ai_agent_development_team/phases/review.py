"""Review phase: quality gate for generated AI-agent artifacts."""

from __future__ import annotations

from ..constants import ARTIFACT_GATE_DESCRIPTION_PREFIX, REQUIRED_ARTIFACT_HINTS
from ..models import ExecutionResult, MicrotaskStatus, ReviewIssue, ReviewResult


def _artifact_gate_issue(hint: str) -> ReviewIssue:
    """Build a high-severity artifact-gate issue for a missing category hint.

    Preconditions: ``hint`` is a non-empty category token (callers pass entries
      from ``REQUIRED_ARTIFACT_HINTS``).
    Postconditions: returns a ``ReviewIssue`` with ``source="artifact_gate"``,
      ``severity="high"``, description ``ARTIFACT_GATE_DESCRIPTION_PREFIX`` +
      ``hint``, and a recommendation naming that hint. Does not mutate inputs.
    """
    assert isinstance(hint, str) and hint.strip(), "hint must be a non-empty string"
    return ReviewIssue(
        source="artifact_gate",
        severity="high",
        description=f"{ARTIFACT_GATE_DESCRIPTION_PREFIX}{hint}",
        recommendation=f"Add at least one artifact path containing '{hint}'.",
    )


def run_review(*, execution_result: ExecutionResult) -> ReviewResult:
    """Deterministically check generated artifacts against required-category hints.

    Preconditions: ``execution_result.files`` and ``.microtasks`` may be empty.
      This is a purely deterministic, non-LLM check — no external agent calls.
    Postconditions: returns a ``ReviewResult`` where ``passed`` is True iff no
      ``high``/``critical`` severity issues were raised (one ``high`` issue per
      missing ``REQUIRED_ARTIFACT_HINTS`` entry, plus one per ``FAILED``
      microtask); ``required_artifacts_ok`` is True iff no ``artifact_gate``-
      sourced issue was raised. Does not mutate ``execution_result``.
    """
    issues = []

    for hint in REQUIRED_ARTIFACT_HINTS:
        if not any(hint in name.lower() for name in execution_result.files):
            issues.append(_artifact_gate_issue(hint))

    failed_microtasks = [
        m for m in execution_result.microtasks if m.status == MicrotaskStatus.FAILED
    ]
    for mt in failed_microtasks:
        issues.append(
            ReviewIssue(
                source="execution",
                severity="high",
                description=f"Microtask failed: {mt.id}",
                recommendation="Re-run with clarified acceptance criteria and additional context.",
            )
        )

    high_or_critical = [i for i in issues if i.severity in ("high", "critical")]
    passed = len(high_or_critical) == 0
    summary = (
        "Review passed."
        if passed
        else f"Review failed with {len(high_or_critical)} high/critical issues across artifact and execution gates."
    )

    return ReviewResult(
        passed=passed,
        issues=issues,
        required_artifacts_ok=len([i for i in issues if i.source == "artifact_gate"]) == 0,
        summary=summary,
    )
