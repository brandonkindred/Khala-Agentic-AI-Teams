"""UI / Visual Designer agent: high-fidelity screens, component specs, design tokens, motion."""

from __future__ import annotations

import logging
from typing import Optional

from frontend_team_deprecated.models import UIDesignerOutput, UXDesignerOutput

from llm_service import LLMClient

from .models import UIDesignerInput
from .prompts import UI_DESIGNER_PROMPT

logger = logging.getLogger(__name__)


def _format_ux_output(ux: Optional[UXDesignerOutput]) -> str:
    """Format UX output for context."""
    if not ux:
        return ""
    parts = []
    if ux.user_journeys:
        parts.append(f"User Journeys:\n{ux.user_journeys}")
    if ux.wireframes_summary:
        parts.append(f"Wireframes:\n{ux.wireframes_summary}")
    if ux.interaction_rules:
        parts.append(f"Interaction Rules:\n{ux.interaction_rules}")
    if ux.microcopy_guidelines:
        parts.append(f"Microcopy Guidelines:\n{ux.microcopy_guidelines}")
    return "\n\n".join(parts) if parts else ""


class UIDesignerAgent:
    """Agent that owns visual system, layout, typography, color, spacing, component states."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: UIDesignerInput) -> UIDesignerOutput:
        """Produce UI design artifacts: component specs, design tokens, motion guidelines."""
        logger.info("UI Designer: starting for task %s", input_data.task_id or "unknown")
        context_parts = [
            f"**Task Description:**\n{input_data.task_description}",
        ]
        if input_data.user_story:
            context_parts.append(f"**User Story:** {input_data.user_story}")
        ux_context = _format_ux_output(input_data.ux_output)
        if ux_context:
            context_parts.append(f"**UX Design Context:**\n{ux_context}")
        if input_data.spec_content:
            context_parts.append(f"**Spec (excerpt):**\n{input_data.spec_content[:5000]}")
        if input_data.architecture:
            context_parts.append(f"**Architecture:**\n{input_data.architecture.overview}")

        prompt = UI_DESIGNER_PROMPT + "\n\n---\n\n" + "\n\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2, think=True)

        return UIDesignerOutput(
            component_specs=data.get("component_specs", "") or "",
            design_tokens=data.get("design_tokens", "") or "",
            motion_guidelines=data.get("motion_guidelines", "") or "",
            high_fidelity_summary=data.get("high_fidelity_summary", "") or "",
            summary=data.get("summary", "") or "",
        )
