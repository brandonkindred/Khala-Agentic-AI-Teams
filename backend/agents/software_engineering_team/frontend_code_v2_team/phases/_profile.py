"""
Frontend stack profile: language detection + the knobs that select frontend
behavior in the shared code-v2 phase implementations.

``_detect_language`` lives here (rather than in ``planning.py``) so the profile
can reference it without importing the heavier phase module — ``planning.py``
re-exports it for callers and tests. See ``shared/stack_profile.py``.
"""

from __future__ import annotations

from pathlib import Path

from software_engineering_team.shared.models import Task
from software_engineering_team.shared.stack_profile import StackProfile
from software_engineering_team.shared.v2_review import ReviewConfig

from ..models import ToolAgentPhaseInput
from ..prompts import TYPESCRIPT_CONVENTIONS


def _detect_language(repo_path: Path, task: Task) -> str:
    """Infer frontend stack from repo or task.

    Preconditions:
        ``repo_path`` is a ``Path`` (may or may not exist); ``task`` is a
        ``Task`` whose ``description``/``requirements`` may be ``None``.
    Postconditions:
        Returns one of ``"angular"``, ``"react"``, or ``"typescript"``; never
        raises. Repo signals take precedence over task-text heuristics;
        ``"typescript"`` is the default.
    """
    if repo_path.is_dir():
        if (repo_path / "angular.json").exists():
            return "angular"
        pkg = repo_path / "package.json"
        if pkg.exists():
            try:
                content = pkg.read_text(encoding="utf-8")
                if "@angular/core" in content or "@angular/common" in content:
                    return "angular"
                if '"react"' in content or "'react'" in content:
                    return "react"
            except Exception:
                pass
        if any(repo_path.rglob("tsconfig.json")):
            return "typescript"
        if any(repo_path.rglob("*.tsx")) or any(repo_path.rglob("*.ts")):
            return "typescript"
    desc = (task.description or "").lower() + " " + (task.requirements or "").lower()
    if "angular" in desc:
        return "angular"
    if "react" in desc:
        return "react"
    if "typescript" in desc or "ts " in desc:
        return "typescript"
    return "typescript"


PROFILE = StackProfile(
    name="frontend",
    default_language="typescript",
    planning_language_label="Language/stack",
    planning_progress_label="stack",
    conventions_by_language={"_default": TYPESCRIPT_CONVENTIONS},
    has_language_conventions=False,
    detect_language=_detect_language,
)


# ---------------------------------------------------------------------------
# Review config: the knobs that select frontend behaviour in the shared
# ``shared.v2_review.run_review`` / ``run_microtask_review`` bodies.
# ---------------------------------------------------------------------------


def _frontend_summary_review(passed: bool, build_ok: bool, lint_ok: bool, n_issues: int, n_critical: int) -> str:
    return f"Review {'passed' if passed else 'failed'}; {n_issues} issue(s)."


def _frontend_summary_microtask(
    microtask_id: str, passed: bool, build_ok: bool, lint_ok: bool, n_issues: int, n_critical: int
) -> str:
    return f"Microtask {microtask_id} review {'passed' if passed else 'failed'}; {n_issues} issue(s)."


def _frontend_microtask_intro(microtask_id: str, n_files: int) -> str:
    return (
        f"Microtask review for {microtask_id} ({n_files} files). "
        "Next step -> Build verification, lint, code review"
    )


REVIEW_CONFIG = ReviewConfig(
    lint_agent_type="frontend",
    build_verify_label="frontend_code_v2",
    build_fail_recommendation_review="Fix build errors; consider triggering Build Specialist.",
    # Frontend keeps the raw linter severity (no remap).
    lint_severity_remap=None,
    # Frontend uses kind.value verbatim (no "tool_" prefix) and a blank rec.
    tool_rec_source_prefix=None,
    tool_rec_recommendation_uses_rec=False,
    # Frontend omits existing_code/spec_context/language on the tool phase input.
    tool_phase_includes_context=False,
    # Frontend run-review `passed` includes lint_ok.
    passed_includes_lint_review=True,
    # Frontend run-review does not log its summary line.
    log_review_summary=False,
    tool_phase_input_factory=ToolAgentPhaseInput,
    summary_review=_frontend_summary_review,
    summary_microtask=_frontend_summary_microtask,
    microtask_intro=_frontend_microtask_intro,
)
