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
from functools import partial
from pathlib import Path
from typing import Tuple

from shared.dev_models.models import Task
from shared.git.git_utils import commit_paths
from shared.repo_context.repo_utils import find_repo_files
from software_engineering_team.shared import v2_review_bindings
from software_engineering_team.shared.phases.setup import (
    configure_quality_tooling_impl,
    run_setup_impl,
)
from software_engineering_team.shared.stack_profile import StackProfile
from software_engineering_team.shared.v2_output_templates import _section as _section  # noqa: F401
from software_engineering_team.shared.v2_phase_bindings import build_phase_bindings
from software_engineering_team.shared.v2_review import ReviewConfig
from software_engineering_team.shared.v2_team_config import V2TeamConfig

from .. import models as _models
from ..models import SetupResult, ToolAgentKind, ToolAgentPhaseInput
from ..prompts import (
    DOCUMENTATION_SELF_REVIEW_PROMPT,
    PLANNING_FIXES_FOR_ISSUES_PROMPT,
    PLANNING_PROMPT,
    TYPESCRIPT_CONVENTIONS,
)

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
# Setup phase: the config-driven ``run_setup``/``configure_quality_tooling``
# entry points, bound once here from the shared implementation
# (``shared/phases/setup.py``) via this team's frontend lint/test hooks.
# Replaces the former per-team ``phases/setup.py`` wrapper module.
# ``commit_paths`` stays a module-level name here (rather than imported inside
# the shared impl) so this module remains the monkeypatch boundary for the
# scaffolding-commit tests.
# ---------------------------------------------------------------------------


def _ensure_linting_configured(path: Path, written: set) -> bool:
    """Verify that a frontend linter is configured in the project.

    Checks for eslint config files (eslint.config.*, .eslintrc*) or ng lint
    availability (angular.json). If none are found, creates a minimal ESLint
    flat config so linting never silently skips.

    Side effect: every repo-relative path this call creates or modifies is added
    to ``written`` so the caller can commit exactly what setup touched.
    """
    # Check for existing eslint config
    eslint_patterns = ("eslint.config.*", ".eslintrc*", ".eslintrc.json", ".eslintrc.js")
    for pattern in eslint_patterns:
        if list(path.glob(pattern)):
            logger.info("Setup: linting already configured via %s", pattern)
            return True

    # Angular projects can use ng lint
    if (path / "angular.json").exists():
        logger.info("Setup: Angular project detected; creating eslint.config.js for ng lint")
        from shared.command_runner.scaffolding import MINIMAL_ANGULAR_ESLINT_CONFIG

        config_file = path / "eslint.config.js"
        if not config_file.exists():
            config_file.write_text(MINIMAL_ANGULAR_ESLINT_CONFIG, encoding="utf-8")
            written.add("eslint.config.js")
        return True

    # React/generic project — create ESLint flat config
    logger.info("Setup: no linting configuration found; creating eslint.config.mjs")
    from shared.command_runner.scaffolding import MINIMAL_REACT_ESLINT_CONFIG

    config_file = path / "eslint.config.mjs"
    if not config_file.exists():
        config_file.write_text(MINIMAL_REACT_ESLINT_CONFIG, encoding="utf-8")
        written.add("eslint.config.mjs")

    # Ensure lint script exists in package.json
    if _ensure_package_script(path, "lint", "eslint ."):
        written.add("package.json")
    return True


