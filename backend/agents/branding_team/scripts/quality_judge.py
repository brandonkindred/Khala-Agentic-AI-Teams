"""LLM-as-judge quality scoring for branding phase outputs.

Scores a phase's output (``ChannelActivationOutput``/``GovernanceOutput``, or
any other phase model) against three dimensions -- strategic coherence,
completeness, brand consistency -- each on a 1-5 scale, plus a short
rationale. Used by ``branding_team.scripts.eval_selective_context`` to
compare the real, selective-context output against a full-context variant
and flag any quality regression.

Two reproducible rubric prompts are defined, sharing the same three scoring
dimensions and the same blind-to-variant design (see
:func:`_build_judge_prompt`/:func:`_build_paired_judge_prompt`), but with
different output schemas:

- :data:`_JUDGE_SYSTEM_PROMPT` scores one output; :func:`score_phase_output`
  is its only call site.
- :data:`_PAIRED_JUDGE_SYSTEM_PROMPT` scores two candidates in a single call
  (nested ``output_a``/``output_b``), used by :func:`score_phase_output_pair`
  to guarantee both are judged by the identical provider/model response and
  to cancel positional bias when called under both A/B orderings (see
  ``eval_selective_context.run_eval``).

Each is the single source of truth for its own shape: every single-output
judge call uses ``_JUDGE_SYSTEM_PROMPT`` verbatim, and every paired judge
call uses ``_PAIRED_JUDGE_SYSTEM_PROMPT`` verbatim.
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

    Dimensions are typed ``float`` (not ``int``) even though a single judge
    call always returns an integer 1-5 rating per the rubric: this same
    model doubles as the container for an *averaged* score --
    ``eval_selective_context._average_phase_quality_score`` combines two
    real per-call scores (e.g. 3 and 4) into a genuine half-point value
    (3.5), which an int-typed field would silently round away before the
    0.5-point regression threshold ever saw it.

    Preconditions:
        None -- constructed only via ``model_validate``/``model_validate_json``
        against a judge LLM's structured reply, or via
        ``_average_phase_quality_score``.
    Postconditions:
        Each of ``strategic_coherence``, ``completeness``, and
        ``brand_consistency`` is a number in ``[1, 5]`` inclusive;
        construction with a value outside that range raises
        ``pydantic.ValidationError``. ``rationale`` may be empty.
    """

    strategic_coherence: float = Field(ge=1, le=5)
    completeness: float = Field(ge=1, le=5)
    brand_consistency: float = Field(ge=1, le=5)
    rationale: str = ""


class PairedPhaseQualityScore(BaseModel):
    """LLM-as-judge scores for two candidate outputs, from a single judge call.

    Preconditions:
        Same as :class:`PhaseQualityScore`, applied independently to each field.
    Postconditions:
        ``output_a`` and ``output_b`` are each a valid :class:`PhaseQualityScore`.
    """

    output_a: PhaseQualityScore
    output_b: PhaseQualityScore


_PAIRED_JUDGE_SYSTEM_PROMPT = """\
You are an independent Brand Strategy Reviewer. You did NOT write either output \
under review. OUTPUT A and OUTPUT B are two independently produced candidates \
for the same phase and mission -- score EACH one independently and on its own \
merits, on three dimensions, each on a 1-5 integer scale (1 = poor, 3 = \
adequate, 5 = excellent). Do not assume either is better because of its letter \
or position; do not score them relative to each other.

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
     "output_a": {
       "strategic_coherence": <1-5 integer>,
       "completeness": <1-5 integer>,
       "brand_consistency": <1-5 integer>,
       "rationale": "<short justification covering all three scores>"
     },
     "output_b": {<same shape, scored independently>}
   }
 - Return JSON only. No markdown fences. No prose outside the object.
"""


def _render_mission_summary(mission: BrandingMission) -> str:
    """Render the mission's judge-relevant identifying fields as pretty-printed JSON.

    Shared by :func:`_build_judge_prompt` and :func:`_build_paired_judge_prompt`
    so the two prompt shapes can never drift on which mission fields the judge sees.

    Preconditions: ``mission`` is a valid ``BrandingMission``.
    Postconditions: returns a non-empty JSON string.
    """
    return json.dumps(
        {
            "company_name": mission.company_name,
            "company_description": mission.company_description,
            "target_audience": mission.target_audience,
            "values": mission.values,
            "desired_voice": mission.desired_voice,
        },
        indent=2,
    )


