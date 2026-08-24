"""LLM-as-judge quality scoring for a single branding phase output.

Scores a phase's output (``ChannelActivationOutput``/``GovernanceOutput``, or
any other phase model) against three dimensions -- strategic coherence,
completeness, brand consistency -- each on a 1-5 scale, plus a short
rationale. Used by ``branding_team.scripts.eval_selective_context`` to
compare the real, selective-context output against a full-context variant
and flag any quality regression.

The rubric below (:data:`_JUDGE_SYSTEM_PROMPT`) is the reproducible judge
prompt required by the eval task: it is the single source of truth for how
scores are assigned, and :func:`score_phase_output` is the only call site
that renders it, so every judge call in this codebase uses the identical
wording.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from llm_service import LLMClient, complete_validated

from ..models import BrandingMission, BrandPhase

JUDGE_RUBRIC_VERSION = "v1"

_JUDGE_SYSTEM_PROMPT = """\
You are an independent Brand Strategy Reviewer. You did NOT write the phase \
output under review; your job is to score it on three dimensions, each on a \
1-5 integer scale (1 = poor, 3 = adequate, 5 = excellent):

1. strategic_coherence -- how internally consistent and logically connected \
   the output is with the mission's strategic core (purpose, mission, \
   vision, positioning, target audience) and with its own stated fields. \
   Contradictions, non-sequiturs, or content that ignores the mission's \
   stated strategy score low.

2. completeness -- how fully the output covers what its schema's fields ask \
   for: no empty/placeholder sections, no fields left thin relative to \
   their sibling fields, no obviously missing coverage a brand phase of \
   this kind should include.

3. brand_consistency -- how well the output's tone, terminology, and \
   substance align with the mission's stated company description, target \
   audience, values, and desired voice supplied in the prompt. Generic, \
   boilerplate, or off-audience content scores low.

OUTPUT contract:
 - Output a SINGLE JSON object matching this schema:
   {
     "strategic_coherence": <1-5 integer>,
     "completeness": <1-5 integer>,
     "brand_consistency": <1-5 integer>,
     "rationale": "<short justification covering all three scores>"
   }
 - Return JSON only. No markdown fences. No prose outside the object.
"""


class PhaseQualityScore(BaseModel):
    """LLM-as-judge score for one phase output on three 1-5 dimensions.

    Preconditions:
        None -- constructed only via ``model_validate``/``model_validate_json``
        against a judge LLM's structured reply.
    Postconditions:
        Each of ``strategic_coherence``, ``completeness``, and
        ``brand_consistency`` is an integer in ``[1, 5]`` inclusive;
        construction with a value outside that range raises
        ``pydantic.ValidationError``. ``rationale`` may be empty.
    """

    strategic_coherence: int = Field(ge=1, le=5)
    completeness: int = Field(ge=1, le=5)
    brand_consistency: int = Field(ge=1, le=5)
    rationale: str = ""


def _build_judge_prompt(
    *, mission: BrandingMission, phase: BrandPhase, output: BaseModel, variant_label: str
) -> str:
    """Render the user prompt for one judge call.

    Preconditions:
        ``output`` is the Pydantic output model instance for ``phase``.
    Postconditions:
        Returns a non-empty string embedding the mission's identifying
        fields, the phase name, the variant label (for the judge's own
        context only -- it does not see the other variant), and
        ``output.model_dump(mode="json")`` as pretty-printed JSON.
    """
    mission_summary = json.dumps(
        {
            "company_name": mission.company_name,
            "company_description": mission.company_description,
            "target_audience": mission.target_audience,
            "values": mission.values,
            "desired_voice": mission.desired_voice,
        },
        indent=2,
    )
    output_json = json.dumps(output.model_dump(mode="json"), indent=2)
    return (
        "--- MISSION ---\n"
        f"{mission_summary}\n\n"
        f"--- PHASE ({phase.value}, {variant_label} context) OUTPUT ---\n"
        f"{output_json}\n\n"
        "--- TASK ---\n"
        "Score this phase output against the rubric in your system prompt."
    )


def score_phase_output(
    client: LLMClient,
    *,
    mission: BrandingMission,
    phase: BrandPhase,
    output: BaseModel,
    variant_label: str,
) -> PhaseQualityScore:
    """Score ``output`` (one phase, one context variant) via LLM-as-judge.

    Preconditions:
        ``client`` is a ready :class:`LLMClient`. ``output`` is the real
        Pydantic output model instance produced for ``phase``. ``variant_label``
        is a short caller-chosen string (e.g. ``"selective"``/``"full"``)
        used only for the judge's own framing, never for scoring logic.
    Postconditions:
        Returns a validated :class:`PhaseQualityScore`. ``structured_output_model``
        is forwarded to ``client.complete_json`` so a dummy/test double can
        route by exact class name instead of parsing prompt text.
    """
    prompt = _build_judge_prompt(
        mission=mission, phase=phase, output=output, variant_label=variant_label
    )
    return complete_validated(
        client,
        prompt,
        schema=PhaseQualityScore,
        objective="score branding phase quality",
        system_prompt=_JUDGE_SYSTEM_PROMPT,
        correction_attempts=1,
        structured_output_model=PhaseQualityScore,
    )


__all__ = [
    "JUDGE_RUBRIC_VERSION",
    "PhaseQualityScore",
    "score_phase_output",
]
