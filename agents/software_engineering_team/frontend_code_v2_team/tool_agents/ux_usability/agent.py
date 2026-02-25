"""UX/Usability tool agent for frontend-code-v2: UX design planning and usability review."""

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

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)

MAX_UX_CODE_CHARS = 30_000
MAX_RELEVANT_CODE_CHARS = 8_000

UX_DESIGNER_PLAN_PROMPT = """You are a UX Designer Agent. Your job is to define user flows, information architecture, interaction design, microcopy, and edge cases BEFORE pixels get involved. You ensure the app makes sense from a user perspective.

**Your expertise:**
- User journeys (happy path and sad paths)
- Wireframes and flow diagrams (describe in text)
- Interaction rules (empty states, errors, loading, success)
- Microcopy guidelines (tone, clarity, consistency)
- Edge cases and error handling from a UX perspective

**Input:**
- Task description and requirements
- Optional: spec content, architecture, user story

**Your task:**
Produce UX design artifacts that the UI Designer and Feature Implementation agents will use:

1. **User Journeys** – Describe the happy path and key sad paths (errors, empty states, validation failures). Use clear step-by-step flows.
2. **Wireframes / Flow Diagrams** – Describe the layout and flow in text (screens, key elements, navigation between them). No actual pixels; focus on structure and hierarchy.
3. **Interaction Rules** – Define rules for: empty states (what shows when no data), error states (how errors are displayed), loading states (spinners, skeletons), success states (feedback, confirmation).
4. **Microcopy Guidelines** – Tone (friendly, professional, concise), clarity rules, consistency (button labels, error messages, placeholders). Provide examples where helpful.

**Output format:**
Return a single JSON object with:
- "user_journeys": string (full user journey description: happy path + sad paths)
- "wireframes_summary": string (wireframe/flow description in text)
- "interaction_rules": string (empty, error, loading, success state rules)
- "microcopy_guidelines": string (tone, clarity, consistency guidelines)
- "summary": string (2-3 sentence summary of key UX decisions)

Respond with valid JSON only. No explanatory text outside JSON.

---

**Task:** {task_description}

**Spec (excerpt):**
{spec_content}
"""

