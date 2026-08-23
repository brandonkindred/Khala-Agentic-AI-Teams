"""
Backend stack profile: language/tooling detection + the knobs that select
backend behavior in the shared code-v2 phase implementations.

``_detect_language`` lives here (rather than in ``planning.py``) so the profile
can reference it without importing the heavier phase module — ``planning.py``
re-exports it for callers and tests. See ``shared/stack_profile.py``.
"""

from __future__ import annotations

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
from software_engineering_team.shared.text_utils import has_section_header, toml_has_section
from software_engineering_team.shared.v2_output_templates import _section as _section  # noqa: F401
from software_engineering_team.shared.v2_phase_bindings import build_phase_bindings
from software_engineering_team.shared.v2_review import ReviewConfig
from software_engineering_team.shared.v2_team_config import V2TeamConfig

from .. import models as _models
from ..models import SetupResult, ToolAgentKind, ToolAgentPhaseInput
from ..prompts import (
    DOCUMENTATION_SELF_REVIEW_PROMPT,
    JAVA_CONVENTIONS,
    PLANNING_FIXES_FOR_ISSUES_PROMPT,
    PLANNING_PROMPT,
    PYTHON_CONVENTIONS,
)

logger = logging.getLogger(__name__)

# Backend repo-briefing filter contract: the extensions read into the development
# agent's context and the directories pruned from the walk. Single-sourced here so
# the fresh-walk ``_read_repo_code`` and the incremental ``RepoContextCache`` the
# team lead threads in cannot drift apart (the cache's byte-identical invariant
# depends on them matching).
_BACKEND_REPO_EXTENSIONS = frozenset(
    {".py", ".java", ".kt", ".yaml", ".yml", ".json", ".toml", ".cfg", ".txt"}
)
_BACKEND_REPO_EXCLUDE_DIRS = frozenset({"node_modules", ".git", "__pycache__", "venv", ".venv"})
# Character budget for the repo briefing (whole files only; the next chunk that
# would exceed it stops the briefing).
_BACKEND_REPO_BRIEFING_MAX_CHARS = 30_000


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


def _detect_tooling(repo_path: Path) -> Tuple[bool, bool]:
    """Return ``(has_lint, has_test)`` for the configured backend tooling.

    Detects ruff/flake8 (or a ``[tool.ruff]`` block in ``pyproject.toml``) as
    lint, and a ``tests`` dir with a pytest config (``pytest.ini`` or a
    ``[tool.pytest`` block in ``pyproject.toml``) as testing. Reads
    ``pyproject.toml`` once and reuses it for both probes. Lint also
    recognises a ``[flake8]`` section in ``setup.cfg`` — a common flake8
    config location that the file-name-only ``.flake8`` probe would miss.

    The ``[tool.ruff]`` / ``[tool.pytest`` pyproject checks use the shared
    ``toml_has_section`` helper: a real TOML parse (stdlib ``tomllib`` on
    Python 3.11+, the ``tomli`` backport if installed) that asks whether the
    table actually exists, so a section header appearing inside a
    multi-line string value can no longer produce a false positive; on
    Python 3.10 without ``tomli`` (or on unparseable TOML) it falls back to
    the line-anchored ``has_section_header`` text scan. The ``[flake8]``
    ``setup.cfg`` probe stays on ``has_section_header`` (INI has no
    multi-line strings, so the text scan is exact there). No hard dependency
    is added: 3.11+ stdlib covers the real runtime, and 3.10 keeps the prior
    best-effort text probe. The pre-flight only decides whether to fail the
    task early for missing tooling, so a residual false positive errs toward
    proceeding (a real build/lint gate still enforces correctness).

    Preconditions: ``repo_path`` is a directory.
    Postconditions: returns two booleans. Raises ``AssertionError`` if the
      precondition is violated (a non-directory ``repo_path`` is a caller
      bug, not a runtime failure mode this method recovers from).
    """
    assert repo_path.is_dir(), "repo_path must be a directory"
    pyproject_path = repo_path / "pyproject.toml"
    pyproject_text = (
        pyproject_path.read_text(encoding="utf-8", errors="replace")
        if pyproject_path.exists()
        else ""
    )
    setup_cfg_path = repo_path / "setup.cfg"
    setup_cfg_text = (
        setup_cfg_path.read_text(encoding="utf-8", errors="replace")
        if setup_cfg_path.exists()
        else ""
    )
    has_lint = (
        (repo_path / "ruff.toml").exists()
        or (repo_path / ".flake8").exists()
        or toml_has_section(pyproject_text, "[tool.ruff]")
        or has_section_header(setup_cfg_text, "[flake8]")
    )
    has_test = (repo_path / "tests").is_dir() and (
        (repo_path / "pytest.ini").exists() or toml_has_section(pyproject_text, "[tool.pytest")
    )
    return has_lint, has_test


