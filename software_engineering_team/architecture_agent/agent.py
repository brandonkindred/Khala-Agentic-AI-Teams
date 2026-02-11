"""Architecture Expert agent: designs system architecture from requirements."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from shared.llm import LLMClient
from shared.models import ArchitectureComponent, SystemArchitecture

from .models import ArchitectureInput, ArchitectureOutput
from .prompts import ARCHITECTURE_PROMPT

logger = logging.getLogger(__name__)


class ArchitectureExpertAgent:
    """
    Staff-level Software Architecture Expert. Uses product requirements to design
    a system architecture that DevOps, Security, Backend, Frontend, and QA agents
    reference when implementing or validating changes.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: ArchitectureInput) -> ArchitectureOutput:
        """Design system architecture from requirements."""
        reqs = input_data.requirements
        context_parts = [
            f"**Product Title:** {reqs.title}",
            f"**Description:** {reqs.description}",
            "**Acceptance Criteria:**",
            *[f"- {c}" for c in reqs.acceptance_criteria],
            "**Constraints:**",
            *[f"- {c}" for c in reqs.constraints],
            f"**Priority:** {reqs.priority}",
        ]
        if input_data.existing_architecture:
            context_parts.extend(["", "**Existing Architecture to extend:**", input_data.existing_architecture])
        if input_data.technology_preferences:
            context_parts.extend(["", "**Technology Preferences:**", ", ".join(input_data.technology_preferences)])

        prompt = ARCHITECTURE_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)

        data = self.llm.complete_json(prompt, temperature=0.2)

        components = []
        for c in data.get("components") or []:
            if isinstance(c, dict) and c.get("name"):
                components.append(
                    ArchitectureComponent(
                        name=c["name"],
                        type=c.get("type", "unknown"),
                        description=c.get("description", ""),
                        technology=c.get("technology"),
                        dependencies=c.get("dependencies", []),
                        interfaces=c.get("interfaces", []),
                    )
                )

        architecture = SystemArchitecture(
            overview=data.get("overview", ""),
            components=components,
            architecture_document=data.get("architecture_document", ""),
            diagrams=data.get("diagrams", {}),
            decisions=data.get("decisions", []),
        )

        return ArchitectureOutput(
            architecture=architecture,
            summary=data.get("summary", ""),
        )