def _render_strategic_core_block(strategic_core: BaseModel | None) -> str:
    """Render the optional GENERATED STRATEGIC CORE prompt block.

    Shared by :func:`_build_judge_prompt` and :func:`_build_paired_judge_prompt`.

    Preconditions: none.
    Postconditions: returns ``""`` when ``strategic_core`` is ``None``;
    otherwise a non-empty block ending in a blank line.
    """
    if strategic_core is None:
        return ""
    strategic_core_json = json.dumps(strategic_core.model_dump(mode="json"), indent=2)
    return f"--- GENERATED STRATEGIC CORE ---\n{strategic_core_json}\n\n"


def _build_judge_prompt(
    *,
    mission: BrandingMission,
    phase: BrandPhase,
    output: BaseModel,
    strategic_core: BaseModel | None,
) -> str:
    """Render the user prompt for one judge call.

    Deliberately blind to which context variant (selective vs. full) produced
    ``output`` and ``strategic_core``: revealing that would let the judge
    score based on the *expected* effect of context reduction rather than
    the output's actual quality, and a single point of integer-scale bias is
    enough to flip a regression verdict. Callers distinguish variants for
    their own bookkeeping (e.g. ``score_phase_output``'s ``variant_label``
    forwarded only to LLM call telemetry); nothing variant-specific reaches
    this prompt's text.

    Preconditions:
        ``output`` is the Pydantic output model instance for ``phase``.
        ``strategic_core`` is the ``StrategicCoreOutput``-shaped model this
        variant's own pipeline run generated for ``BrandPhase.STRATEGIC_CORE``
        (``None`` only when judging the strategic core phase itself, which
        has no upstream strategic core to compare against).
    Postconditions:
        Returns a non-empty string embedding the mission's identifying
        fields, the generated strategic core (when supplied) so the judge
        can score coherence against what was actually generated upstream
        (not just the raw mission brief), the phase name, and
        ``output.model_dump(mode="json")`` as pretty-printed JSON -- with no
        selective/full-context label anywhere in the returned text.
    """
    mission_summary = _render_mission_summary(mission)
    strategic_core_block = _render_strategic_core_block(strategic_core)
    output_json = json.dumps(output.model_dump(mode="json"), indent=2)
    return (
        "--- MISSION ---\n"
        f"{mission_summary}\n\n"
        f"{strategic_core_block}"
        f"--- PHASE ({phase.value}) OUTPUT ---\n"
        f"{output_json}\n\n"
        "--- TASK ---\n"
        "Score this phase output against the rubric in your system prompt. Score "
        "strategic_coherence against the GENERATED STRATEGIC CORE above (when supplied), "
        "not just the raw mission brief -- that is the actual upstream strategy this "
        "phase's output must stay consistent with."
    )


def score_phase_output(
    client: LLMClient,
    *,
    mission: BrandingMission,
    phase: BrandPhase,
    output: BaseModel,
    variant_label: str,
    strategic_core: BaseModel | None = None,
) -> PhaseQualityScore:
    """Score ``output`` (one phase, one context variant) via LLM-as-judge.

    Preconditions:
        ``client`` is a ready :class:`LLMClient`. ``output`` is the real
        Pydantic output model instance produced for ``phase``. ``variant_label``
        is a short caller-chosen string (e.g. ``"selective"``/``"full"``)
        forwarded only to ``complete_validated``'s ``objective`` for LLM call
        telemetry/log attribution -- it never reaches the rendered prompt
        text the judge sees (see ``_build_judge_prompt``), so the judge
        cannot score based on which variant produced ``output``.
        ``strategic_core`` should be this same variant's own generated
        ``BrandPhase.STRATEGIC_CORE`` output when ``phase`` is downstream of
        it (every phase this eval judges is); omit only when judging the
        strategic core phase itself.
    Postconditions:
        Returns a validated :class:`PhaseQualityScore`.

        Note: ``structured_output_model`` is passed to ``complete_validated``
        so a dummy/test double can route by exact class name instead of
        parsing prompt text.
    """
    prompt = _build_judge_prompt(
        mission=mission,
        phase=phase,
        output=output,
        strategic_core=strategic_core,
    )
    return complete_validated(
        client,
        prompt,
        schema=PhaseQualityScore,
        objective=f"score branding phase quality ({variant_label})",
        system_prompt=_JUDGE_SYSTEM_PROMPT,
        correction_attempts=1,
        structured_output_model=PhaseQualityScore,
    )


