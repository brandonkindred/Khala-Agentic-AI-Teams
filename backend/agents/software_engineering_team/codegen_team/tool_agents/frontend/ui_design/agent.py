"""UI Design tool agent for frontend-code-v2: visual system, layout, typography, component specs."""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.codegen_team.models import ToolAgentPhaseInput
from software_engineering_team.shared.prompt_utils import JSON_OUTPUT_INSTRUCTION

from .._plan_base import PlanGeneratorToolAgent

UI_DESIGNER_PLAN_PROMPT = (
    """You are a UI / Visual Designer Agent. Your job is to define the visual system, layout, typography, color, spacing, component states. You ensure it looks like the design, not "close enough, ship it."

**Your expertise:**
- High-fidelity screens (describe in text; structure and layout)
- Component specs (states, variants, responsive rules)
- Design tokens (colors, typography scale, spacing scale)
- Motion guidelines (when and how animation is used)

**Input:**
- Task description and requirements
- Optional: UX output (user journeys, interaction rules, microcopy)
- Optional: spec content, architecture

**Your task:**
Produce UI design artifacts that the Design System and Feature Implementation agents will use:

1. **Component Specs** – For each component or screen, specify: states (default, hover, focus, disabled, error), variants (primary/secondary buttons, etc.), responsive rules (breakpoints, behavior on mobile/tablet/desktop).
2. **Design Tokens** – Define: color palette (primary, secondary, error, success, background, surface, text), typography scale (headings, body, captions, font families), spacing scale (4px base, 8, 12, 16, 24, 32, 48).
3. **Motion Guidelines** – When to use animation (transitions, loading, feedback), duration (e.g. 200ms for micro-interactions, 300ms for transitions), easing. Restraint: "delight" without being annoying.
4. **High-Fidelity Summary** – Describe the visual layout: key screens, hierarchy, key UI elements, alignment and grid.

**Output format:**
Return a single JSON object with:
- "component_specs": string (component states, variants, responsive rules)
- "design_tokens": string (colors, typography, spacing)
- "motion_guidelines": string (when and how animation is used)
- "high_fidelity_summary": string (visual layout and key screens)
- "summary": string (2-3 sentence summary of key UI decisions)
"""
    + JSON_OUTPUT_INSTRUCTION
    + """

---

**Task:** {task_description}

**Spec (excerpt):**
{spec_content}
"""
)


class UiDesignToolAgent(PlanGeneratorToolAgent):
    """UI Design tool agent: visual system, layout, typography, component specs."""

    log_label = "UI Design"
    execute_summary = "UI Design execute — no changes applied."
    review_summary = "UI Design review stub."
    problem_solve_summary = "UI Design problem-solving stub."
    deliver_summary = "UI Design deliver."

    no_model_recommendations = [
        "Consider layout and component structure.",
        "Define design tokens: colors, typography, spacing.",
        "Establish motion guidelines for transitions and feedback.",
    ]
    no_model_summary = "UI Design planning stub (no LLM)."
    llm_error_recommendations = ["Consider layout and component structure."]
    llm_error_summary = "UI Design planning failed (LLM error)."
    empty_recommendations = ["Consider layout and component structure."]
    default_summary = "UI Design planning complete."
    field_labels = (
        ("component_specs", "Component Specs"),
        ("design_tokens", "Design Tokens"),
        ("motion_guidelines", "Motion Guidelines"),
        ("high_fidelity_summary", "Layout"),
    )

    def _build_plan_prompt(self, inp: ToolAgentPhaseInput) -> str:
        return UI_DESIGNER_PLAN_PROMPT.format(
            task_description=inp.task_description or "N/A",
            spec_content=inp.task_description or "",
        )