UX_ENGINEER_REVIEW_PROMPT = """You are a UX Engineer Agent. Your job is to focus on the feel of the product: performance perception, interaction polish, usability. You catch the stuff users notice immediately but specs rarely mention.

**Your expertise:**
- Interaction polish (focus flow, keyboard shortcuts, friction removal)
- Sensible defaults and progressive disclosure
- Usability review (what feels off, what could be smoother)
- "Delight" without being annoying (motion restraint, feedback timing)

**Input:**
- Code to review (HTML templates, TypeScript components)
- Task description

**Your task:**
Review the code for UX polish and usability. Identify issues that affect the feel of the product:

1. **Focus flow** – Is tab order logical? Are focus indicators visible? Any focus traps?
2. **Keyboard shortcuts** – Are there actions that should have shortcuts? Missing Escape to close?
3. **Friction removal** – Unnecessary clicks? Confusing flows? Could defaults be smarter?
4. **Motion/feedback** – Is feedback timing appropriate? Any jarring or missing transitions? Restraint: delight without being annoying.
5. **Progressive disclosure** – Is information revealed at the right time? Overwhelming or too hidden?

For each issue, produce a code_review-style report with a clear "recommendation" – what the coding agent should implement.

**Output format:**
Return a single JSON object with:
- "issues": list of objects, each with:
  - "severity": string (critical, major, medium, minor)
  - "category": string (focus, keyboard, usability, motion, feedback)
  - "file_path": string (file or component)
  - "description": string (what the UX problem is)
  - "recommendation": string (concrete instruction for the coding agent)
- "summary": string (overall UX polish assessment)
- "approved": boolean (true when no critical/major issues; false when polish pass is needed)

If no issues are found, return empty issues list and approved=true. Be practical – focus on issues that materially affect user experience.

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


class UxUsabilityToolAgent:
    """UX/Usability tool agent: UX design planning and usability review with fixes."""

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("UX/Usability: microtask %s (execute stub)", inp.microtask.id)
        return ToolAgentOutput(summary="UX/Usability execute — no changes applied.")

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Generate UX design artifacts: user journeys, wireframes, interaction rules, microcopy."""
        if not self.llm:
            return ToolAgentPhaseOutput(
                recommendations=[
                    "Consider user flows and interactions.",
                    "Define empty, error, loading, and success states.",
                    "Establish microcopy guidelines for consistency.",
                ],
                summary="UX planning stub (no LLM).",
            )
        prompt = UX_DESIGNER_PLAN_PROMPT.format(
            task_description=inp.task_description or "N/A",
            spec_content=(inp.task_description or "")[:6000],
        )
        try:
            raw = self.llm.complete_text(prompt)
        except Exception as e:
            logger.warning("UX plan LLM call failed: %s", e)
            return ToolAgentPhaseOutput(
                recommendations=["Consider user flows and interactions."],
                summary="UX planning failed (LLM error).",
            )
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
        recommendations: List[str] = []
        if data.get("user_journeys"):
            recommendations.append(f"User Journeys: {data['user_journeys'][:500]}")
        if data.get("wireframes_summary"):
            recommendations.append(f"Wireframes: {data['wireframes_summary'][:500]}")
        if data.get("interaction_rules"):
            recommendations.append(f"Interaction Rules: {data['interaction_rules'][:500]}")
        if data.get("microcopy_guidelines"):
            recommendations.append(f"Microcopy: {data['microcopy_guidelines'][:500]}")
        return ToolAgentPhaseOutput(
            recommendations=recommendations if recommendations else ["Consider user flows and interactions."],
            summary=data.get("summary", "UX planning complete."),
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Find UX/usability issues in current code. Returns issues with source=ux."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="UX review skipped (no LLM).")
        code_text = "\n\n".join(
            f"--- {p} ---\n{c}" for p, c in list(inp.current_files.items())[:20]
        )[:MAX_UX_CODE_CHARS]
        if not code_text.strip():
            return ToolAgentPhaseOutput(summary="UX review skipped (no code).")
        prompt = UX_ENGINEER_REVIEW_PROMPT.format(
            task_description=inp.task_description or "N/A",
            code=code_text,
        )
        try:
            raw = self.llm.complete_text(prompt)
        except Exception as e:
            logger.warning("UX review LLM call failed: %s", e)
            return ToolAgentPhaseOutput(summary="UX review failed (LLM error).")
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
                        source="ux",
                        severity=item.get("severity", "medium"),
                        description=item.get("description", ""),
                        file_path=item.get("file_path", ""),
                        recommendation=item.get("recommendation", ""),
                    )
                )
        return ToolAgentPhaseOutput(
            issues=issues,
            summary=f"UX review: {len(issues)} issue(s) found.",
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix UX-owned issues one at a time."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="UX problem_solve skipped (no LLM).")
        ux_issues = [
            i
            for i in inp.review_issues
            if (i.source or "").strip() in ("ux", "ux_usability", "tool_ux_usability")
        ]
        if not ux_issues:
            return ToolAgentPhaseOutput(summary="No UX issues to fix.")
        merged = dict(inp.current_files)
        fixed_count = 0
        for issue in ux_issues:
            relevant_code = _relevant_code_for_issue(issue, merged)
            prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT.format(
                source=issue.source or "ux",
                severity=issue.severity or "medium",
                description=issue.description or "",
                file_path=issue.file_path or "N/A",
                recommendation=issue.recommendation or "Fix the UX issue.",
                current_code=relevant_code,
            )
            try:
                raw = self.llm.complete_text(prompt)
            except Exception as e:
                logger.warning(
                    "UX fix for issue %s failed: %s",
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
            summary=f"UX: fixed {fixed_count} of {len(ux_issues)} issue(s) (one at a time).",
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="UX deliver.")
