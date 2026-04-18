"""Nutritionist agent (SPEC-004).

After ADR-001 / SPEC-004 this agent is a **narrator**. Numbers come
from ``nutrition_calc.compute_daily_targets``; the LLM only authors
narrative (guidelines, foods to emphasize/avoid, summary). Two methods:

- ``narrate_plan(profile, targets, rationale)`` — produces a
  ``NarrativePayload`` alongside already-computed numeric targets.
- ``narrate_general_guidance(profile, guidance_key)`` — produces a
  ``GuidanceOnlyPayload`` for cohorts the calculator refuses to emit
  numbers for (minors, CKD 4-5, incomplete profiles).

Both methods parse via ``llm_service.complete_validated`` so the
output is schema-validated with one self-correcting retry. On LLM
failure we return an **empty** payload with the known clinician note
attached — the numeric part of the plan still ships (empty
narrative is strictly better than no plan).

The ``_merge_profile_structural``-style pattern from SPEC-002 does not
apply here: narration has no sensible structural fallback. We accept
an empty narrative as the failure mode.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from llm_service import (
    LLMError,
    LLMPermanentError,
    LLMSchemaValidationError,
    complete_validated,
    get_client,
)

from ...models import ClientProfile, DailyTargets
from .schemas import GuidanceOnlyPayload, NarrativePayload

logger = logging.getLogger(__name__)


NARRATOR_SYSTEM_PROMPT = """You are an expert nutritionist authoring short, practical narrative guidance.

You will be given:
- A client profile (household, dietary needs, allergies, lifestyle, goals, biometrics, clinical context).
- EXACT numeric daily targets (kcal, protein_g, carbs_g, fat_g, plus micronutrient targets).
- A structured rationale explaining how those numbers were derived.

Your job: write brief, actionable guidance to pair WITH those numbers.

STRICT RULES:
- DO NOT emit, re-state, adjust, or contradict any numeric target.
  You are NOT the source of numbers. The calculator is.
- You MAY mention numbers in prose ("your protein target of ~130 g") only if the number is already in the inputs. Never invent values.
- Never invent allergies or medical conditions.
- Keep each list item short (≤ 15 words). Max 6 bullets per list.
- Output valid JSON matching the schema. No markdown fences. No prose outside the JSON."""


GUIDANCE_SYSTEM_PROMPT = """You are an expert nutritionist authoring general food-group guidance for a user whose profile does NOT get numeric targets from our calculator.

You will be given a client profile and a ``guidance_key`` explaining why numbers are not being emitted (e.g. "minor", "ckd_stage_4", "pregnancy_t2", "insufficient_input").

STRICT RULES:
- DO NOT emit any numeric target. Not kcal, not grams, not mg.
  If you mention a number at all, the output will be rejected.
- Emit qualitative food-group guidance: kinds of foods to favor, kinds to limit, general patterns (e.g. "lean proteins at each meal", "high-potassium foods").
- Always include a ``clinician_note`` asking the user to work with a clinician or registered dietitian. This field must be non-empty.
- Never invent allergies or medical conditions.
- Keep lists short (≤ 6 bullets, ≤ 15 words each).
- Output valid JSON matching the schema. No markdown fences. No prose outside the JSON."""


class NutritionistAgent:
    """SPEC-004 narrator.

    The agent is thin: it owns two prompts and delegates to
    ``llm_service.complete_validated`` for actual completion. No
    ``strands.Agent`` dependency — the calculator is the source of
    truth, so a second layer of orchestration buys nothing.
    """

    def __init__(self, *, agent_key: str = "nutrition_meal_planning") -> None:
        self._agent_key = agent_key
        self._client = None  # lazily resolved on first call

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = get_client(self._agent_key)
        return self._client

    def narrate_plan(
        self,
        profile: ClientProfile,
        targets: DailyTargets,
        rationale_dict: dict | None,
    ) -> NarrativePayload:
        """Author narrative for a plan that has numeric targets."""
        prompt = self._build_plan_prompt(profile, targets, rationale_dict)
        return self._call_validated(
            prompt,
            schema=NarrativePayload,
            system_prompt=NARRATOR_SYSTEM_PROMPT,
            empty_fallback=NarrativePayload(),
        )

    def narrate_general_guidance(
        self,
        profile: ClientProfile,
        guidance_key: str,
        default_clinician_note: str,
    ) -> GuidanceOnlyPayload:
        """Author qualitative guidance with a required clinician note."""
        prompt = self._build_guidance_prompt(profile, guidance_key)
        # Fallback carries the calculator's pre-authored clinician
        # note so even on total LLM failure the user sees "work with
        # your clinician" rather than nothing.
        empty = GuidanceOnlyPayload(
            clinician_note=default_clinician_note
            or (
                "Please work with your clinician or registered dietitian for personalized guidance."
            )
        )
        return self._call_validated(
            prompt,
            schema=GuidanceOnlyPayload,
            system_prompt=GUIDANCE_SYSTEM_PROMPT,
            empty_fallback=empty,
        )

    def _call_validated(
        self,
        prompt: str,
        *,
        schema,
        system_prompt: str,
        empty_fallback,
    ):
        """Wrap ``complete_validated`` with terminal-error fallback.

        llm_service raises several error classes; any of them results
        in the empty-fallback payload so the numeric half of the plan
        still ships.
        """
        try:
            client = self._get_client()
            return complete_validated(
                client,
                prompt,
                schema=schema,
                system_prompt=system_prompt,
            )
        except LLMSchemaValidationError as e:
            logger.warning("Nutritionist narrator schema validation failed after retry: %s", e)
            return empty_fallback
        except LLMPermanentError as e:
            logger.warning("Nutritionist narrator permanent LLM error: %s", e)
            return empty_fallback
        except LLMError as e:
            logger.warning("Nutritionist narrator transient LLM error: %s", e)
            return empty_fallback
        except Exception as e:  # pragma: no cover - defensive
            logger.exception("Nutritionist narrator unexpected error: %s", e)
            return empty_fallback

    # --- Prompt builders -----------------------------------------------

    @staticmethod
    def _build_plan_prompt(
        profile: ClientProfile,
        targets: DailyTargets,
        rationale_dict: dict | None,
    ) -> str:
        profile_json = profile.model_dump_json(indent=2)
        targets_json = targets.model_dump_json(indent=2)
        rationale_blob = (
            json.dumps(rationale_dict, indent=2, default=str)
            if rationale_dict
            else "(not provided)"
        )
        return (
            "Client profile:\n"
            + profile_json
            + "\n\nNumeric daily targets (authoritative — do NOT restate or adjust):\n"
            + targets_json
            + "\n\nCalculator rationale (for context; do not contradict):\n"
            + rationale_blob
            + "\n\nProduce narrative ONLY: balance_guidelines, foods_to_emphasize, "
            "foods_to_avoid, notes, summary. Output valid JSON only."
        )

    @staticmethod
    def _build_guidance_prompt(profile: ClientProfile, guidance_key: str) -> str:
        profile_json = profile.model_dump_json(indent=2)
        return (
            "Client profile:\n"
            + profile_json
            + f"\n\nguidance_key: {guidance_key}\n"
            + "\nOutput qualitative JSON: balance_guidelines, foods_to_emphasize, "
            "foods_to_avoid, notes, clinician_note. Never include numeric "
            "targets. Output valid JSON only."
        )
