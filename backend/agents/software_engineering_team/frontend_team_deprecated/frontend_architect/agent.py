"""Frontend Architect agent: folder structure, routing, state, error handling, API patterns."""

from __future__ import annotations

import logging
from typing import Optional

from frontend_team_deprecated.models import (
    DesignSystemOutput,
    FrontendArchitectOutput,
    UIDesignerOutput,
    UXDesignerOutput,
)

from llm_service import LLMClient

from .models import FrontendArchitectInput
from .prompts import FRONTEND_ARCHITECT_PROMPT

logger = logging.getLogger(__name__)


def _format_design_context(
    ux: Optional[UXDesignerOutput],
    ui: Optional[UIDesignerOutput],
    ds: Optional[DesignSystemOutput],
) -> str:
    """Format design artifacts for context."""
    parts = []
    if ux and ux.summary:
        parts.append(f"UX: {ux.summary}")
    if ui and ui.summary:
        parts.append(f"UI: {ui.summary}")
    if ds and ds.summary:
        parts.append(f"Design System: {ds.summary}")
    return "\n".join(parts) if parts else ""


class FrontendArchitectAgent:
    """Agent that owns app architecture and long-term maintainability."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: FrontendArchitectInput) -> FrontendArchitectOutput:
        """Produce architecture artifacts: folder structure, routing, state, error handling, API patterns."""
        logger.info("Frontend Architect: starting for task %s", input_data.task_id or "unknown")
        context_parts = [
            f"**Task Description:**\n{input_data.task_description}",
        ]
        if input_data.user_story:
            context_parts.append(f"**User Story:** {input_data.user_story}")
        design_ctx = _format_design_context(
            input_data.ux_output,
            input_data.ui_output,
            input_data.design_system_output,
        )
        if design_ctx:
            context_parts.append(f"**Design Context:**\n{design_ctx}")
        if input_data.spec_content:
            context_parts.append(f"**Spec (excerpt):**\n{input_data.spec_content[:6000]}")
        if input_data.architecture:
            context_parts.append(f"**Architecture:**\n{input_data.architecture.overview}")
            if input_data.architecture.components:
                context_parts.append(
                    "**Components:**\n"
                    + "\n".join(
                        f"- {c.name} ({c.type}): {c.description}"
                        for c in input_data.architecture.components
                        if c.type in ("frontend", "ui", "client")
                    )
                )

        prompt = FRONTEND_ARCHITECT_PROMPT + "\n\n---\n\n" + "\n\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2, think=True)

        return FrontendArchitectOutput(
            folder_structure=data.get("folder_structure", "") or "",
            routing_strategy=data.get("routing_strategy", "") or "",
            state_management=data.get("state_management", "") or "",
            error_handling=data.get("error_handling", "") or "",
            api_client_patterns=data.get("api_client_patterns", "") or "",
            summary=data.get("summary", "") or "",
        )