PROFILE = StackProfile(
    name="backend",
    default_language="python",
    planning_language_label="Language",
    planning_progress_label="language",
    conventions_by_language={"java": JAVA_CONVENTIONS, "_default": PYTHON_CONVENTIONS},
    has_language_conventions=True,
    build_verify_label="backend_code_v2",
    detect_language=_detect_language,
    repo_extensions=_BACKEND_REPO_EXTENSIONS,
    repo_exclude_dirs=_BACKEND_REPO_EXCLUDE_DIRS,
    repo_max_chars=_BACKEND_REPO_BRIEFING_MAX_CHARS,
    detect_tooling=_detect_tooling,
)


# ---------------------------------------------------------------------------
# Setup phase: the config-driven ``run_setup``/``configure_quality_tooling``
# entry points, bound once here from the shared implementation
# (``shared/phases/setup.py``) via this team's Python lint/test hooks. Replaces
# the former per-team ``phases/setup.py`` wrapper module. ``commit_paths`` stays
# a module-level name here (rather than imported inside the shared impl) so
# this module remains the monkeypatch boundary for the scaffolding-commit
# tests.
# ---------------------------------------------------------------------------


def _ensure_linting_configured(path: Path, written: set) -> bool:
    """Verify that a Python linter is configured in the project.

    Checks for ruff.toml, [tool.ruff] in pyproject.toml, .flake8, or [flake8]
    in setup.cfg. If none are found, creates a minimal pyproject.toml with ruff
    configuration so linting never silently skips.

    Side effect: every repo-relative path this call creates or modifies is added
    to ``written`` so the caller can commit exactly what setup touched.
    """
    ruff_toml = path / "ruff.toml"
    pyproject = path / "pyproject.toml"
    flake8_cfg = path / ".flake8"
    setup_cfg = path / "setup.cfg"

    # Check existing config files
    if ruff_toml.exists():
        logger.info("Setup: linting already configured via ruff.toml")
        return True
    if pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="replace")
            if "[tool.ruff]" in text:
                logger.info("Setup: linting already configured via pyproject.toml [tool.ruff]")
                return True
        except Exception as e:
            logger.debug("Setup: could not read pyproject.toml for lint config check: %s", e)
    if flake8_cfg.exists():
        logger.info("Setup: linting already configured via .flake8")
        return True
    if setup_cfg.exists():
        try:
            text = setup_cfg.read_text(encoding="utf-8", errors="replace")
            if "[flake8]" in text:
                logger.info("Setup: linting already configured via setup.cfg [flake8]")
                return True
        except Exception as e:
            logger.debug("Setup: could not read setup.cfg for lint config check: %s", e)

    # No linting config found — create minimal ruff config in pyproject.toml
    logger.info("Setup: no linting configuration found; creating pyproject.toml with ruff config")
    from shared.command_runner.scaffolding import _MINIMAL_PYPROJECT_TOML

    if pyproject.exists():
        # Append ruff config to existing pyproject.toml
        existing = pyproject.read_text(encoding="utf-8", errors="replace")
        if "[tool.ruff]" not in existing:
            ruff_section = (
                "\n[tool.ruff]\n"
                'target-version = "py310"\n'
                "line-length = 120\n\n"
                "[tool.ruff.lint]\n"
                'select = ["E", "F", "I", "N", "W", "UP", "B", "SIM"]\n'
                'ignore = ["E501"]\n'
            )
            pyproject.write_text(existing + ruff_section, encoding="utf-8")
            written.add("pyproject.toml")
    else:
        pyproject.write_text(_MINIMAL_PYPROJECT_TOML, encoding="utf-8")
        written.add("pyproject.toml")
    return True


