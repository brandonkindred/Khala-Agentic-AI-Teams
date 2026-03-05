"""UX Designer agent: user flows, wireframes, interaction rules, microcopy."""

from __future__ import annotations

import logging
from typing import Optional

from software_engineering_team.shared.llm import LLMClient
from software_engineering_team.shared.models import SystemArchitecture

from frontend_team_deprecated.models import UXDesignerOutput
from .models import UXDesignerInput
from .prompts import UX_DESIGNER_PROMPT

logger = logging.getLogger(__name__)


class UXDesignerAgent:
    """Agent that owns user flows, information architecture, interaction design, microcopy, edge cases."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: UXDesignerInput) -> UXDesignerOutput:
        """Produce UX design artifacts: user journeys, wireframes, interaction rules, microcopy."""
        logger.info("UX Designer: starting for task %s", input_data.task_id or "unknown")
        context_parts = [
            f"**Task Description:**\n{input_data.task_description}",
        ]
        if input_data.user_story:
            context_parts.append(f"**User Story:** {input_data.user_story}")
        if input_data.spec_content:
            context_parts.append(f"**Spec (excerpt):**\n{input_data.spec_content[:6000]}")
        if input_data.architecture:
            context_parts.append(f"**Architecture:**\n{input_data.architecture.overview}")

        prompt = UX_DESIGNER_PROMPT + "\n\n---\n\n" + "\n\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)

        return UXDesignerOutput(
            user_journeys=data.get("user_journeys", "") or "",
            wireframes_summary=data.get("wireframes_summary", "") or "",
            interaction_rules=data.get("interaction_rules", "") or "",
            microcopy_guidelines=data.get("microcopy_guidelines", "") or "",
            summary=data.get("summary", "") or "",
        )
