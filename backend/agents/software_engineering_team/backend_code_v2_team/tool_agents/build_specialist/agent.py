"""
Build Specialist tool agent for backend-code-v2: identifies all build/test issues in review and fixes them one at a time.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import List

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from ...models import (
    ReviewIssue,
    ToolAgentInput,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)
from ...output_templates import parse_problem_solving_single_issue_template
from ...prompts import PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT
from ..base import BackendReviewToolAgent

logger = logging.getLogger(__name__)

MAX_RELEVANT_CODE_CHARS = 8_000


def _run_backend_build_and_parse(repo_path: Path) -> List[ReviewIssue]:
    """Run backend syntax check and optionally pytest; return one ReviewIssue per parsed failure."""
    try:
        from software_engineering_team.shared.command_runner import (
            run_command,
            run_pytest,
            run_python_syntax_check,
        )
    except ImportError:
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
                path, _, msg = line.partition(":")
                path, msg = path.strip(), msg.strip()
                if path and msg:
                    issues.append(
                        ReviewIssue(
                            source="build_specialist",
                            severity="critical",
                            description=msg[:500],
                            file_path=path[:300],
                            recommendation="Fix the syntax error in this file.",
                        )
                    )
        if not issues:
            issues.append(
                ReviewIssue(
                    source="build_specialist",
                    severity="critical",
                    description=result.error_summary[:500],
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
                        description=(f.message or f.raw_excerpt or "")[:500],
                        file_path=(f.file_path or "")[:300],
                        recommendation=rec[:500],
                    )
                )
            if not issues:
                issues.append(
                    ReviewIssue(
                        source="build_specialist",
                        severity="critical",
                        description=test_result.pytest_error_summary()[:500],
                        recommendation="Fix the failing tests.",
                    )
                )
    return issues


class BuildSpecialistAdapterAgent(BackendReviewToolAgent):
    """Identifies all build/test issues in review and fixes them one at a time in problem_solve.

    ``review`` runs the backend build via the shared ``build_runner`` path;
    ``execute``/``deliver`` are bespoke stubs for the backend build flow;
    ``problem_solve`` (with python/java conventions) is inherited.
    """

    name = "Build Specialist"
    empty_label = "build issues"
    issue_source = "build_specialist"
    problem_solve_sources = ("build", "build_specialist", "tool_build_specialist")
    problem_solving_prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT
    max_relevant_code_chars = MAX_RELEVANT_CODE_CHARS
    default_severity = "critical"
    default_recommendation = "Fix the build error."
    plan_recommendations = ["Ensure build configuration and dependencies are in scope."]
    plan_summary = "Build Specialist planning."
    build_runner = staticmethod(_run_backend_build_and_parse)
    build_review_noun = "build/test issue(s)"
    _parse_single_issue = staticmethod(parse_problem_solving_single_issue_template)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("Build Specialist: microtask %s (execute stub)", inp.microtask.id)
        return ToolAgentOutput(
            summary="Build Specialist execute — no changes applied.",
            recommendations=["Integrate with build verifier or build-fix flow for full support."],
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            summary="Build Specialist deliver — ensure build passes before merge."
        )
