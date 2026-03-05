"""Accessibility tool agent for frontend-code-v2: WCAG 2.2 compliance review and fixes."""

from __future__ import annotations

import json
import logging
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
from software_engineering_team.shared.coding_standards import CODING_STANDARDS

if TYPE_CHECKING:
    from software_engineering_team.shared.llm import LLMClient

logger = logging.getLogger(__name__)

MAX_ACCESSIBILITY_CODE_CHARS = 12_000
MAX_RELEVANT_CODE_CHARS = 8_000

ACCESSIBILITY_REVIEW_PROMPT = """You are an expert Accessibility Engineer specializing in WCAG 2.2 compliance. Your job is to review frontend code and produce a list of well-defined accessibility issues for the coding agent to fix. You do NOT write fixes yourself – the coding agent implements them.

""" + CODING_STANDARDS + """

**Your expertise:**
- WCAG 2.2 (Web Content Accessibility Guidelines) – Perceivable, Operable, Understandable, Robust
- Semantic HTML, ARIA attributes, keyboard navigation, focus management
- Screen reader compatibility, color contrast, text alternatives
- Form labels, error identification, responsive and touch targets
- Component library accessibility patterns (Material UI, Angular Material, Vuetify, etc.)

**Input:**
- Code to review (JSX/TSX, HTML templates, TypeScript/JavaScript components, CSS/SCSS)
- Language (typescript, javascript, react, vue, angular)
- Optional: task description, architecture

**Your task:**
1. Review the code for WCAG 2.2 compliance. Check for: missing alt text, poor color contrast, missing labels, keyboard traps, insufficient focus indicators, non-semantic markup, missing ARIA where needed, form accessibility, etc.
2. For each issue found, produce a well-defined report with a clear "recommendation" – what the coding agent should implement to fix it.
3. Reference the specific WCAG 2.2 criterion (e.g. 1.1.1 Non-text Content, 2.1.1 Keyboard, 2.4.3 Focus Order, 4.1.2 Name, Role, Value).
4. Do NOT produce fixed_code. Return issues only. The coding agent will implement fixes and commit to the feature branch.

**Output format:**
Return a single JSON object with:
- "issues": list of objects, each with:
  - "severity": string (critical, high, medium, low) – critical/high block merge
  - "wcag_criterion": string (e.g. "1.1.1", "2.2.1", "4.1.2")
  - "description": string (what the accessibility problem is)
  - "file_path": string (file path or component name)
  - "recommendation": string (REQUIRED – concrete instruction for the coding agent: what code to add/change to fix this)
- "summary": string (overall WCAG 2.2 compliance assessment)

**Approval rule:** Code is approved when there are no critical or high severity issues. Medium/low issues may be acceptable for merge but should still be listed.

If no issues are found, return empty issues list. Be thorough. Each recommendation must be actionable – the coding agent should know exactly what to implement.

Respond with valid JSON only. No explanatory text outside JSON.

---

**Task:** {task_description}

**Code to review:**
{code}
"""


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


class AccessibilityToolAgent:
    """Accessibility tool agent: WCAG 2.2 compliance review and fixes one at a time."""

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("Accessibility: microtask %s (execute stub)", inp.microtask.id)
        return ToolAgentOutput(summary="Accessibility execute — no changes applied.")

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=[
                "Consider WCAG 2.2 compliance: alt text, labels, keyboard navigation, focus indicators.",
                "Use semantic HTML elements (button, nav, main, header, footer).",
                "Add ARIA attributes where native semantics are insufficient.",
            ],
            summary="Accessibility planning: WCAG and semantic markup recommendations.",
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Find accessibility issues in current code. Returns issues with source=accessibility."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="Accessibility review skipped (no LLM).")
        code_text = "\n\n".join(
            f"--- {p} ---\n{c}" for p, c in list(inp.current_files.items())[:20]
        )[:MAX_ACCESSIBILITY_CODE_CHARS]
        if not code_text.strip():
            return ToolAgentPhaseOutput(summary="Accessibility review skipped (no code).")
        prompt = ACCESSIBILITY_REVIEW_PROMPT.format(
            task_description=inp.task_description or "N/A",
            code=code_text,
        )
        try:
            raw = self.llm.complete_text(prompt)
        except Exception as e:
            logger.warning("Accessibility review LLM call failed: %s", e)
            return ToolAgentPhaseOutput(summary="Accessibility review failed (LLM error).")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    data = json.loads(raw[start:end])
                except json.JSONDecodeError:
                    data = {}
            else:
                data = {}
        issues: List[ReviewIssue] = []
        for item in data.get("issues") or []:
            if isinstance(item, dict):
                issues.append(
                    ReviewIssue(
                        source="accessibility",
                        severity=item.get("severity", "medium"),
                        description=item.get("description", ""),
                        file_path=item.get("file_path", ""),
                        recommendation=item.get("recommendation", ""),
                    )
                )
        return ToolAgentPhaseOutput(
            issues=issues,
            summary=f"Accessibility review: {len(issues)} issue(s) found.",
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix accessibility-owned issues one at a time."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="Accessibility problem_solve skipped (no LLM).")
        a11y_issues = [
            i
            for i in inp.review_issues
            if (i.source or "").strip() in ("accessibility", "tool_accessibility")
        ]
        if not a11y_issues:
            return ToolAgentPhaseOutput(summary="No accessibility issues to fix.")
        merged = dict(inp.current_files)
        fixed_count = 0
        for issue in a11y_issues:
            relevant_code = _relevant_code_for_issue(issue, merged)
            prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT.format(
                source=issue.source or "accessibility",
                severity=issue.severity or "medium",
                description=issue.description or "",
                file_path=issue.file_path or "N/A",
                recommendation=issue.recommendation or "Fix the accessibility issue.",
                current_code=relevant_code,
            )
            try:
                raw = self.llm.complete_text(prompt)
            except Exception as e:
                logger.warning(
                    "Accessibility fix for issue %s failed: %s",
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
            summary=f"Accessibility: fixed {fixed_count} of {len(a11y_issues)} issue(s) (one at a time).",
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Accessibility deliver.")
