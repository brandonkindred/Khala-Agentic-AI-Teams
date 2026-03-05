"""Design System & UI Engineering agent: component library, tokens, a11y, docs."""

from __future__ import annotations

import logging
from typing import Optional

from software_engineering_team.shared.llm import LLMClient
from software_engineering_team.shared.models import SystemArchitecture

from frontend_team_deprecated.models import DesignSystemOutput, UIDesignerOutput
from .models import DesignSystemInput
from .prompts import DESIGN_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def _format_ui_output(ui: Optional[UIDesignerOutput]) -> str:
    """Format UI output for context."""
    if not ui:
        return ""
    parts = []
    if ui.component_specs:
        parts.append(f"Component Specs:\n{ui.component_specs}")
    if ui.design_tokens:
        parts.append(f"Design Tokens:\n{ui.design_tokens}")
    if ui.motion_guidelines:
        parts.append(f"Motion Guidelines:\n{ui.motion_guidelines}")
    if ui.high_fidelity_summary:
        parts.append(f"High-Fidelity Summary:\n{ui.high_fidelity_summary}")
    return "\n\n".join(parts) if parts else ""


class DesignSystemAgent:
    """Agent that owns translating design into reusable component library."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: DesignSystemInput) -> DesignSystemOutput:
        """Produce design system artifacts: component library plan, tokens, a11y, docs."""
        logger.info("Design System: starting for task %s", input_data.task_id or "unknown")
        context_parts = [
            f"**Task Description:**\n{input_data.task_description}",
        ]
        if input_data.user_story:
            context_parts.append(f"**User Story:** {input_data.user_story}")
        ui_context = _format_ui_output(input_data.ui_output)
        if ui_context:
            context_parts.append(f"**UI Design Context:**\n{ui_context}")
        if input_data.spec_content:
            context_parts.append(f"**Spec (excerpt):**\n{input_data.spec_content[:5000]}")
        if input_data.architecture:
            context_parts.append(f"**Architecture:**\n{input_data.architecture.overview}")

        prompt = DESIGN_SYSTEM_PROMPT + "\n\n---\n\n" + "\n\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)

        return DesignSystemOutput(
            component_library_plan=data.get("component_library_plan", "") or "",
            token_implementation_plan=data.get("token_implementation_plan", "") or "",
            a11y_in_components=data.get("a11y_in_components", "") or "",
            documentation_plan=data.get("documentation_plan", "") or "",
            summary=data.get("summary", "") or "",
        )
