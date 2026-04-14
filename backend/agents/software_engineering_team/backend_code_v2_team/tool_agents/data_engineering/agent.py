"""
Data Engineering tool agent: schema design, data models, data integrity.

Implemented from scratch inside the backend-code-v2 team.
Uses template-based output (not JSON) so parsing works across model providers.

NOTE: This agent does NOT produce migration scripts by default. Migrations are
only generated when explicitly requested for modifying existing database schemas.
For greenfield projects, models/schemas are created directly without migration
infrastructure.
"""

from __future__ import annotations

import logging

from strands import Agent

from llm_service import get_strands_model

from ...models import (
    ToolAgentInput,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)
from ...output_templates import parse_files_and_summary_template
from ...prompts import FILES_OUTPUT_TEMPLATE_INSTRUCTIONS

logger = logging.getLogger(__name__)

DATA_ENGINEERING_PROMPT = (
    """You are an expert Data Engineering specialist.

Given a microtask about database schema, data models, or data integrity,
produce the required files (models, seed data, etc.).

**IMPORTANT:** Do NOT generate database migration files (Alembic versions, Flyway scripts, etc.)
unless the microtask EXPLICITLY requests migrations for modifying an existing schema.
For new/greenfield projects, create models and schemas directly without migration infrastructure.

**Microtask:** {description}
**Language:** {language}
**Existing code context:** {existing_code}
"""
    + FILES_OUTPUT_TEMPLATE_INSTRUCTIONS
)


class DataEngineeringToolAgent:
    """Produces schema definitions, data models, and data integrity checks."""

    def __init__(self, llm=None) -> None:
        from strands.models.model import Model as _StrandsModel

        self._model = llm if (llm is not None and isinstance(llm, _StrandsModel)) else get_strands_model()

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        prompt = DATA_ENGINEERING_PROMPT.format(
            description=inp.microtask.description or inp.microtask.title,
            language=inp.language,
            existing_code=inp.existing_code[:4000] if inp.existing_code else "(none)",
        )
        logger.info("DataEngineering: running for microtask %s", inp.microtask.id)
        raw = (lambda _r: str(_r))(Agent(model=self._model)(prompt)).strip()
        data = parse_files_and_summary_template(raw)
        return ToolAgentOutput(
            files=data.get("files") or {},
            recommendations=[],
            summary=data.get("summary", ""),
        )

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Recommend how data/schema work should be reflected in the microtask plan."""
        return ToolAgentPhaseOutput(
            recommendations=[
                "Consider data models and integrity checks. Only add migrations if modifying existing schema."
            ],
            summary="Data engineering planning input provided.",
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Domain-specific review: schema consistency, model integrity."""
        return ToolAgentPhaseOutput(
            recommendations=["Verify schema definitions and model consistency."],
            summary="Data engineering review completed.",
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Suggest data-layer fixes for issues found in review."""
        return ToolAgentPhaseOutput(
            recommendations=["Check schema constraints and model relationships."],
            summary="Data engineering problem-solving input provided.",
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Final data-domain actions before merge."""
        return ToolAgentPhaseOutput(summary="Data engineering deliver phase completed.")