def _ensure_testing_configured(path: Path, written: set) -> bool:
    """Verify that a frontend test framework is configured in the project.

    Checks for vitest/jest config files or test scripts in package.json.
    If missing, creates a minimal vitest configuration so tests never skip.

    Side effect: every repo-relative path this call creates or modifies is added
    to ``written`` so the caller can commit exactly what setup touched.
    """
    # Check for existing test config
    test_configs = (
        "vitest.config.*",
        "jest.config.*",
        "karma.conf.js",
    )
    for pattern in test_configs:
        if list(path.glob(pattern)):
            logger.info("Setup: testing already configured via %s", pattern)
            return True

    # Check if package.json has a meaningful test script
    pkg_json = path / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            test_script = pkg.get("scripts", {}).get("test", "")
            if test_script and "no test" not in test_script and "exit 1" not in test_script:
                logger.info("Setup: testing already configured via package.json test script")
                return True
        except Exception as e:
            logger.debug("Setup: could not parse package.json for test script check: %s", e)

    # Create vitest config based on framework
    is_angular = (path / "angular.json").exists()
    if is_angular:
        logger.info("Setup: creating vitest.config.mts for Angular project")
        from shared.command_runner.scaffolding import (
            MINIMAL_ANGULAR_TEST_SETUP,
            MINIMAL_ANGULAR_VITEST_CONFIG,
        )

        config_file = path / "vitest.config.mts"
        if not config_file.exists():
            config_file.write_text(MINIMAL_ANGULAR_VITEST_CONFIG, encoding="utf-8")
            written.add("vitest.config.mts")
        # Ensure test setup file
        src = path / "src"
        src.mkdir(parents=True, exist_ok=True)
        setup_file = src / "test-setup.ts"
        if not setup_file.exists():
            setup_file.write_text(MINIMAL_ANGULAR_TEST_SETUP, encoding="utf-8")
            written.add("src/test-setup.ts")
    else:
        logger.info("Setup: creating vitest.config.ts for React project")
        from shared.command_runner.scaffolding import MINIMAL_REACT_VITEST_CONFIG

        config_file = path / "vitest.config.ts"
        if not config_file.exists():
            config_file.write_text(MINIMAL_REACT_VITEST_CONFIG, encoding="utf-8")
            written.add("vitest.config.ts")

    if _ensure_package_script(path, "test", "vitest run"):
        written.add("package.json")
    if _ensure_package_script(path, "test:coverage", "vitest run --coverage"):
        written.add("package.json")
    return True


def _ensure_package_script(path: Path, script_name: str, script_cmd: str) -> bool:
    """Add a script to package.json if it doesn't already exist, or overwrite it
    if the existing script is a placeholder that just fails (contains "exit 1").

    Returns:
        True when package.json was modified, False otherwise (missing file,
        a real script already present, or a read/parse error).
    """
    pkg_json = path / "package.json"
    if not pkg_json.exists():
        return False
    try:
        pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
        scripts = pkg.setdefault("scripts", {})
        if script_name not in scripts or "exit 1" in scripts.get(script_name, ""):
            scripts[script_name] = script_cmd
            pkg_json.write_text(json.dumps(pkg, indent=2), encoding="utf-8")
            return True
    except Exception as e:
        logger.warning("Could not update package.json script %s: %s", script_name, e)
    return False


def configure_quality_tooling(repo_path: Path) -> Tuple[bool, bool]:
    """Ensure lint/test scaffolding exists on the CURRENT branch and commit it.

    Delegates to the shared implementation, injecting this team's frontend
    lint/test hooks and the module-level ``commit_paths`` (kept importable here
    as the monkeypatch boundary for the scaffolding-commit tests).

    Preconditions:
        - ``repo_path`` is a git repository checked out on the branch to configure.
    Postconditions:
        - Linting and testing are configured on the current branch and any newly
          written scaffolding is committed to it. Returns ``(lint_ok, test_ok)``.
    """
    return configure_quality_tooling_impl(
        repo_path,
        ensure_linting=_ensure_linting_configured,
        ensure_testing=_ensure_testing_configured,
        commit_paths=commit_paths,
    )