def _ensure_testing_configured(path: Path, written: set) -> bool:
    """Verify that a Python test framework is configured in the project.

    Checks for both a pytest config (pytest.ini or [tool.pytest] in
    pyproject.toml) AND a tests/ directory — both must be present to count as
    "already configured". Creates whichever is missing so tests never
    silently skip.

    Side effect: every repo-relative path this call creates or modifies is added
    to ``written`` so the caller can commit exactly what setup touched.
    """
    pytest_ini = path / "pytest.ini"
    pyproject = path / "pyproject.toml"
    tests_dir = path / "tests"

    has_pytest_config = pytest_ini.exists()
    if not has_pytest_config and pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="replace")
            has_pytest_config = "[tool.pytest" in text
        except Exception as e:
            logger.debug("Setup: could not read pyproject.toml for pytest config check: %s", e)

    if has_pytest_config and tests_dir.exists():
        logger.info("Setup: testing already configured")
        return True

    # Ensure tests directory exists
    if not tests_dir.exists():
        logger.info("Setup: creating tests/ directory")
        tests_dir.mkdir(parents=True, exist_ok=True)
        init_file = tests_dir / "__init__.py"
        if not init_file.exists():
            init_file.write_text("", encoding="utf-8")
            written.add("tests/__init__.py")
        test_file = tests_dir / "test_main.py"
        if not test_file.exists():
            test_file.write_text(
                '"""Minimal test so pytest runs."""\n\ndef test_health():\n    assert True\n',
                encoding="utf-8",
            )
            written.add("tests/test_main.py")

    # Ensure pytest config exists
    if not has_pytest_config:
        logger.info("Setup: no pytest configuration found; adding to pyproject.toml")
        if pyproject.exists():
            existing = pyproject.read_text(encoding="utf-8", errors="replace")
            if "[tool.pytest" not in existing:
                pytest_section = (
                    '\n[tool.pytest.ini_options]\naddopts = "-v"\ntestpaths = ["tests"]\n'
                )
                pyproject.write_text(existing + pytest_section, encoding="utf-8")
                written.add("pyproject.toml")
        else:
            # If pyproject.toml doesn't exist yet (unlikely after linting setup), use pytest.ini
            pytest_ini.write_text(
                "[pytest]\naddopts = -v\ntestpaths = tests\n",
                encoding="utf-8",
            )
            written.add("pytest.ini")

    return True


def configure_quality_tooling(repo_path: Path) -> Tuple[bool, bool]:
    """Ensure lint/test scaffolding exists on the CURRENT branch and commit it.

    Delegates to the shared implementation, injecting this team's Python
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
    Ensure the repository is initialized and ready for development.

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


# ---------------------------------------------------------------------------
# V2TeamConfig: the single source of truth for backend's team-specific knobs.
# Defined here (alongside PROFILE) so that both the orchestrator and tool agents
# can import it without creating a circular dependency.
# ---------------------------------------------------------------------------

BACKEND_CONFIG = V2TeamConfig(
    stack_profile=PROFILE,
    tool_agent_kinds=frozenset(k.value for k in ToolAgentKind if k is not ToolAgentKind.GENERAL),
    extra_review_clause="",
    output_template_path_prefixes=("backend/", "./backend/"),
    output_template_allowed_languages=("python", "java"),
    output_template_coerce_unknown=True,
)


# ---------------------------------------------------------------------------
# Phase bindings: the config-driven documentation/planning/output-template
# entry points, bound once here from the shared implementations via
# ``BACKEND_CONFIG`` (see ``shared/v2_phase_bindings.py``). Replaces the
# former per-team ``phases/documentation.py``, ``phases/planning.py``, and
# ``output_templates.py`` twin wrapper modules.
# ---------------------------------------------------------------------------

_bindings = build_phase_bindings(
    models=_models,
    config=BACKEND_CONFIG,
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
# points, bound once here from the shared implementation via BACKEND_CONFIG
# and REVIEW_CONFIG (see shared/v2_review_bindings.py). Replaces the former
# per-team phases/review.py wrapper module. Each partial rebinds only
# config/review_config (and, for doc self-review, prompt/parser) -- the
# wrapped functions still resolve their sibling helpers
# (_run_llm_review/_run_qa_agent/_run_security_agent/_run_build_verification)
# by bare name inside shared.v2_review_bindings on every call, so that module
# stays the test patch surface (see its module docstring).
# ---------------------------------------------------------------------------

run_review = partial(
    v2_review_bindings.run_review, config=BACKEND_CONFIG, review_config=REVIEW_CONFIG
)
run_microtask_review = partial(
    v2_review_bindings.run_microtask_review, config=BACKEND_CONFIG, review_config=REVIEW_CONFIG
)
run_code_review_phase = partial(v2_review_bindings.run_code_review_phase, config=BACKEND_CONFIG)
run_qa_testing_phase = partial(
    v2_review_bindings.run_qa_testing_phase, config=BACKEND_CONFIG, review_config=REVIEW_CONFIG
)
run_security_testing_phase = partial(
    v2_review_bindings.run_security_testing_phase,
    config=BACKEND_CONFIG,
    review_config=REVIEW_CONFIG,
)
run_documentation_self_review = partial(
    v2_review_bindings.run_documentation_self_review,
    prompt=DOCUMENTATION_SELF_REVIEW_PROMPT,
    parse_template=parse_documentation_self_review_template,
)
_run_llm_review = partial(v2_review_bindings._run_llm_review, config=BACKEND_CONFIG)
_run_qa_agent = v2_review_bindings._run_qa_agent
_run_security_agent = v2_review_bindings._run_security_agent
_run_build_verification = partial(v2_review_bindings._run_build_verification, config=BACKEND_CONFIG)

__all__ = [
    "PROFILE",
    "REVIEW_CONFIG",
    "BACKEND_CONFIG",
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
