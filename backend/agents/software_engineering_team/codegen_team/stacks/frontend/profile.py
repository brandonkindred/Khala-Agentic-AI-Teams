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
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm_service import LLMClient
from shared.dev_models.models import ReviewContext, Task
from shared.git.git_utils import commit_paths
from shared.repo_context.repo_utils import find_repo_files
from software_engineering_team.codegen_team import models as _models
from software_engineering_team.codegen_team.models import (
    MicrotaskStatus,
    SetupResult,
    ToolAgentInput,
    ToolAgentKind,
    ToolAgentOutput,
    ToolAgentPhaseInput,
)
from software_engineering_team.codegen_team.stacks.frontend.prompts import (
    DOCUMENTATION_SELF_REVIEW_PROMPT,
    EXECUTION_PROMPT,
    PLANNING_FIXES_FOR_ISSUES_PROMPT,
    PLANNING_PROMPT,
    TYPESCRIPT_CONVENTIONS,
)
from software_engineering_team.shared import v2_review_bindings
from software_engineering_team.shared.agent_review import AgentReviewCache
from software_engineering_team.shared.phases.dbc_phase import run_dbc_comments_review
from software_engineering_team.shared.phases.execution import (
    GateOutcome,
    ReviewDependencies,
    run_gated_execution_impl,
)
from software_engineering_team.shared.phases.setup import (
    configure_quality_tooling_impl,
    run_setup_impl,
)
from software_engineering_team.shared.stack_profile import StackProfile
from software_engineering_team.shared.v2_execution_bindings import (
    build_execution_bindings,
    scope_tool_agents_by_kind,
)
from software_engineering_team.shared.v2_output_templates import _section as _section  # noqa: F401
from software_engineering_team.shared.v2_phase_bindings import build_phase_bindings
from software_engineering_team.shared.v2_review import ReviewConfig
from software_engineering_team.shared.v2_team_config import V2TeamConfig

ToolAgentRunner = Callable[[ToolAgentInput], ToolAgentOutput]

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

# ``ToolAgentKind`` is a single superset enum shared by both stacks (see
# ``codegen_team/models.py``), so this must enumerate frontend's own subset
# explicitly rather than "every member except GENERAL" -- that computation
# was only correct back when each stack had its own separate enum type.
# Kept in lockstep with the roster ``orchestrator._build_frontend_tool_agents``
# constructs.
_FRONTEND_TOOL_AGENT_KINDS = (
    ToolAgentKind.STATE_MANAGEMENT,
    ToolAgentKind.AUTH,
    ToolAgentKind.API_OPENAPI,
    ToolAgentKind.DOCUMENTATION,
    ToolAgentKind.TESTING_QA,
    ToolAgentKind.SECURITY,
    ToolAgentKind.GIT_BRANCH_MANAGEMENT,
    ToolAgentKind.UI_DESIGN,
    ToolAgentKind.BRANDING_THEME,
    ToolAgentKind.UX_USABILITY,
    ToolAgentKind.ACCESSIBILITY,
    ToolAgentKind.PERFORMANCE,
    ToolAgentKind.ARCHITECTURE,
    ToolAgentKind.BUILD_SPECIALIST,
    ToolAgentKind.LINTER,
)

