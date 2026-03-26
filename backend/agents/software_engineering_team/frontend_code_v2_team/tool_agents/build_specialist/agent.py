"""Build Specialist tool agent for frontend-code-v2: identifies all build issues in review and fixes them one at a time."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from ...models import (
    ReviewIssue,
    ToolAgentInput,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)
from ...output_templates import parse_problem_solving_single_issue_template
from ...prompts import PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT

if TYPE_CHECKING:
    from llm_service import LLMClient

logger = logging.getLogger(__name__)

MAX_RELEVANT_CODE_CHARS = 8_000


def _relevant_code_for_issue(issue: ReviewIssue, current_files: Dict[str, str]) -> str:
    """Return code context for a single issue: prefer issue's file, else first files."""
    if issue.file_path and issue.file_path in current_files:
        content = current_files[issue.file_path]
        if len(content) <= MAX_RELEVANT_CODE_CHARS:
            return f"--- {issue.file_path} ---\n{content}"
        return f"--- {issue.file_path} ---\n{content[:MAX_RELEVANT_CODE_CHARS]}\n... [truncated]"
    parts: List[str] = []
    total = 0
    for path, content in list(current_files.items())[:10]:
        chunk = f"--- {path} ---\n{content}\n"
        if total + len(chunk) > MAX_RELEVANT_CODE_CHARS:
            remaining = MAX_RELEVANT_CODE_CHARS - total
            if remaining > 200:
                chunk = f"--- {path} ---\n{content[:remaining]}\n... [truncated]"
                parts.append(chunk)
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts) if parts else "(no code)"


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
                description=result.error_summary[:500],
                recommendation="Fix the build error.",
            )
        )
    return issues


class BuildSpecialistAdapterAgent:
    """Identifies all build issues in review and fixes them one at a time in problem_solve."""

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("Build Specialist: microtask %s (execute stub)", inp.microtask.id)
        return ToolAgentOutput(summary="Build Specialist execute — no changes applied.")

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=["Ensure build config and dependencies are in scope."],
            summary="Build Specialist planning.",
        )

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

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix build-related issues one at a time. Only fixes issues with source build or build_specialist."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="Build Specialist problem_solve skipped (no LLM).")
        build_issues = [
            i
            for i in inp.review_issues
            if (i.source or "").strip() in ("build", "build_specialist", "tool_build_specialist")
        ]
        if not build_issues:
            return ToolAgentPhaseOutput(summary="No build issues to fix.")
        merged = dict(inp.current_files)
        fixed_count = 0
        for issue in build_issues:
            relevant_code = _relevant_code_for_issue(issue, merged)
            prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT.format(
                source=issue.source or "build_specialist",
                severity=issue.severity or "critical",
                description=issue.description or "",
                file_path=issue.file_path or "N/A",
                recommendation=issue.recommendation or "Fix the build error.",
                current_code=relevant_code,
            )
            try:
                raw = self.llm.complete_text(prompt, think=True)
            except Exception as e:
                logger.warning(
                    "Build Specialist fix for issue %s failed: %s",
                    (issue.description or "")[:50],
                    e,
                )
                continue
            parsed = parse_problem_solving_single_issue_template(raw)
            fixed_files = parsed.get("files") or {}
            if fixed_files:
                merged.update(fixed_files)
                fixed_count += 1
        return ToolAgentPhaseOutput(
            files=merged,
            summary=f"Build Specialist: fixed {fixed_count} of {len(build_issues)} issue(s) (one at a time).",
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Build Specialist deliver.")
