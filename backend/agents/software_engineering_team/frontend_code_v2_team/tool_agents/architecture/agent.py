"""Architecture tool agent for frontend-code-v2: generates architecture artifacts in plan phase."""

from __future__ import annotations

import json
import logging

from strands import Agent

from llm_service import get_strands_model

from ...models import (
    ToolAgentInput,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)

logger = logging.getLogger(__name__)

MAX_SPEC_CHARS = 6_000

FRONTEND_ARCHITECT_PROMPT = """You are an expert Frontend Architect Agent. Your job is to define app architecture and long-term maintainability. You stop the codebase from turning into a spaghetti museum.

**Your expertise:**
- Folder/module structure and conventions
- Routing strategy
- State management strategy (server state vs UI state)
- Error handling strategy and global boundary patterns
- API client patterns and typing strategy

**Input:**
- Task description and requirements
- Optional: spec content, architecture
- Optional: UX, UI, Design System artifacts from prior agents

**Your task:**
Produce architecture artifacts that the Feature Implementation agent will use:

1. **Folder Structure** – Directory layout: src structure, where components go, where services/hooks go, shared vs feature-specific. Naming conventions. Framework-native project structure (React hooks/components, Angular standalone, Vue composition API).
2. **Routing Strategy** – Route structure, lazy-loaded routes, guards, route params. How navigation works.
3. **State Management** – Server state (API data, caching) vs UI state (form state, modals, filters). When to use services, signals, or state management libraries. Data flow.
4. **Error Handling** – Global error boundary, HTTP interceptor for errors, how to surface errors to users. Retry strategies.
5. **API Client Patterns** – How to call APIs: HTTP client usage, typing (interfaces for request/response), error handling, loading states. Base URL, interceptors.

**Output format:**
Return a single JSON object with:
- "folder_structure": string (directory layout, conventions)
- "routing_strategy": string (routes, lazy loading, guards)
- "state_management": string (server vs UI state, data flow)
- "error_handling": string (error boundaries, interceptors, user-facing errors)
- "api_client_patterns": string (HTTP client, typing, error handling)
- "summary": string (2-3 sentence summary of architecture decisions)

Respond with valid JSON only. No explanatory text outside JSON.

---

**Task:** {task_description}

**Spec (excerpt):**
{spec_content}
"""


class ArchitectureToolAgent:
    """Architecture tool agent: generates architecture artifacts in plan phase."""

    def __init__(self, llm=None) -> None:
        from strands.models.model import Model as _StrandsModel

        self._model = llm if (llm is not None and isinstance(llm, _StrandsModel)) else get_strands_model()
        self.llm = llm  # kept for backward compat checks

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("Architecture: microtask %s (execute stub)", inp.microtask.id)
        return ToolAgentOutput(summary="Architecture execute — no changes applied.")

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Generate architecture artifacts: folder structure, routing, state management, error handling, API patterns."""
        if not self._model:
            return ToolAgentPhaseOutput(
                recommendations=[
                    "Define folder structure with feature-based organization.",
                    "Use lazy-loaded routes for code splitting.",
                    "Separate server state (API data) from UI state (forms, modals).",
                    "Implement global error boundary and HTTP interceptor.",
                    "Create typed API client with loading/error states.",
                ],
                summary="Architecture planning (no LLM).",
            )
        spec_excerpt = (inp.spec_context or "")[:MAX_SPEC_CHARS]
        task_desc = inp.task_description or inp.task_title or "Frontend application"
        prompt = FRONTEND_ARCHITECT_PROMPT.format(
            task_description=task_desc,
            spec_content=spec_excerpt if spec_excerpt.strip() else "(no spec provided)",
        )
        try:
            raw = (lambda _r: str(_r))(Agent(model=self._model)(prompt)).strip()
        except Exception as e:
            logger.warning("Architecture plan LLM call failed: %s", e)
            return ToolAgentPhaseOutput(
                recommendations=["Architecture planning failed (LLM error)."],
                summary="Architecture planning failed.",
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            import re

            match = re.search(r"\{[\s\S]*\}", raw)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    data = {}
            else:
                data = {}
        recommendations = []
        if data.get("folder_structure"):
            recommendations.append(f"Folder structure: {data['folder_structure'][:500]}")
        if data.get("routing_strategy"):
            recommendations.append(f"Routing: {data['routing_strategy'][:500]}")
        if data.get("state_management"):
            recommendations.append(f"State management: {data['state_management'][:500]}")
        if data.get("error_handling"):
            recommendations.append(f"Error handling: {data['error_handling'][:500]}")
        if data.get("api_client_patterns"):
            recommendations.append(f"API patterns: {data['api_client_patterns'][:500]}")
        summary = data.get("summary", "Architecture artifacts generated.")
        return ToolAgentPhaseOutput(
            recommendations=recommendations
            if recommendations
            else ["Architecture artifacts generated."],
            summary=summary[:500] if summary else "Architecture planning complete.",
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Architecture review (no issues to report).")

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Architecture problem-solving (no fixes needed).")

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Architecture deliver.")
