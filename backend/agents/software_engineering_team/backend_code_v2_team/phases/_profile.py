"""
Backend stack profile: language detection + the knobs that select backend
behavior in the shared code-v2 phase implementations.

``_detect_language`` lives here (rather than in ``planning.py``) so the profile
can reference it without importing the heavier phase module — ``planning.py``
re-exports it for callers and tests. See ``shared/stack_profile.py``.
"""

from __future__ import annotations

from pathlib import Path

from shared.repo_context.repo_utils import find_repo_files
from software_engineering_team.shared.models import Task
from software_engineering_team.shared.stack_profile import StackProfile
from software_engineering_team.shared.v2_review import ReviewConfig

from ..models import ToolAgentPhaseInput
from ..prompts import JAVA_CONVENTIONS, PYTHON_CONVENTIONS


def _detect_language(repo_path: Path, task: Task) -> str:
    """Infer whether the project is Python or Java from the repo.

    Preconditions:
        ``repo_path`` is a ``Path`` (may or may not exist); ``task`` is a
        ``Task`` whose ``description``/``requirements`` may be ``None``.
    Postconditions:
        Returns ``"java"`` or ``"python"``; never raises. Repo signals take
        precedence over task-text heuristics; ``"python"`` is the default.
    """
    if repo_path.is_dir():
        # Pruned os.walk (find_repo_files) so a checkout with a large
        # node_modules/.git/.venv is never descended into while probing for
        # build files / *.java — the same I/O discipline as
        # read_repo_code_budgeted, replacing the rglob calls that enumerated
        # those excluded subtrees before filtering.
        if find_repo_files(repo_path, names={"pom.xml", "build.gradle"}):
            return "java"
        if find_repo_files(repo_path, names={"requirements.txt", "pyproject.toml"}):
            return "python"
        if find_repo_files(repo_path, suffixes={".java"}):
            return "java"
    desc = (task.description or "").lower() + " " + (task.requirements or "").lower()
    if "spring" in desc or "java" in desc or "maven" in desc or "gradle" in desc:
        return "java"
    return "python"


PROFILE = StackProfile(
    name="backend",
    default_language="python",
    planning_language_label="Language",
    planning_progress_label="language",
    conventions_by_language={"java": JAVA_CONVENTIONS, "_default": PYTHON_CONVENTIONS},
    has_language_conventions=True,
    build_verify_label="backend_code_v2",
    detect_language=_detect_language,
)


# ---------------------------------------------------------------------------
# Review config: the knobs that select backend behaviour in the shared
# ``shared.v2_review.run_review`` / ``run_microtask_review`` bodies.
# ---------------------------------------------------------------------------

# Backend remaps linter severities into review severities (frontend keeps raw).
_BACKEND_LINT_SEVERITY_MAP = {"error": "high", "warning": "medium", "info": "low"}


def _backend_summary_review(
    passed: bool, build_ok: bool, lint_ok: bool, n_issues: int, n_critical: int
) -> str:
    """One-line result summary for the backend full-Review phase.

    Preconditions: all args are the booleans/ints the shared reviewer computes.
    Postconditions: returns a single human-readable line naming the build/lint
    outcome and the issue count (with the critical/high count). ``passed`` is
    unused — the backend full-review summary reports gate status, not an
    overall pass/fail label.
    """
    return (
        f"Review: build={'OK' if build_ok else 'FAIL'}, lint={'OK' if lint_ok else 'FAIL'}, "
        f"{n_issues} issues ({n_critical} critical/high)."
    )


def _backend_summary_microtask(
    microtask_id: str, passed: bool, build_ok: bool, lint_ok: bool, n_issues: int, n_critical: int
) -> str:
    """One-line result summary for a backend microtask review.

    Preconditions: ``microtask_id`` is the microtask's id; the rest are the
    reviewer-computed booleans/ints.
    Postconditions: returns a single line naming the microtask, its build/lint
    outcome, issue count (with critical/high count), and a PASSED/FAILED label
    driven by ``passed``.
    """
    return (
        f"Microtask {microtask_id} review: build={'OK' if build_ok else 'FAIL'}, "
        f"lint={'OK' if lint_ok else 'FAIL'}, {n_issues} issues ({n_critical} critical/high). "
        f"{'PASSED' if passed else 'FAILED'}"
    )


def _backend_microtask_intro(microtask_id: str, n_files: int) -> str:
    """Intro line emitted when a backend microtask review begins.

    Preconditions: ``microtask_id`` is the microtask's id; ``n_files`` >= 0 is
    the number of files scoped into the review.
    Postconditions: returns a single line naming the microtask and its file
    count, marking the start of the per-microtask quality-gate sequence.
    """
    return f"Running microtask review for {microtask_id} ({n_files} files)"


REVIEW_CONFIG = ReviewConfig(
    lint_agent_type="backend",
    build_verify_label="backend_code_v2",
    build_fail_recommendation_review="Fix compilation/test errors before proceeding.",
    lint_severity_remap=_BACKEND_LINT_SEVERITY_MAP,
    tool_rec_source_prefix="tool_",
    tool_rec_recommendation_uses_rec=True,
    tool_phase_includes_context=True,
    # Backend run-review ignores lint_ok in `passed` (only build + blocking).
    passed_includes_lint_review=False,
    log_review_summary=True,
    tool_phase_input_factory=ToolAgentPhaseInput,
    summary_review=_backend_summary_review,
    summary_microtask=_backend_summary_microtask,
    microtask_intro=_backend_microtask_intro,
)
