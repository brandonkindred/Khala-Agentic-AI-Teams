"""UI Design tool agent for frontend-code-v2: visual system, layout, typography, component specs."""

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
    from software_engineering_team.shared.llm import LLMClient

logger = logging.getLogger(__name__)

UI_DESIGNER_PLAN_PROMPT = """You are a UI / Visual Designer Agent. Your job is to define the visual system, layout, typography, color, spacing, component states. You ensure it looks like the design, not "close enough, ship it."

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

Respond with valid JSON only. No explanatory text outside JSON.

---

**Task:** {task_description}

**Spec (excerpt):**
{spec_content}
"""


class UiDesignToolAgent:
    """UI Design tool agent: visual system, layout, typography, component specs."""

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("UI Design: microtask %s (execute stub)", inp.microtask.id)
        return ToolAgentOutput(summary="UI Design execute — no changes applied.")

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Generate UI design artifacts: component specs, design tokens, motion guidelines."""
        if not self.llm:
            return ToolAgentPhaseOutput(
                recommendations=[
                    "Consider layout and component structure.",
                    "Define design tokens: colors, typography, spacing.",
                    "Establish motion guidelines for transitions and feedback.",
                ],
                summary="UI Design planning stub (no LLM).",
            )
        prompt = UI_DESIGNER_PLAN_PROMPT.format(
            task_description=inp.task_description or "N/A",
            spec_content=(inp.task_description or "")[:5000],
        )
        try:
            raw = self.llm.complete_text(prompt)
        except Exception as e:
            logger.warning("UI Design plan LLM call failed: %s", e)
            return ToolAgentPhaseOutput(
                recommendations=["Consider layout and component structure."],
                summary="UI Design planning failed (LLM error).",
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
        if data.get("component_specs"):
            recommendations.append(f"Component Specs: {data['component_specs'][:500]}")
        if data.get("design_tokens"):
            recommendations.append(f"Design Tokens: {data['design_tokens'][:500]}")
        if data.get("motion_guidelines"):
            recommendations.append(f"Motion Guidelines: {data['motion_guidelines'][:500]}")
        if data.get("high_fidelity_summary"):
            recommendations.append(f"Layout: {data['high_fidelity_summary'][:500]}")
        return ToolAgentPhaseOutput(
            recommendations=recommendations if recommendations else ["Consider layout and component structure."],
            summary=data.get("summary", "UI Design planning complete."),
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="UI Design review stub.")

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="UI Design problem-solving stub.")

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="UI Design deliver.")
