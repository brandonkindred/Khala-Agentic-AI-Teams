"""Architecture tool agent for frontend-code-v2: generates architecture artifacts in plan phase."""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.codegen_team.models import ToolAgentPhaseInput
from software_engineering_team.shared.prompt_utils import JSON_OUTPUT_INSTRUCTION

from .._plan_base import PlanGeneratorToolAgent

# Cap on the spec content inlined into the single architecture-planning prompt.
# This is a one-shot LLM call with no tool the model can call to read the rest
# of an oversized spec, so an unbounded spec risks a context-length failure
# that degrades the whole call to the generic "planning failed" fallback.
MAX_SPEC_CHARS = 6_000

FRONTEND_ARCHITECT_PROMPT = (
    """You are an expert Frontend Architect Agent. Your job is to define app architecture and long-term maintainability. You stop the codebase from turning into a spaghetti museum.

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
"""
    + JSON_OUTPUT_INSTRUCTION
    + """

---

**Task:** {task_description}

**Spec (excerpt):**
{spec_content}
"""
)


class ArchitectureToolAgent(PlanGeneratorToolAgent):
    """Architecture tool agent: generates architecture artifacts in plan phase."""

    log_label = "Architecture"
    execute_summary = "Architecture execute — no changes applied."
    review_summary = "Architecture review (no issues to report)."
    problem_solve_summary = "Architecture problem-solving (no fixes needed)."
    deliver_summary = "Architecture deliver."

    no_model_recommendations = [
        "Define folder structure with feature-based organization.",
        "Use lazy-loaded routes for code splitting.",
        "Separate server state (API data) from UI state (forms, modals).",
        "Implement global error boundary and HTTP interceptor.",
        "Create typed API client with loading/error states.",
    ]
    no_model_summary = "Architecture planning (no LLM)."
    llm_error_recommendations = ["Architecture planning failed (LLM error)."]
    llm_error_summary = "Architecture planning failed."
    empty_recommendations = ["Architecture artifacts generated."]
    default_summary = "Architecture artifacts generated."
    empty_summary_override = "Architecture planning complete."
    field_labels = (
        ("folder_structure", "Folder structure"),
        ("routing_strategy", "Routing"),
        ("state_management", "State management"),
        ("error_handling", "Error handling"),
        ("api_client_patterns", "API patterns"),
    )

    def _build_plan_prompt(self, inp: ToolAgentPhaseInput) -> str:
        """Build the architecture-planning LLM prompt from task and spec context.

        Preconditions:
            ``inp`` is a :class:`ToolAgentPhaseInput`; all fields are optional.

        Postconditions:
            Returns ``FRONTEND_ARCHITECT_PROMPT`` formatted with the task description
            (falling back to ``task_title``, then a default) and the spec content,
            capped at ``MAX_SPEC_CHARS`` (falling back to a placeholder when blank).
        """
        spec_text = (inp.spec_context or "")[:MAX_SPEC_CHARS]
        task_desc = inp.task_description or inp.task_title or "Frontend application"
        return FRONTEND_ARCHITECT_PROMPT.format(
            task_description=task_desc,
            spec_content=spec_text if spec_text.strip() else "(no spec provided)",
        )
