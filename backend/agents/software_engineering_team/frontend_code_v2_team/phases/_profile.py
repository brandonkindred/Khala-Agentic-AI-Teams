"""
Frontend stack profile: language/tooling detection + the knobs that select
frontend behavior in the shared code-v2 phase implementations.

``_detect_language`` lives here (rather than in ``planning.py``) so the profile
can reference it without importing the heavier phase module — ``planning.py``
re-exports it for callers and tests. See ``shared/stack_profile.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Tuple

from shared.dev_models.models import Task
from shared.repo_context.repo_utils import find_repo_files
from software_engineering_team.shared.stack_profile import StackProfile
from software_engineering_team.shared.v2_review import ReviewConfig

from ..models import ToolAgentPhaseInput
from ..prompts import TYPESCRIPT_CONVENTIONS

logger = logging.getLogger(__name__)

# Frontend repo-briefing filter contract: the extensions read into the development
# agent's context and the directories pruned from the walk. Single-sourced here so
# the fresh-walk ``_read_repo_code`` and the incremental ``RepoContextCache`` the
# team lead threads in cannot drift apart (the cache's byte-identical invariant
# depends on them matching).
_FRONTEND_REPO_EXTENSIONS = frozenset(
    {".ts", ".tsx", ".js", ".jsx", ".html", ".css", ".scss", ".json", ".yaml", ".yml"}
)
_FRONTEND_REPO_EXCLUDE_DIRS = frozenset({"node_modules", ".git", "dist", "build", ".angular"})
# Character budget for the repo briefing (whole files only; the next chunk that
# would exceed it stops the briefing).
_FRONTEND_REPO_BRIEFING_MAX_CHARS = 30_000


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
            except (OSError, UnicodeDecodeError) as exc:
                # Best-effort substring probe on the raw text (no json.loads),
                # so only file-read (OSError) and decode (UnicodeDecodeError)
                # failures can land here — a malformed package.json just means
                # no stack signal was found and the repo walk / task-text
                # heuristics decide instead. Logged at DEBUG (mirroring
                # _detect_tooling) so a real config problem stays observable.
                logger.debug("[%s] failed to read/decode package.json: %s", repo_path, exc)
        # Pruned os.walk (find_repo_files) so a checkout with a large
        # node_modules/.git/dist/.angular is never descended into while probing
        # for tsconfig / *.ts / *.tsx — the same I/O discipline as
        # read_repo_code_budgeted, replacing the rglob calls that enumerated
        # those excluded subtrees before filtering.
        if find_repo_files(repo_path, names={"tsconfig.json"}):
            return "typescript"
        if find_repo_files(repo_path, suffixes={".tsx", ".ts"}):
            return "typescript"
    desc = (task.description or "").lower() + " " + (task.requirements or "").lower()
    if "angular" in desc:
        return "angular"
    if "react" in desc:
        return "react"
    if "typescript" in desc or "ts " in desc:
        return "typescript"
    return "typescript"


def _detect_tooling(repo_path: Path) -> Tuple[bool, bool]:
    """Return ``(has_lint, has_test)`` for the configured frontend tooling.

    Detects ESLint/Angular configs as lint, and Vitest/Jest/Karma or a real
    ``npm test`` script as testing. Best-effort: an unparseable ``package.json``
    just means no test script was found.

    Preconditions: ``repo_path`` is a directory.
    Postconditions: returns two booleans. Raises ``AssertionError`` if the
      precondition is violated (a non-directory ``repo_path`` is a caller
      bug, not a runtime failure mode this method recovers from).
    """
    assert repo_path.is_dir(), "repo_path must be a directory"
    has_lint = (
        next(repo_path.glob("eslint.config.*"), None) is not None
        or next(repo_path.glob(".eslintrc*"), None) is not None
        or (repo_path / "angular.json").exists()
    )
    has_test = False
    if (
        next(repo_path.glob("vitest.config.*"), None) is not None
        or next(repo_path.glob("jest.config.*"), None) is not None
        or (repo_path / "karma.conf.js").exists()
    ):
        has_test = True
    else:
        pkg_json = repo_path / "package.json"
        if pkg_json.exists():
            try:
                pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
                test_script = pkg.get("scripts", {}).get("test", "")
                if test_script and "no test" not in test_script and "exit 1" not in test_script:
                    has_test = True
            except Exception as exc:
                # A malformed package.json means no test script was found;
                # log at DEBUG so a real config problem is observable during
                # debugging without failing the best-effort pre-flight gate.
                logger.debug("[%s] failed to parse package.json: %s", repo_path, exc)
    return has_lint, has_test


PROFILE = StackProfile(
    name="frontend",
    default_language="typescript",
    planning_language_label="Language/stack",
    planning_progress_label="stack",
    conventions_by_language={"_default": TYPESCRIPT_CONVENTIONS},
    has_language_conventions=False,
    build_verify_label="frontend_code_v2",
    detect_language=_detect_language,
    repo_extensions=_FRONTEND_REPO_EXTENSIONS,
    repo_exclude_dirs=_FRONTEND_REPO_EXCLUDE_DIRS,
    repo_max_chars=_FRONTEND_REPO_BRIEFING_MAX_CHARS,
    detect_tooling=_detect_tooling,
)


# ---------------------------------------------------------------------------
# Review config: the knobs that select frontend behaviour in the shared
# ``shared.v2_review.run_review`` / ``run_microtask_review`` bodies.
# ---------------------------------------------------------------------------


def _frontend_summary_review(
    passed: bool, build_ok: bool, lint_ok: bool, n_issues: int, n_critical: int
) -> str:
    """One-line result summary for the frontend full-Review phase.

    Preconditions: all args are the booleans/ints the shared reviewer computes.
    Postconditions: returns a single human-readable line naming pass/fail and
    the issue count; ignores ``build_ok``/``lint_ok``/``n_critical`` (frontend
    keeps its summary terse — the per-gate status is logged separately).
    """
    return f"Review {'passed' if passed else 'failed'}; {n_issues} issue(s)."


def _frontend_summary_microtask(
    microtask_id: str, passed: bool, build_ok: bool, lint_ok: bool, n_issues: int, n_critical: int
) -> str:
    """One-line result summary for a frontend microtask review.

    Preconditions: ``microtask_id`` is the microtask's id; the rest are the
    reviewer-computed booleans/ints.
    Postconditions: returns a single line naming the microtask, pass/fail, and
    issue count; ignores ``build_ok``/``lint_ok``/``n_critical`` (terse summary).
    """
    return (
        f"Microtask {microtask_id} review {'passed' if passed else 'failed'}; {n_issues} issue(s)."
    )


def _frontend_microtask_intro(microtask_id: str, n_files: int) -> str:
    """Intro line emitted when a frontend microtask review begins.

    Preconditions: ``microtask_id`` is the microtask's id; ``n_files`` >= 0 is
    the number of files scoped into the review.
    Postconditions: returns a single line naming the microtask, its file count,
    and the next quality-gate step.
    """
    return (
        f"Microtask review for {microtask_id} ({n_files} files). "
        "Next step -> Build verification, lint, code review"
    )


REVIEW_CONFIG = ReviewConfig(
    lint_agent_type="frontend",
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
