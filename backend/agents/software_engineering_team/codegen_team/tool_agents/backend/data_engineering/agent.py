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

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.codegen_team.stacks.backend.profile import (
    parse_files_and_summary_template,
)
from software_engineering_team.codegen_team.stacks.backend.prompts import (
    FILES_OUTPUT_TEMPLATE_INSTRUCTIONS,
)

from ..static_agents import FileGeneratorToolAgent

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


class DataEngineeringToolAgent(FileGeneratorToolAgent):
    """Produces schema definitions, data models, and data integrity checks."""

    log_label = "DataEngineering"
    generation_prompt = DATA_ENGINEERING_PROMPT
    _parse_files_and_summary = staticmethod(parse_files_and_summary_template)

    plan_recommendations = [
        "Consider data models and integrity checks. Only add migrations if modifying existing schema."
    ]
    plan_summary = "Data engineering planning input provided."
    review_recommendations = ["Verify schema definitions and model consistency."]
    review_summary = "Data engineering review completed."
    problem_solve_recommendations = ["Check schema constraints and model relationships."]
    problem_solve_summary = "Data engineering problem-solving input provided."
    deliver_summary = "Data engineering deliver phase completed."