def _build_paired_judge_prompt(
    *,
    mission: BrandingMission,
    phase: BrandPhase,
    output_a: BaseModel,
    output_b: BaseModel,
    strategic_core: BaseModel | None,
) -> str:
    """Render the user prompt for one paired judge call scoring two outputs.

    Preconditions:
        ``output_a``/``output_b`` are the two Pydantic output model instances
        for ``phase`` under comparison. ``strategic_core`` is shared between
        both (every phase this eval judges has a shared, non-diverging
        ``STRATEGIC_CORE`` upstream of it -- see
        ``eval_selective_context._first_diverging_phase``), so only one copy
        is ever rendered.
    Postconditions:
        Returns a non-empty string with no selective/full-context labeling
        anywhere -- only the neutral "OUTPUT A"/"OUTPUT B" headings, which
        carry no information about which context variant produced which.
    """
    mission_summary = _render_mission_summary(mission)
    strategic_core_block = _render_strategic_core_block(strategic_core)
    output_a_json = json.dumps(output_a.model_dump(mode="json"), indent=2)
    output_b_json = json.dumps(output_b.model_dump(mode="json"), indent=2)
    return (
        "--- MISSION ---\n"
        f"{mission_summary}\n\n"
        f"{strategic_core_block}"
        f"--- PHASE ({phase.value}) OUTPUT A ---\n"
        f"{output_a_json}\n\n"
        f"--- PHASE ({phase.value}) OUTPUT B ---\n"
        f"{output_b_json}\n\n"
        "--- TASK ---\n"
        "Score OUTPUT A and OUTPUT B independently against the rubric in your system "
        "prompt. Score strategic_coherence against the GENERATED STRATEGIC CORE above "
        "(when supplied), not just the raw mission brief -- that is the actual upstream "
        "strategy each output must stay consistent with."
    )


def score_phase_output_pair(
    client: LLMClient,
    *,
    mission: BrandingMission,
    phase: BrandPhase,
    output_a: BaseModel,
    output_b: BaseModel,
    strategic_core: BaseModel | None = None,
) -> PairedPhaseQualityScore:
    """Score two candidate outputs for the same phase in a single judge call.

    Scoring both outputs in one LLM call -- rather than two separate
    ``score_phase_output`` calls -- guarantees they are judged by the
    identical underlying model/provider response. Under a live,
    multi-provider ``FailoverLLMClient``, two separate calls are not
    guaranteed to hit the same provider: a 429 between them can hand the
    second call off to a different provider with a different scoring
    calibration, so an apparent quality delta could reflect a change in
    *judge* rather than a change in *output quality*. A single call has no
    such window. Also used to keep the judge blind to which output is which
    (see :func:`_build_paired_judge_prompt`): callers assign ``output_a``/
    ``output_b`` for their own bookkeeping, but neither label reveals a
    selective/full-context distinction to the judge.

    Preconditions:
        ``client`` is a ready :class:`LLMClient`. ``output_a``/``output_b``
        are the real Pydantic output model instances produced for ``phase``.
        ``strategic_core`` should be the shared, non-diverging generated
        ``BrandPhase.STRATEGIC_CORE`` output (see
        ``eval_selective_context._first_diverging_phase``); omit only when
        judging the strategic core phase itself.
    Postconditions:
        Returns a validated :class:`PairedPhaseQualityScore`.

        Note: ``structured_output_model`` is passed to ``complete_validated``
        so a dummy/test double can route by exact class name instead of
        parsing prompt text.
    """
    prompt = _build_paired_judge_prompt(
        mission=mission,
        phase=phase,
        output_a=output_a,
        output_b=output_b,
        strategic_core=strategic_core,
    )
    return complete_validated(
        client,
        prompt,
        schema=PairedPhaseQualityScore,
        objective="score branding phase quality (paired)",
        system_prompt=_PAIRED_JUDGE_SYSTEM_PROMPT,
        correction_attempts=1,
        structured_output_model=PairedPhaseQualityScore,
    )


__all__ = [
    "JUDGE_RUBRIC_VERSION",
    "PairedPhaseQualityScore",
    "PhaseQualityScore",
    "score_phase_output",
    "score_phase_output_pair",
]
