"""
Build Specialist tool agent for backend-code-v2: identifies all build/test issues in review and fixes them one at a time.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Dict, List

from strands import Agent

from llm_service import get_strands_model

from ...models import (
    ReviewIssue,
    ToolAgentInput,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)
from ...output_templates import parse_problem_solving_single_issue_template
from ...prompts import (
    JAVA_CONVENTIONS,
    PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT,
    PYTHON_CONVENTIONS,
)

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


class BuildSpecialistAdapterAgent:
    """Identifies all build/test issues in review and fixes them one at a time in problem_solve."""

    def __init__(self, llm=None) -> None:
        from strands.models.model import Model as _StrandsModel

        self._model = llm if (llm is not None and isinstance(llm, _StrandsModel)) else get_strands_model()
        self.llm = llm  # kept for backward compat checks

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("Build Specialist: microtask %s (execute stub)", inp.microtask.id)
        return ToolAgentOutput(
            summary="Build Specialist execute — no changes applied.",
            recommendations=["Integrate with build verifier or build-fix flow for full support."],
        )

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=["Ensure build configuration and dependencies are in scope."],
            summary="Build Specialist planning.",
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Run backend build (syntax + pytest) and return one issue per parsed failure (identify all issues)."""
        if not inp.repo_path:
            return ToolAgentPhaseOutput(summary="Build Specialist review skipped (no repo_path).")
        path = Path(inp.repo_path).resolve()
        if not path.exists():
            return ToolAgentPhaseOutput(
                summary="Build Specialist review skipped (repo path missing)."
            )
        issues = _run_backend_build_and_parse(path)
        return ToolAgentPhaseOutput(
            issues=issues,
            summary=f"Build Specialist review: {len(issues)} build/test issue(s) found.",
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix build-related issues one at a time. Only fixes issues with source build or build_specialist."""
        if not self._model:
            return ToolAgentPhaseOutput(summary="Build Specialist problem_solve skipped (no LLM).")
        build_issues = [
            i
            for i in inp.review_issues
            if (i.source or "").strip() in ("build", "build_specialist", "tool_build_specialist")
        ]
        if not build_issues:
            return ToolAgentPhaseOutput(summary="No build issues to fix.")
        lang = (inp.language or "python").strip().lower()
        language_conventions = JAVA_CONVENTIONS if lang == "java" else PYTHON_CONVENTIONS
        merged = dict(inp.current_files)
        fixed_count = 0
        for issue in build_issues:
            relevant_code = _relevant_code_for_issue(issue, merged)
            prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT.format(
                language_conventions=language_conventions,
                source=issue.source or "build_specialist",
                severity=issue.severity or "critical",
                description=issue.description or "",
                file_path=issue.file_path or "N/A",
                recommendation=issue.recommendation or "Fix the build error.",
                current_code=relevant_code,
            )
            try:
                raw = (lambda _r: str(_r))(Agent(model=self._model)(prompt)).strip()
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
        return ToolAgentPhaseOutput(
            summary="Build Specialist deliver — ensure build passes before merge."
        )
