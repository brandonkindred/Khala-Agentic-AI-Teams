"""Review phase: quality gate for generated AI-agent artifacts."""

from __future__ import annotations

from ..models import ExecutionResult, MicrotaskStatus, ReviewIssue, ReviewResult

REQUIRED_ARTIFACT_HINTS = ("blueprint", "evaluation", "safety", "runbook", "mcp")


def run_review(*, execution_result: ExecutionResult) -> ReviewResult:
    issues = []
    file_names = "\n".join(execution_result.files.keys()).lower()

    for hint in REQUIRED_ARTIFACT_HINTS:
        if hint not in file_names:
            issues.append(
                ReviewIssue(
                    source="artifact_gate",
                    severity="high",
                    description=f"Missing expected artifact category: {hint}",
                    recommendation=f"Add at least one artifact path containing '{hint}'.",
                )
            )

    failed_microtasks = [m for m in execution_result.microtasks if m.status == MicrotaskStatus.FAILED]
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