def run_setup(*, repo_path: Path, task_title: str = "") -> SetupResult:
    """
    Ensure the repository is initialized and ready for frontend development.

    - If the path is not a git repo: git init, create README.md with project title,
      initial commit, rename master to main if needed, create development branch.
    - If already a repo: ensure development branch exists and is checked out;
      optionally ensure README exists (create minimal one if missing).
    - Always verifies linting and testing are configured before returning.

    Preconditions:
        ``repo_path`` is a filesystem path (created if missing).
    Postconditions:
        Returns a ``SetupResult``; delegates to the shared implementation with
        this team's ``configure_quality_tooling``.
    """
    return run_setup_impl(
        repo_path=repo_path,
        task_title=task_title,
        configure_quality_tooling=configure_quality_tooling,
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


# ---------------------------------------------------------------------------
# Extra review clause: frontend-specific accessibility-verification note
# injected into code-review task requirements.
# Defined here (rather than in review.py) so V2TeamConfig can reference it
# without creating a circular import (review.py already imports from _profile).
# ---------------------------------------------------------------------------

_ACCESSIBILITY_VERIFY_NOTE = (
    "Also verify accessibility: semantic markup, ARIA attributes, keyboard "
    "navigation, and color contrast."
)

# ---------------------------------------------------------------------------
# V2TeamConfig: the single source of truth for frontend's team-specific knobs.
# Defined here (alongside PROFILE) so that both the orchestrator and tool agents
# can import it without creating a circular dependency.
# ---------------------------------------------------------------------------

FRONTEND_CONFIG = V2TeamConfig(
    stack_profile=PROFILE,
    tool_agent_kinds=frozenset(k.value for k in ToolAgentKind if k is not ToolAgentKind.GENERAL),
    extra_review_clause=_ACCESSIBILITY_VERIFY_NOTE,
    output_template_path_prefixes=("frontend/", "./frontend/"),
    output_template_allowed_languages=("angular", "react", "vue", "typescript", "javascript"),
    output_template_coerce_unknown=False,
)


# ---------------------------------------------------------------------------
# Phase bindings: the config-driven documentation/planning/output-template
# entry points, bound once here from the shared implementations via
# ``FRONTEND_CONFIG`` (see ``shared/v2_phase_bindings.py``). Replaces the
# former per-team ``phases/documentation.py``, ``phases/planning.py``, and
# ``output_templates.py`` twin wrapper modules.
# ---------------------------------------------------------------------------

_bindings = build_phase_bindings(
    models=_models,
    config=FRONTEND_CONFIG,
    planning_prompt=PLANNING_PROMPT,
    planning_fixes_prompt=PLANNING_FIXES_FOR_ISSUES_PROMPT,
)

run_documentation_phase = _bindings.run_documentation_phase
run_planning = _bindings.run_planning
plan_fixes_for_unresolved_issues = _bindings.plan_fixes_for_unresolved_issues
_parse_planning_output = _bindings.parse_planning_output
parse_files_and_summary_template = _bindings.parse_files_and_summary_template
parse_planning_template = _bindings.parse_planning_template
parse_review_template = _bindings.parse_review_template
parse_problem_solving_template = _bindings.parse_problem_solving_template
parse_problem_solving_single_issue_template = _bindings.parse_problem_solving_single_issue_template
parse_batch_fix_template = _bindings.parse_batch_fix_template
parse_documentation_self_review_template = _bindings.parse_documentation_self_review_template

# ---------------------------------------------------------------------------
# Review bindings: the config-driven review/QA/security/doc-self-review entry
# points, bound once here from the shared implementation via FRONTEND_CONFIG
# and REVIEW_CONFIG (see shared/v2_review_bindings.py). Replaces the former
# per-team phases/review.py wrapper module. Each partial rebinds only
# config/review_config (and, for doc self-review, prompt/parser) -- the
# wrapped functions still resolve their sibling helpers
# (_run_llm_review/_run_qa_agent/_run_security_agent/_run_build_verification)
# by bare name inside shared.v2_review_bindings on every call, so that module
# stays the test patch surface (see its module docstring).
# ---------------------------------------------------------------------------

run_review = partial(
    v2_review_bindings.run_review, config=FRONTEND_CONFIG, review_config=REVIEW_CONFIG
)
run_microtask_review = partial(
    v2_review_bindings.run_microtask_review, config=FRONTEND_CONFIG, review_config=REVIEW_CONFIG
)
run_code_review_phase = partial(v2_review_bindings.run_code_review_phase, config=FRONTEND_CONFIG)
run_qa_testing_phase = partial(
    v2_review_bindings.run_qa_testing_phase, config=FRONTEND_CONFIG, review_config=REVIEW_CONFIG
)
run_security_testing_phase = partial(
    v2_review_bindings.run_security_testing_phase,
    config=FRONTEND_CONFIG,
    review_config=REVIEW_CONFIG,
)
run_documentation_self_review = partial(
    v2_review_bindings.run_documentation_self_review,
    prompt=DOCUMENTATION_SELF_REVIEW_PROMPT,
    parse_template=parse_documentation_self_review_template,
)
_run_llm_review = partial(v2_review_bindings._run_llm_review, config=FRONTEND_CONFIG)
_run_qa_agent = v2_review_bindings._run_qa_agent
_run_security_agent = v2_review_bindings._run_security_agent
_run_build_verification = partial(
    v2_review_bindings._run_build_verification, config=FRONTEND_CONFIG
)

__all__ = [
    "PROFILE",
    "REVIEW_CONFIG",
    "FRONTEND_CONFIG",
    "run_setup",
    "configure_quality_tooling",
    "run_documentation_phase",
    "run_planning",
    "plan_fixes_for_unresolved_issues",
    "_parse_planning_output",
    "_detect_language",
    "parse_files_and_summary_template",
    "parse_planning_template",
    "parse_review_template",
    "parse_problem_solving_template",
    "parse_problem_solving_single_issue_template",
    "parse_batch_fix_template",
    "parse_documentation_self_review_template",
    "run_review",
    "run_microtask_review",
    "run_code_review_phase",
    "run_qa_testing_phase",
    "run_security_testing_phase",
    "run_documentation_self_review",
    "_run_llm_review",
    "_run_qa_agent",
    "_run_security_agent",
    "_run_build_verification",
]