FRONTEND_CONFIG = V2TeamConfig(
    stack_profile=PROFILE,
    tool_agent_kinds=frozenset(k.value for k in _FRONTEND_TOOL_AGENT_KINDS),
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

# ---------------------------------------------------------------------------
# Execution bindings: the config-driven run_execution/run_execution_with_review_gates/
# GATE_CONFIG entry points, bound once here via ``build_execution_bindings``
# (see ``shared/v2_execution_bindings.py``). Replaces the former per-team
# ``phases/execution.py`` twin wrapper module. The three gate adapters below
# stay team-authored (frontend architecture: one unified ``run_microtask_review``
# called three times, filtering issues by ``source``) -- see
# ``shared/v2_execution_bindings.py``'s module docstring for why this fork is
# intentional and not collapsed into shared code. The QA and security gates
# each scope the ``tool_agents`` mapping down to their own kind
# (``testing_qa`` / ``security``) via the shared ``scope_tool_agents_by_kind``
# before calling ``run_microtask_review``, so their tool-agent fan-out no
# longer runs every wired tool agent on every gate call (matching
# the backend stack's per-gate scoping).
# ---------------------------------------------------------------------------


def _code_review_gate(
    *,
    llm: LLMClient,
    task: Task,
    microtask: Any,
    repo_path: Path,
    files: Dict[str, str],
    deps: ReviewDependencies,
    detail_callback: Callable[[str], None],
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
) -> GateOutcome:
    """Run the frontend code-review gate (build + lint + code review agents,
    plus every wired tool agent).

    Preconditions: ``deps.build_verifier``/``deps.code_review_agent``/
      ``deps.linting_tool_agent``/``deps.tool_agents`` are set consistently
      with what the caller wants exercised; ``files`` is the microtask's
      current ``{path: content}`` output.
    Postconditions: the review/agent logic itself never raises (build, lint,
      code-review, and tool-agent failures are all contained to synthetic
      issues or logged warnings); an exception from ``detail_callback`` —
      which is invoked outside that containment and, in the gated loop,
      forwards to the caller-supplied ``progress_callback`` — is not caught
      here and propagates uncaught. Calls ``run_microtask_review`` with
      ``qa_agent=None, security_agent=None`` (disabling only those two LLM
      review steps) and the full, unscoped ``deps.tool_agents`` mapping —
      unlike the QA/security gates, this call does not narrow ``tool_agents``
      to a single kind, so the returned ``issues`` can include
      build/lint/code-review findings *and* every wired tool agent's findings
      (e.g. accessibility, ui_design), not only code-review-sourced ones.
      Copies ``passed``/``issues``/``summary``/``raw_issue_count`` (defaulting
      to ``None``) from the result unfiltered. Also forwards
      ``deps.tool_agent_cache`` into ``run_microtask_review`` so that any
      ``testing_qa``/``security`` tool-agent calls made here are served from
      cache (rather than re-invoked) when the QA/Security gates already
      computed them earlier in the same cycle, or vice versa.
    """
    r = run_microtask_review(
        llm=llm,
        task=task,
        microtask=microtask,
        repo_path=repo_path,
        files=files,
        build_verifier=deps.build_verifier,
        qa_agent=None,
        security_agent=None,
        code_review_agent=deps.code_review_agent,
        linting_tool_agent=deps.linting_tool_agent,
        tool_agents=deps.tool_agents,
        detail_callback=detail_callback,
        review_context=review_context,
        enable_llm_review_grounding=enable_llm_review_grounding,
        tool_agent_cache=deps.tool_agent_cache,
    )
    return GateOutcome(
        passed=r.passed,
        issues=r.issues,
        summary=r.summary,
        raw_issue_count=getattr(r, "raw_issue_count", None),
    )


def _qa_gate(
    *,
    llm: LLMClient,
    task: Task,
    microtask: Any,
    repo_path: Path,
    files: Dict[str, str],
    deps: ReviewDependencies,
    detail_callback: Callable[[str], None],
    cache: Optional[AgentReviewCache] = None,
) -> GateOutcome:
    """Run the frontend QA gate, keeping only ``source == "qa"`` issues.

    Disables the external ``security_agent``/``code_review_agent``/
    ``linting_tool_agent`` and passes ``build_verifier=None`` — build and
    lint are then genuinely skipped by ``run_microtask_review``.
    ``code_review_agent=None`` does not skip code review, though: the shared
    ``_code_review_step`` still runs its LLM-fallback reviewer whenever no
    external agent is supplied, and the fan-out calls it unconditionally, so
    a code-review LLM call happens on every invocation of this gate; its
    issues are filtered out below, not never produced.

    ``cache``: forwarded to ``run_microtask_review`` as the pre-existing
    per-agent QA/security LLM cache (unrelated to ``tool_agent_cache``).
    ``deps.tool_agent_cache`` is forwarded separately so a ``testing_qa``
    tool-agent result already computed by the CR gate's fan-out this cycle is
    reused here instead of re-invoked.

    Preconditions: ``deps.qa_agent``/``deps.tool_agents`` are set consistently
      with what the caller wants exercised; ``files`` is the microtask's
      current ``{path: content}`` output.
    Postconditions: the review/agent logic itself never raises (an outright
      QA-agent or tool-agent failure is contained to a synthetic issue or a
      logged warning); an exception from ``detail_callback`` — which is
      invoked outside that containment and, in the gated loop, forwards to
      the caller-supplied ``progress_callback`` — is not caught here and
      propagates uncaught. Calls ``run_microtask_review`` with only
      ``qa_agent`` enabled among the external review agents (build and lint
      skipped; the LLM-fallback code-review step still runs and contributes
      to ``r.issues``) and ``tool_agents`` scoped to
      ``ToolAgentKind.TESTING_QA`` via ``scope_tool_agents_by_kind`` (``None``
      when that kind isn't wired), then filters ``r.issues`` to
      ``source == "qa"`` before returning, discarding the code-review issues
      and any other non-QA-sourced ones. ``passed`` is computed as
      ``not qa_issues`` (true iff no QA-sourced issue survives filtering)
      rather than taken from ``r.passed`` — a stray non-QA issue in
      ``r.issues`` cannot fail this gate.
    """
    r = run_microtask_review(
        llm=llm,
        task=task,
        microtask=microtask,
        repo_path=repo_path,
        files=files,
        build_verifier=None,
        qa_agent=deps.qa_agent,
        security_agent=None,
        code_review_agent=None,
        linting_tool_agent=None,
        tool_agents=scope_tool_agents_by_kind(deps.tool_agents, ToolAgentKind.TESTING_QA),
        detail_callback=detail_callback,
        cache=cache,
        tool_agent_cache=deps.tool_agent_cache,
    )
    qa_issues = [i for i in r.issues if i.source == "qa"]
    return GateOutcome(passed=not qa_issues, issues=qa_issues, summary=r.summary)


def _security_gate(
    *,
    llm: LLMClient,
    task: Task,
    microtask: Any,
    repo_path: Path,
    files: Dict[str, str],
    deps: ReviewDependencies,
    detail_callback: Callable[[str], None],
    cache: Optional[AgentReviewCache] = None,
) -> GateOutcome:
    """Run the frontend security gate, keeping only ``source == "security"`` issues.

    Disables the external ``qa_agent``/``code_review_agent``/
    ``linting_tool_agent`` and passes ``build_verifier=None`` — build and
    lint are then genuinely skipped by ``run_microtask_review``.
    ``code_review_agent=None`` does not skip code review, though: the shared
    ``_code_review_step`` still runs its LLM-fallback reviewer whenever no
    external agent is supplied, and the fan-out calls it unconditionally, so
    a code-review LLM call happens on every invocation of this gate; its
    issues are filtered out below, not never produced.

    ``cache``: forwarded to ``run_microtask_review`` as the pre-existing
    per-agent QA/security LLM cache (unrelated to ``tool_agent_cache``).
    ``deps.tool_agent_cache`` is forwarded separately so a ``security``
    tool-agent result already computed by the CR gate's fan-out this cycle is
    reused here instead of re-invoked.

    Preconditions: ``deps.security_agent``/``deps.tool_agents`` are set
      consistently with what the caller wants exercised; ``files`` is the
      microtask's current ``{path: content}`` output.
    Postconditions: the review/agent logic itself never raises (an outright
      security-agent or tool-agent failure is contained to a synthetic issue
      or a logged warning); an exception from ``detail_callback`` — which is
      invoked outside that containment and, in the gated loop, forwards to
      the caller-supplied ``progress_callback`` — is not caught here and
      propagates uncaught. Calls ``run_microtask_review`` with only
      ``security_agent`` enabled among the external review agents (build and
      lint skipped; the LLM-fallback code-review step still runs and
      contributes to ``r.issues``) and ``tool_agents`` scoped to
      ``ToolAgentKind.SECURITY`` via ``scope_tool_agents_by_kind`` (``None``
      when that kind isn't wired), then filters ``r.issues`` to
      ``source == "security"`` before returning, discarding the code-review
      issues and any other non-security-sourced ones. ``passed`` is computed
      as ``not sec_issues`` (true iff no security-sourced issue survives
      filtering) rather than taken from ``r.passed`` — a stray non-security
      issue in ``r.issues`` cannot fail this gate.
    """
    r = run_microtask_review(
        llm=llm,
        task=task,
        microtask=microtask,
        repo_path=repo_path,
        files=files,
        build_verifier=None,
        qa_agent=None,
        security_agent=deps.security_agent,
        code_review_agent=None,
        linting_tool_agent=None,
        tool_agents=scope_tool_agents_by_kind(deps.tool_agents, ToolAgentKind.SECURITY),
        detail_callback=detail_callback,
        cache=cache,
        tool_agent_cache=deps.tool_agent_cache,
    )
    sec_issues = [i for i in r.issues if i.source == "security"]
    return GateOutcome(passed=not sec_issues, issues=sec_issues, summary=r.summary)


def _run_batch_coding_fixes(**kwargs: Any) -> Any:
    """Lazy binding of the frontend batch-fix runner (kept per-team for its Agent patch surface).

    Preconditions: ``kwargs`` matches
      ``.problem_solving.run_batch_coding_fixes``'s signature.
    Postconditions: returns that function's result unchanged; the import is
      deferred to call time so this module has no import-time dependency on
      ``.problem_solving`` (``problem_solving.py`` imports ``._profile`` at
      module level, so the reverse import here must stay lazy to avoid a
      circular import).
    """
    from .problem_solving import run_batch_coding_fixes

    return run_batch_coding_fixes(**kwargs)


_exec_bindings = build_execution_bindings(
    models=_models,
    profile=PROFILE,
    execution_prompt=EXECUTION_PROMPT,
    parse_files_and_summary=parse_files_and_summary_template,
    run_code_review_gate=_code_review_gate,
    run_qa_gate=_qa_gate,
    run_security_gate=_security_gate,
    run_batch_coding_fixes=_run_batch_coding_fixes,
    run_documentation_self_review=run_documentation_self_review,
    # DbC comments self-review: a non-blocking, best-effort step that inserts
    # Design-by-Contract comments into a completed microtask's files after the
    # review-gate cycles pass and before Documentation. The shared reusable
    # reviewer is assigned directly (not via a lazy wrapper): it lives in the
    # shared package with no circular-import constraint, and callers rely on it
    # being this exact callable. Gated at the call site by `enable_dbc_comments`.
    # Frontend's non-Python files have no AST-level insertion safety net of their
    # own, so the shared phase's post-insertion build-verification revert is the
    # sole guard against a bad DbC edit reaching a commit here.
    run_dbc_self_review=run_dbc_comments_review,
    status_code_review=MicrotaskStatus.IN_REVIEW,
    status_qa=MicrotaskStatus.IN_REVIEW,
    status_security=MicrotaskStatus.IN_REVIEW,
    status_qa_security=MicrotaskStatus.IN_QA_SECURITY_TESTING,
    max_total_cycles=lambda config: config.max_retries * 3,
    code_review_retry_cap=lambda config: config.max_retries,
    max_cycles_requires_failing_gate=False,
    startup_log_message=lambda task_id, total, config: (
        f"[{task_id}] Starting execution with batch review flow: "
        f"{total} microtasks, max_retries={config.max_retries}, on_failure={config.on_failure}"
    ),
    gate_issue_log_verb="found",
    # QA and Security are independent analysis calls over the same
    # post-Code-Review snapshot on the frontend too (each gate scopes its
    # tool-agent call to its own kind via ``scope_tool_agents_by_kind`` above) --
    # matching the backend stack's existing concurrent behavior. The CR
    # gate's full ``deps.tool_agents`` fan-out still calls ``testing_qa``/
    # ``security`` a second time, but all three gates share
    # ``deps.tool_agent_cache`` (see the gate-adapter comment above), so the
    # second call within a cycle is served from cache instead of re-invoking
    # the tool agent -- see docs/GATE_DEPENDENCY_GRAPH.md.
    parallelize_qa_security=True,
)

run_execution = _exec_bindings.run_execution
GATE_CONFIG = _exec_bindings.gate_config


def run_execution_with_review_gates(
    *,
    llm: LLMClient,
    task: Task,
    planning_result: Any,
    repo_path: Path,
    architecture: Optional[Any] = None,
    spec_content: str = "",
    existing_code: str = "",
    tool_runners: Optional[Dict[Any, Any]] = None,
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]] = None,
    only_microtask_ids: Optional[List[str]] = None,
    review_config: Optional[Any] = None,
    review_deps: Optional[ReviewDependencies] = None,
) -> Any:
    """
    Execute microtasks with batch-based review cycles.

    After each microtask is coded, it must pass through review phases:
    1. Code Review (build + lint + code review) - batch fix all issues
    2. QA Testing + Security Testing - independent, concurrent analysis passes
       over the same post-Code-Review snapshot (``GATE_CONFIG.parallelize_qa_security``
       is ``True``); batch fix all issues from either, then restart from Code Review
    3. Documentation - self-review loop (3-5 iterations, never fails)

    Key behavior:
    - Each review phase collects ALL issues and sends them to the coding agent at once
    - After QA and/or Security fixes, the flow restarts from Code Review
    - Documentation uses self-review iterations (no failure mode)

    ``progress_callback(current_index, completed, total, title, microtask_phase, phase_detail)`` is called during execution.
    ``current_index`` is the 1-based index of the currently executing microtask.
    ``microtask_phase`` is one of: "coding", "code_review", "qa_testing", "security_testing",
    "qa_security_testing", "documentation", "completed". "qa_security_testing" is reported
    while QA and Security run concurrently (see ``GATE_CONFIG.parallelize_qa_security``);
    it must not be read as "qa_testing has passed".
    ``phase_detail`` provides human-readable detail about the current action.

    Thin wrapper: the loop lives in the shared ``run_gated_execution_impl``,
    parameterised by this module's ``GATE_CONFIG`` -- referenced here by bare
    name (resolved at call time, not captured at import time) so tests can
    monkeypatch it.

    Preconditions:
      - ``review_deps``, if given, supplies whichever of
        ``build_verifier``/``code_review_agent``/``linting_tool_agent``/
        ``qa_agent``/``security_agent``/``tool_agents`` the configured gates
        need; unset ones mean "not available" to ``run_microtask_review``,
        not an error.
    Postconditions:
      - Returns an ``ExecutionResult``; each microtask ends COMPLETED,
        SKIPPED, FAILED or REVIEW_FAILED.
      - Raises ``MicrotaskReviewFailedError`` when a microtask's review fails
        and ``on_failure == "stop"`` (or a security failure with
        ``security_failure_always_stops``).
      - Matching the backend, ``GATE_CONFIG.parallelize_qa_security=True``
        here too: QA and Security run concurrently over the same
        post-Code-Review snapshot, so ``progress_callback`` can report
        ``"qa_security_testing"`` for this team as well.
    """
    return run_gated_execution_impl(
        gate_config=GATE_CONFIG,
        llm=llm,
        task=task,
        planning_result=planning_result,
        repo_path=repo_path,
        architecture=architecture,
        spec_content=spec_content,
        existing_code=existing_code,
        tool_runners=tool_runners,
        progress_callback=progress_callback,
        only_microtask_ids=only_microtask_ids,
        review_config=review_config,
        review_deps=review_deps,
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
    "ReviewDependencies",
    "ToolAgentRunner",
    "run_execution",
    "run_execution_with_review_gates",
    "GATE_CONFIG",
    "_code_review_gate",
    "_qa_gate",
    "_security_gate",
]
