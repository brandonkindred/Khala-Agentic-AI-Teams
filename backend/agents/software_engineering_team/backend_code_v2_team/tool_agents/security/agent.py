"""Security tool agent for backend-code-v2: finds security issues in review and fixes them one at a time."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, List, Optional

from ...models import (
    ReviewIssue,
    ToolAgentInput,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)
from ...output_templates import parse_problem_solving_single_issue_template, parse_review_template
from ...prompts import (
    JAVA_CONVENTIONS,
    PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT,
    PYTHON_CONVENTIONS,
    SECURITY_TOOL_AGENT_REVIEW_PROMPT,
)

if TYPE_CHECKING:
    from llm_service import LLMClient

logger = logging.getLogger(__name__)

MAX_SECURITY_CODE_CHARS = 12_000
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


class SecurityToolAgent:
    """Security tool agent: finds security issues in review and fixes them one at a time in problem_solve."""

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("Security: microtask %s (execute stub)", inp.microtask.id)
        return ToolAgentOutput(summary="Security execute — no changes applied.")

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=["Consider injection prevention, auth checks, and secure defaults."],
            summary="Security planning.",
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Find security issues in current code. Returns issues with source=security."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="Security review skipped (no LLM).")
        code_text = "\n\n".join(
            f"--- {p} ---\n{c}" for p, c in list(inp.current_files.items())[:20]
        )[:MAX_SECURITY_CODE_CHARS]
        if not code_text.strip():
            return ToolAgentPhaseOutput(summary="Security review skipped (no code).")
        prompt = SECURITY_TOOL_AGENT_REVIEW_PROMPT.format(
            task_description=inp.task_description or "N/A",
            code=code_text,
        )
        try:
            raw = self.llm.complete_text(prompt, think=True)
        except Exception as e:
            logger.warning("Security review LLM call failed: %s", e)
            return ToolAgentPhaseOutput(summary="Security review failed (LLM error).")
        data = parse_review_template(raw)
        issues: List[ReviewIssue] = []
        for item in data.get("issues") or []:
            if isinstance(item, dict):
                issues.append(
                    ReviewIssue(
                        source="security",
                        severity=item.get("severity", "medium"),
                        description=item.get("description", ""),
                        file_path=item.get("file_path", ""),
                        recommendation=item.get("recommendation", ""),
                    )
                )
        return ToolAgentPhaseOutput(
            issues=issues,
            summary=f"Security review: {len(issues)} issue(s) found.",
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix security-owned issues one at a time. Only fixes issues with source security or tool_security."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="Security problem_solve skipped (no LLM).")
        security_issues = [
            i
            for i in inp.review_issues
            if (i.source or "").strip() in ("security", "tool_security")
        ]
        if not security_issues:
            return ToolAgentPhaseOutput(summary="No security issues to fix.")
        lang = (inp.language or "python").strip().lower()
        language_conventions = JAVA_CONVENTIONS if lang == "java" else PYTHON_CONVENTIONS
        merged = dict(inp.current_files)
        fixed_count = 0
        for issue in security_issues:
            relevant_code = _relevant_code_for_issue(issue, merged)
            prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT.format(
                language_conventions=language_conventions,
                source=issue.source or "security",
                severity=issue.severity or "medium",
                description=issue.description or "",
                file_path=issue.file_path or "N/A",
                recommendation=issue.recommendation or "Fix the security issue.",
                current_code=relevant_code,
            )
            try:
                raw = self.llm.complete_text(prompt, think=True)
            except Exception as e:
                logger.warning(
                    "Security fix for issue %s failed: %s",
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
            summary=f"Security: fixed {fixed_count} of {len(security_issues)} issue(s) (one at a time).",
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Security deliver.")
