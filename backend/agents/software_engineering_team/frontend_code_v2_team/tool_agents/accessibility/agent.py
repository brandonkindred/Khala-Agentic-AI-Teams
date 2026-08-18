"""Accessibility tool agent for frontend-code-v2: WCAG 2.2 compliance review."""

from __future__ import annotations

from typing import Dict

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.shared.coding_standards import CODING_STANDARDS
from software_engineering_team.shared.prompt_utils import JSON_OUTPUT_INSTRUCTION
from software_engineering_team.shared.tool_agent_base import (
    BaseReviewToolAgent,
    relevant_code_for_issue,
)

from ...models import ReviewIssue

MAX_RELEVANT_CODE_CHARS = 8_000

ACCESSIBILITY_REVIEW_PROMPT = (
    """You are an expert Accessibility Engineer specializing in WCAG 2.2 compliance. Your job is to review frontend code and produce a list of well-defined accessibility issues for the coding agent to fix. You do NOT write fixes yourself – the coding agent implements them.

"""
    + CODING_STANDARDS
    + """

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
"""
    + JSON_OUTPUT_INSTRUCTION
    + """

---

**Task:** {task_description}

**Code to review:**
{code}
"""
)


def _relevant_code_for_issue(issue: ReviewIssue, current_files: Dict[str, str]) -> str:
    """Return code context for a single issue: prefer issue's file, else first files."""
    return relevant_code_for_issue(issue, current_files, MAX_RELEVANT_CODE_CHARS)


class AccessibilityToolAgent(BaseReviewToolAgent):
    """Accessibility tool agent: WCAG 2.2 compliance review; reports issues for the coding agent to fix."""

    name = "Accessibility"
    empty_label = "accessibility issues"
    issue_source = "accessibility"
    review_prompt = ACCESSIBILITY_REVIEW_PROMPT
    max_relevant_code_chars = MAX_RELEVANT_CODE_CHARS
    review_parse_mode = "json"
    uses_json_model = True
    review_model_attr = "_model_json"
    plan_recommendations = [
        "Consider WCAG 2.2 compliance: alt text, labels, keyboard navigation, focus indicators.",
        "Use semantic HTML elements (button, nav, main, header, footer).",
        "Add ARIA attributes where native semantics are insufficient.",
    ]
    plan_summary = "Accessibility planning: WCAG and semantic markup recommendations."
