"""Shared build-specialist tool agent: run the real build, report one issue per failure.

Both code-v2 stacks ship a "build specialist" tool agent that runs the project
build in the review phase, emits one :class:`ReviewIssue` per parsed failure, and
fixes them one at a time via ``problem_solve`` (opted into via
:class:`~software_engineering_team.shared.tool_agent_base.SingleIssueProblemSolveMixin`
— unlike the review-lens tool agents, a build failure isn't a subjective
opinion about someone else's code, so this agent owns fixing what it finds).
The two stacks differ only in:

* the build runner — backend runs a Python syntax check plus ``pytest``;
  frontend runs the detected JS framework build, and
* the review noun used in the summary, and
* the backend additionally feeds python/java conventions to its single-issue fix
  prompt (via :class:`BackendReviewToolAgent`) and ships bespoke ``execute`` /
  ``deliver`` stubs.

This module owns the two build runners and the shared base
(:class:`BuildSpecialistToolAgentBase`); each stack ships only a thin profile
that sets ``build_runner``/``build_review_noun`` and its team-specific
single-issue prompt + parser. The concrete per-stack ``agent.py`` keeps a
top-level ``from strands import Agent`` so tests can monkeypatch it — the shared
base resolves ``Agent`` from the concrete subclass module.

This is distinct from ``build_fix_specialist`` (a global, LLM-driven minimal-edit
fixer with its own input/output contract); these runners are per-microtask build
verifiers.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import List

from software_engineering_team.shared.tool_agent_base import (
    BaseReviewToolAgent,
    SingleIssueProblemSolveMixin,
)
from software_engineering_team.shared.v2_models import ReviewIssue

logger = logging.getLogger(__name__)

MAX_RELEVANT_CODE_CHARS = 8_000


def run_backend_build_and_parse(repo_path: Path) -> List[ReviewIssue]:
    """Run backend syntax check and optionally pytest; return one issue per failure.

    Preconditions: ``repo_path`` is a ``pathlib.Path`` to an existing directory.
    Postconditions: returns a list of :class:`ReviewIssue` (empty when no Python
    project is present or the build/tests pass). Never raises for a missing
    project — returns ``[]`` instead.
    """
    try:
        from shared.command_runner.executor import (
            run_command,
            run_pytest,
            run_python_syntax_check,
        )
    except ImportError:  # pragma: no cover  # defensive: command_runner is always importable here
        logger.warning("Build Specialist: shared.command_runner not available")
        return []
    backend_dir = repo_path if any(repo_path.rglob("*.py")) else repo_path / "backend"
    if not backend_dir.exists() or not any(backend_dir.rglob("*.py")):
        logger.info("Build Specialist: no Python project at %s", repo_path)
        return []
    issues: List[ReviewIssue] = []

    result = run_python_syntax_check(backend_dir)
    if not result.success:
        stderr = (result.stderr or "").strip()
        if stderr.startswith("Syntax errors found:"):
            for line in stderr.split("\n")[1:]:
                line = line.strip()
                if not line or ":" not in line:
                    continue
                # Line format is "<path>: <message>"; split on the ": " delimiter
                # (not the first bare colon) so a Windows drive letter (e.g. "C:\\")
                # and colon-bearing messages are preserved.
                path, _, msg = line.partition(": ")
                path, msg = path.strip(), msg.strip()
                if path and msg:
                    issues.append(
                        ReviewIssue(
                            source="build_specialist",
                            severity="critical",
                            description=msg,
                            file_path=path,
                            recommendation="Fix the syntax error in this file.",
                        )
                    )
        if not issues:
            issues.append(
                ReviewIssue(
                    source="build_specialist",
                    severity="critical",
                    description=result.error_summary,
                    recommendation="Fix the syntax errors.",
                )
            )
        return issues

    tests_dir = backend_dir / "tests"
    if tests_dir.exists() and any(tests_dir.rglob("test_*.py")):
        req_txt = backend_dir / "requirements.txt"
        if req_txt.exists():
            try:
                run_command(
                    [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                    cwd=backend_dir,
                    timeout=120,
                )
            except Exception as e:
                logger.warning("Build Specialist: pip install failed (non-fatal): %s", e)
        test_result = run_pytest(backend_dir, python_exe=sys.executable)
        if not test_result.success:
            failures = test_result.parsed_failures("pytest")
            for f in failures:
                rec = (f.suggestion or f.playbook_hint or "Fix the test or implementation.").strip()
                issues.append(
                    ReviewIssue(
                        source="build_specialist",
                        severity="critical",
                        description=(f.message or f.raw_excerpt or ""),
                        file_path=(f.file_path or ""),
                        recommendation=rec,
                    )
                )
            if not issues:
                issues.append(
                    ReviewIssue(
                        source="build_specialist",
                        severity="critical",
                        description=test_result.pytest_error_summary(),
                        recommendation="Fix the failing tests.",
                    )
                )
    return issues


def run_frontend_build_and_parse(repo_path: Path) -> List[ReviewIssue]:
    """Run the frontend build and return one issue per parsed failure.

    Preconditions: ``repo_path`` is a ``pathlib.Path`` to an existing directory.
    Postconditions: returns a list of :class:`ReviewIssue` (empty when no frontend
    project is present or the build passes). Never raises for a missing project.
    """
    try:
        from shared.command_runner.executor import (
            detect_frontend_framework,
            run_frontend_build,
        )
    except ImportError:  # pragma: no cover  # defensive: command_runner is always importable here
        logger.warning("Build Specialist: shared.command_runner not available")
        return []
    frontend_dir = repo_path if (repo_path / "package.json").exists() else repo_path / "frontend"
    if not (frontend_dir / "package.json").exists():
        logger.info("Build Specialist: no frontend project at %s", repo_path)
        return []
    result = run_frontend_build(frontend_dir)
    if result.success:
        return []
    # Detect framework and use appropriate error parsing. The Angular-specific
    # parser only understands ng build output; React/Vue (and anything else) use
    # the generic fallback so their errors aren't mis-parsed by the ng parser.
    detected_framework = detect_frontend_framework(frontend_dir)
    parse_kind = "ng_build" if detected_framework == "angular" else "generic"
    failures = result.parsed_failures(parse_kind)
    issues: List[ReviewIssue] = []
    for f in failures:
        rec = (f.suggestion or f.playbook_hint or "Fix the build error.").strip()
        issues.append(
            ReviewIssue(
                source="build_specialist",
                severity="critical",
                description=(f.message or f.raw_excerpt or ""),
                file_path=(f.file_path or ""),
                recommendation=rec,
            )
        )
    if not issues:
        issues.append(
            ReviewIssue(
                source="build_specialist",
                severity="critical",
                description=result.error_summary,
                recommendation="Fix the build error.",
            )
        )
    return issues


class BuildSpecialistToolAgentBase(SingleIssueProblemSolveMixin, BaseReviewToolAgent):
    """Shared base for the per-stack build-specialist tool agents.

    ``review`` runs the configured :attr:`build_runner` over the resolved
    ``repo_path`` (inherited from :class:`BaseReviewToolAgent`); ``problem_solve``
    fixes the reported build issues one at a time. Concrete profiles must set
    :attr:`build_runner` (a ``staticmethod``), :attr:`build_review_noun`,
    :attr:`problem_solving_prompt`, and :attr:`_parse_single_issue`.

    Invariants: instance state is limited to ``_model`` and ``llm`` (no
    build-specialist instance state), so tests that build instances via
    ``__new__`` behave identically to constructed ones.
    """

    name = "Build Specialist"
    empty_label = "build issues"
    issue_source = "build_specialist"
    problem_solve_sources = ("build", "build_specialist", "tool_build_specialist")
    max_relevant_code_chars = MAX_RELEVANT_CODE_CHARS
    default_severity = "critical"
    default_recommendation = "Fix the build error."
    plan_recommendations = ["Ensure build configuration and dependencies are in scope."]
    plan_summary = "Build Specialist planning."
