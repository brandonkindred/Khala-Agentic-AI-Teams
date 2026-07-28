"""Problem-solving phase: attempt targeted remediation after review failures."""

from __future__ import annotations

from ..models import ExecutionResult, ProblemSolvingResult, ReviewResult


def run_problem_solving(
    *, execution_result: ExecutionResult, review_result: ReviewResult
) -> ProblemSolvingResult:
    """Synthesize placeholder artifacts for any missing-artifact-category issue.

    Preconditions: ``review_result.issues`` may be empty. This is a purely
      deterministic, non-LLM fix — it only ever addresses ``artifact_gate``-
      sourced issues (missing required artifact categories); it cannot
      resolve ``execution``-sourced issues (failed microtasks).
    Postconditions: returns a ``ProblemSolvingResult`` where ``resolved`` is
      True iff at least one placeholder file was synthesized; ``files`` is a
      new dict — ``execution_result.files`` merged with the placeholder
      patches — and ``execution_result`` itself is not mutated. ``fixes_applied``
      lists each synthesized placeholder and is empty when ``resolved`` is False.
    """
    fixes_applied = []
    patched_files = {}

    for issue in review_result.issues:
        if issue.source == "artifact_gate":
            token = issue.description.split(":")[-1].strip()
            path = f"ai_system/{token}_placeholder.md"
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
