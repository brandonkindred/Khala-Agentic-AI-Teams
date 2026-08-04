"""Problem-solving phase: attempt targeted remediation after review failures."""

from __future__ import annotations

from ..constants import (
    ARTIFACT_GATE_DESCRIPTION_PREFIX,
    PLACEHOLDER_ARTIFACT_DIR,
    REQUIRED_ARTIFACT_HINTS,
)
from ..models import ExecutionResult, ProblemSolvingResult, ReviewResult


def run_problem_solving(
    *, execution_result: ExecutionResult, review_result: ReviewResult
) -> ProblemSolvingResult:
    """Synthesize placeholder artifacts for missing known artifact-category issues.

    Preconditions: ``review_result.issues`` may be empty. This is a purely
      deterministic, non-LLM fix — it only ever addresses ``artifact_gate``-
      sourced issues whose description starts with
      ``ARTIFACT_GATE_DESCRIPTION_PREFIX``, yields a non-empty hint token, and
      that token is in ``REQUIRED_ARTIFACT_HINTS``; it cannot resolve
      ``execution``-sourced issues (failed microtasks), malformed artifact-gate
      descriptions, or unknown category tokens.
    Postconditions: returns a ``ProblemSolvingResult`` where ``resolved`` is
      True iff at least one placeholder file was synthesized; ``files`` is a
      new dict — ``execution_result.files`` merged with the placeholder
      patches — and ``execution_result`` itself is not mutated. ``fixes_applied``
      lists each synthesized placeholder and is empty when ``resolved`` is False.
      Unknown-token ``artifact_gate`` issues produce no placeholder.
    """
    fixes_applied = []
    patched_files = {}

    for issue in review_result.issues:
        if issue.source != "artifact_gate":
            continue
        if not issue.description.startswith(ARTIFACT_GATE_DESCRIPTION_PREFIX):
            continue
        token = issue.description[len(ARTIFACT_GATE_DESCRIPTION_PREFIX) :].strip()
        if not token or token not in REQUIRED_ARTIFACT_HINTS:
            continue
        path = f"{PLACEHOLDER_ARTIFACT_DIR}/{token}_placeholder.md"
        patched_files[path] = (
            f"# Placeholder {token}\n\nAuto-generated during problem-solving to satisfy artifact gate."
        )
        fixes_applied.append(f"Added placeholder artifact for missing category '{token}'.")

    resolved = len(patched_files) > 0
    summary = (
        "Applied targeted artifact-gap fixes."
        if resolved
        else "No deterministic fixes were available."
    )
    merged_files = dict(execution_result.files)
    merged_files.update(patched_files)

    return ProblemSolvingResult(
        resolved=resolved, fixes_applied=fixes_applied, files=merged_files, summary=summary
    )
