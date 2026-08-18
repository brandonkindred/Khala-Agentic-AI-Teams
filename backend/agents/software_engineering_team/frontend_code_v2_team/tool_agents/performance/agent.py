"""Performance tool agent for frontend-code-v2: bundle size, code splitting, caching, runtime cost."""

from __future__ import annotations

from typing import Dict

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.shared.prompt_utils import JSON_OUTPUT_INSTRUCTION
from software_engineering_team.shared.tool_agent_base import (
    BaseReviewToolAgent,
    relevant_code_for_issue,
)

from ...models import ReviewIssue

MAX_RELEVANT_CODE_CHARS = 8_000

PERFORMANCE_REVIEW_PROMPT = (
    """You are a Performance Engineer Agent. Your job is to protect the app from shipping a 14 MB JavaScript novella. You own speed, responsiveness, bundle size, and runtime cost.

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


class PerformanceToolAgent(BaseReviewToolAgent):
    """Performance tool agent: bundle size, code splitting, caching, runtime cost review.

    Reports issues for the coding agent to fix; ``review`` runs in JSON mode
    (``_model_json``) so a prose response is biased toward returning JSON.
    """

    name = "Performance"
    empty_label = "performance issues"
    issue_source = "performance"
    review_prompt = PERFORMANCE_REVIEW_PROMPT
    max_relevant_code_chars = MAX_RELEVANT_CODE_CHARS
    review_parse_mode = "json"
    uses_json_model = True
    review_model_attr = "_model_json"
    plan_recommendations = [
        "Set performance budgets: main bundle < 250KB, route chunks < 100KB.",
        "Use lazy loading for routes and heavy components.",
        "Add trackBy to *ngFor directives to prevent rerender storms.",
        "Consider HTTP caching headers and service worker for PWA.",
    ]
    plan_summary = "Performance planning: bundle size, lazy loading, caching recommendations."
