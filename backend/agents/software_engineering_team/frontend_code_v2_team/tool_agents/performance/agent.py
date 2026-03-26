"""Performance tool agent for frontend-code-v2: bundle size, code splitting, caching, runtime cost."""

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
    from llm_service import LLMClient

logger = logging.getLogger(__name__)

MAX_PERFORMANCE_CODE_CHARS = 25_000
MAX_RELEVANT_CODE_CHARS = 8_000

PERFORMANCE_REVIEW_PROMPT = """You are a Performance Engineer Agent. Your job is to protect the app from shipping a 14 MB JavaScript novella. You own speed, responsiveness, bundle size, and runtime cost.

**Your expertise:**
- Performance budgets (bundle size, route chunk size, LCP/INP targets)
- Code splitting and lazy loading
- Caching strategy (HTTP caching, service worker if needed)
- Profiling and performance regression tests
- Framework-specific: lazy routes, code splitting (React.lazy, Vue async components, Angular standalone)

**Input:**
- Code to review
- Task description
- Optional: build output (npm run build, bundle analysis)

**Your task:**
Review the code for performance. Identify issues and produce recommendations:

1. **Performance Budgets** – Recommend or enforce: main bundle size limit, route-level chunk limits, LCP/INP targets. Flag if code suggests large bundles.
2. **Code Splitting** – Are routes lazy-loaded? Are heavy components dynamically imported? Recommend lazy loading where appropriate.
3. **Caching** – HTTP caching headers, service worker for PWA? Recommend caching strategy.
4. **Rerender Storms** – Flag obvious causes: missing keys in lists, unnecessary re-renders, missing memoization (React.memo, useMemo), large component trees.
5. **Issues** – For each problem, produce a code_review-style issue with severity, description, and suggestion.

**Output format:**
Return a single JSON object with:
- "issues": list of objects, each with:
  - "severity": string (critical, major, medium, minor)
  - "category": string (bundle, chunking, caching, rerender, etc.)
  - "file_path": string
  - "description": string
  - "recommendation": string (concrete fix for coding agent)
- "approved": boolean (true when no critical performance issues)
- "performance_budgets": string (recommended budgets)
- "code_splitting_plan": string (lazy load recommendations)
- "caching_strategy": string (caching recommendations)
- "summary": string

If no critical issues, return approved=true. Be practical – focus on issues that materially affect load time or runtime performance.

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


class PerformanceToolAgent:
    """Performance tool agent: bundle size, code splitting, caching, runtime cost review and fixes."""

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("Performance: microtask %s (execute stub)", inp.microtask.id)
        return ToolAgentOutput(summary="Performance execute — no changes applied.")

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=[
                "Set performance budgets: main bundle < 250KB, route chunks < 100KB.",
                "Use lazy loading for routes and heavy components.",
                "Add trackBy to *ngFor directives to prevent rerender storms.",
                "Consider HTTP caching headers and service worker for PWA.",
            ],
            summary="Performance planning: bundle size, lazy loading, caching recommendations.",
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Find performance issues in current code. Returns issues with source=performance."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="Performance review skipped (no LLM).")
        code_text = "\n\n".join(
            f"--- {p} ---\n{c}" for p, c in list(inp.current_files.items())[:20]
        )[:MAX_PERFORMANCE_CODE_CHARS]
        if not code_text.strip():
            return ToolAgentPhaseOutput(summary="Performance review skipped (no code).")
        prompt = PERFORMANCE_REVIEW_PROMPT.format(
            task_description=inp.task_description or "N/A",
            code=code_text,
        )
        try:
            raw = self.llm.complete_text(prompt, think=True)
        except Exception as e:
            logger.warning("Performance review LLM call failed: %s", e)
            return ToolAgentPhaseOutput(summary="Performance review failed (LLM error).")
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
                        source="performance",
                        severity=item.get("severity", "medium"),
                        description=item.get("description", ""),
                        file_path=item.get("file_path", ""),
                        recommendation=item.get("recommendation", ""),
                    )
                )
        return ToolAgentPhaseOutput(
            issues=issues,
            summary=f"Performance review: {len(issues)} issue(s) found.",
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix performance-owned issues one at a time."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="Performance problem_solve skipped (no LLM).")
        perf_issues = [
            i
            for i in inp.review_issues
            if (i.source or "").strip() in ("performance", "tool_performance")
        ]
        if not perf_issues:
            return ToolAgentPhaseOutput(summary="No performance issues to fix.")
        merged = dict(inp.current_files)
        fixed_count = 0
        for issue in perf_issues:
            relevant_code = _relevant_code_for_issue(issue, merged)
            prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT.format(
                source=issue.source or "performance",
                severity=issue.severity or "medium",
                description=issue.description or "",
                file_path=issue.file_path or "N/A",
                recommendation=issue.recommendation or "Fix the performance issue.",
                current_code=relevant_code,
            )
            try:
                raw = self.llm.complete_text(prompt, think=True)
            except Exception as e:
                logger.warning(
                    "Performance fix for issue %s failed: %s",
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
            summary=f"Performance: fixed {fixed_count} of {len(perf_issues)} issue(s) (one at a time).",
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Performance deliver.")
