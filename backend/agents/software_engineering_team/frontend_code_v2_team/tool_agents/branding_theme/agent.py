"""Branding/Theme tool agent for frontend-code-v2: design system, tokens, component library planning."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, List, Optional

from ...models import (
    ToolAgentInput,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)

if TYPE_CHECKING:
    from llm_service import LLMClient

logger = logging.getLogger(__name__)

DESIGN_SYSTEM_PLAN_PROMPT = """You are an expert Design System & UI Engineering Agent. Your job is to translate design into a reusable component library plan. You prevent copy-pasted UI entropy.

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

Respond with valid JSON only. No explanatory text outside JSON.

---

**Task:** {task_description}

**Spec (excerpt):**
{spec_content}
"""


class BrandingThemeToolAgent:
    """Branding/Theme tool agent: design system, tokens, component library planning."""

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("Branding/Theme: microtask %s (execute stub)", inp.microtask.id)
        return ToolAgentOutput(summary="Branding/Theme execute — no changes applied.")

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Generate design system artifacts: component library, tokens, a11y, documentation."""
        if not self.llm:
            return ToolAgentPhaseOutput(
                recommendations=[
                    "Consider design tokens and theme compliance.",
                    "Plan component library structure: shared vs app-specific.",
                    "Bake accessibility into component patterns.",
                ],
                summary="Branding/Theme planning stub (no LLM).",
            )
        prompt = DESIGN_SYSTEM_PLAN_PROMPT.format(
            task_description=inp.task_description or "N/A",
            spec_content=(inp.task_description or "")[:5000],
        )
        try:
            raw = self.llm.complete_text(prompt, think=True)
        except Exception as e:
            logger.warning("Branding/Theme plan LLM call failed: %s", e)
            return ToolAgentPhaseOutput(
                recommendations=["Consider design tokens and theme compliance."],
                summary="Branding/Theme planning failed (LLM error).",
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
        if data.get("component_library_plan"):
            recommendations.append(f"Component Library: {data['component_library_plan'][:500]}")
        if data.get("token_implementation_plan"):
            recommendations.append(
                f"Token Implementation: {data['token_implementation_plan'][:500]}"
            )
        if data.get("a11y_in_components"):
            recommendations.append(f"A11y in Components: {data['a11y_in_components'][:500]}")
        if data.get("documentation_plan"):
            recommendations.append(f"Documentation: {data['documentation_plan'][:500]}")
        return ToolAgentPhaseOutput(
            recommendations=recommendations
            if recommendations
            else ["Consider design tokens and theme compliance."],
            summary=data.get("summary", "Branding/Theme planning complete."),
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Branding/Theme review stub.")

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Branding/Theme problem-solving stub.")

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Branding/Theme deliver.")
