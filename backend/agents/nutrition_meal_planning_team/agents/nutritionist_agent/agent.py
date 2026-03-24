"""Nutritionist agent: expert-style daily targets and balance guidelines from client profile."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from llm_service import LLMClient, LLMError, LLMJsonParseError

from ...models import ClientProfile, DailyTargets, NutritionPlan

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert nutritionist. Given a client profile (household, dietary needs, allergies, lifestyle, goals), produce a structured nutrition plan as JSON.

Output JSON with:
- daily_targets: object with optional numbers: calories_kcal, protein_g, carbs_g, fat_g, fiber_g, sodium_mg
- balance_guidelines: array of strings (e.g. "more vegetables", "limit added sugar")
- foods_to_emphasize: array of strings
- foods_to_avoid: array of strings (respect allergies and dietary needs)
- notes: string with any extra guidance

Do NOT recommend specific recipes or meals. Only targets and guidelines. Use evidence-based ranges (e.g. 0.8g protein per kg if not specified). Output only valid JSON."""


class NutritionistAgent:
    """Produces NutritionPlan from ClientProfile using LLM."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def run(self, profile: ClientProfile) -> NutritionPlan:
        """Generate nutrition plan from client profile."""
        prompt = (
            "Client profile:\n"
            + profile.model_dump_json(indent=2)
            + "\n\nProduce the nutrition plan JSON (daily_targets, balance_guidelines, foods_to_emphasize, foods_to_avoid, notes)."
        )
        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.3,
                system_prompt=SYSTEM_PROMPT,
                expected_keys=[
                    "daily_targets",
                    "balance_guidelines",
                    "foods_to_emphasize",
                    "foods_to_avoid",
                ],
            )
        except (LLMJsonParseError, LLMError) as e:
            logger.warning("Nutritionist LLM call failed, returning empty plan: %s", e)
            return NutritionPlan(generated_at=datetime.now(timezone.utc).isoformat())

        if "daily_targets" in data and isinstance(data["daily_targets"], dict):
            data["daily_targets"] = DailyTargets.model_validate(data["daily_targets"])
        data["generated_at"] = datetime.now(timezone.utc).isoformat()
        return NutritionPlan.model_validate(data)
