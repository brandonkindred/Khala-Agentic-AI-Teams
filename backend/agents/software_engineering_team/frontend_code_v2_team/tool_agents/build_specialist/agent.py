"""Build Specialist tool agent for frontend-code-v2: identifies all build issues in review and fixes them one at a time."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.shared.tool_agent_base import (
    BaseReviewToolAgent,
    relevant_code_for_issue,
)

from ...models import ReviewIssue, ToolAgentPhaseInput, ToolAgentPhaseOutput
from ...output_templates import parse_problem_solving_single_issue_template
from ...prompts import PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT

logger = logging.getLogger(__name__)

MAX_RELEVANT_CODE_CHARS = 8_000


def _relevant_code_for_issue(issue: ReviewIssue, current_files: Dict[str, str]) -> str:
    """Return code context for a single issue: prefer issue's file, else first files."""
    return relevant_code_for_issue(issue, current_files, MAX_RELEVANT_CODE_CHARS)


def _run_frontend_build_and_parse(repo_path: Path) -> List[ReviewIssue]:
    """Run frontend build and return one ReviewIssue per parsed failure."""
    try:
        from software_engineering_team.shared.command_runner import (
            detect_frontend_framework,
            run_frontend_build,
        )
    except ImportError:
        logger.warning("Build Specialist: shared.command_runner not available")
        return []
    frontend_dir = repo_path if (repo_path / "package.json").exists() else repo_path / "frontend"
    if not (frontend_dir / "package.json").exists():
        logger.info("Build Specialist: no frontend project at %s", repo_path)
        return []
    result = run_frontend_build(frontend_dir)
    if result.success:
        return []
    # Detect framework and use appropriate error parsing
    detected_framework = detect_frontend_framework(frontend_dir)
    # ng_build parser works for Angular; for React/Vue, use the generic fallback
    parse_kind = "ng_build" if detected_framework == "angular" else "ng_build"
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


class BuildSpecialistAdapterAgent(BaseReviewToolAgent):
    """Identifies all build issues in review and fixes them one at a time in problem_solve.

    ``review`` is bespoke (it runs the frontend build); ``problem_solve`` and the
    other lifecycle methods are inherited from :class:`BaseReviewToolAgent`.
    """

    name = "Build Specialist"
    empty_label = "build issues"
    issue_source = "build_specialist"
    problem_solve_sources = ("build", "build_specialist", "tool_build_specialist")
    problem_solving_prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT
    max_relevant_code_chars = MAX_RELEVANT_CODE_CHARS
    default_severity = "critical"
    default_recommendation = "Fix the build error."
    plan_recommendations = ["Ensure build config and dependencies are in scope."]
    plan_summary = "Build Specialist planning."
    _parse_single_issue = staticmethod(parse_problem_solving_single_issue_template)

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Run frontend build and return one issue per parsed failure (identify all issues)."""
        if not inp.repo_path:
            return ToolAgentPhaseOutput(summary="Build Specialist review skipped (no repo_path).")
        path = Path(inp.repo_path).resolve()
        if not path.exists():
            return ToolAgentPhaseOutput(
                summary="Build Specialist review skipped (repo path missing)."
            )
        issues = _run_frontend_build_and_parse(path)
        return ToolAgentPhaseOutput(
            issues=issues,
            summary=f"Build Specialist review: {len(issues)} build issue(s) found.",
        )
