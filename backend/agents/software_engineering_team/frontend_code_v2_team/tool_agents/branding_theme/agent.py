"""Branding/Theme tool agent for frontend-code-v2: design system, tokens, component library planning."""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.shared.prompt_utils import JSON_OUTPUT_INSTRUCTION

from ...models import ToolAgentPhaseInput
from .._plan_base import PlanGeneratorToolAgent

DESIGN_SYSTEM_PLAN_PROMPT = (
    """You are an expert Design System & UI Engineering Agent. Your job is to translate design into a reusable component library plan. You prevent copy-pasted UI entropy.

**Your expertise:**
- Component library planning (shared vs app-specific components)
- Token implementation (CSS variables, theming, dark mode)
- Accessibility baked into components (focus, keyboard, ARIA patterns)
- Storybook-style documentation (even if not using Storybook)

**Input:**
- Task description and requirements
- Optional: UI output (component specs, design tokens, motion)
- Optional: spec content, architecture

**Your task:**
Produce design system artifacts that the Feature Implementation agent will use:

1. **Component Library Plan** – What is shared vs app-specific? Which components should be reusable (buttons, inputs, cards, modals)? Naming conventions. Structure of the component library.
2. **Token Implementation Plan** – How to implement design tokens: CSS variables (e.g. --color-primary, --spacing-md), theming approach, dark mode strategy. Framework-specific theming if applicable (e.g. Material UI for React, Angular Material, Vuetify).
3. **A11y in Components** – Accessibility baked into each component type: focus management, keyboard navigation, ARIA patterns (aria-label, aria-expanded, aria-controls), screen reader considerations.
4. **Documentation Plan** – Storybook-style documentation: what each component documents (props, variants, usage examples). Even without Storybook, define what would be documented.

**Output format:**
Return a single JSON object with:
- "component_library_plan": string (shared vs app-specific, structure, naming)
- "token_implementation_plan": string (CSS vars, theming, dark mode)
- "a11y_in_components": string (focus, keyboard, ARIA per component type)
- "documentation_plan": string (Storybook-style docs plan)
- "summary": string (2-3 sentence summary of design system decisions)
"""
    + JSON_OUTPUT_INSTRUCTION
    + """

---

**Task:** {task_description}

**Spec (excerpt):**
{spec_content}
"""
)


class BrandingThemeToolAgent(PlanGeneratorToolAgent):
    """Branding/Theme tool agent: design system, tokens, component library planning."""

    log_label = "Branding/Theme"
    execute_summary = "Branding/Theme execute — no changes applied."
    review_summary = "Branding/Theme review stub."
    problem_solve_summary = "Branding/Theme problem-solving stub."
    deliver_summary = "Branding/Theme deliver."

    no_model_recommendations = [
        "Consider design tokens and theme compliance.",
        "Plan component library structure: shared vs app-specific.",
        "Bake accessibility into component patterns.",
    ]
    no_model_summary = "Branding/Theme planning stub (no LLM)."
    llm_error_recommendations = ["Consider design tokens and theme compliance."]
    llm_error_summary = "Branding/Theme planning failed (LLM error)."
    empty_recommendations = ["Consider design tokens and theme compliance."]
    default_summary = "Branding/Theme planning complete."
    field_labels = (
        ("component_library_plan", "Component Library"),
        ("token_implementation_plan", "Token Implementation"),
        ("a11y_in_components", "A11y in Components"),
        ("documentation_plan", "Documentation"),
    )

    def _build_plan_prompt(self, inp: ToolAgentPhaseInput) -> str:
        return DESIGN_SYSTEM_PLAN_PROMPT.format(
            task_description=inp.task_description or "N/A",
            spec_content=(inp.task_description or ""),
        )
