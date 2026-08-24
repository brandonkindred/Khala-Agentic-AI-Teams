"""Unit tests for the LLM-as-judge quality scoring helper.

Covers ``PhaseQualityScore`` field bounds and ``score_phase_output`` against
the forced dummy client (deterministic, offline, no network) -- the same
guarantee ``eval_selective_context.py``'s own tests rely on.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from branding_team.models import (
    BrandPhase,
    ChannelActivationOutput,
    GovernanceOutput,
    StrategicCoreOutput,
)
from branding_team.scripts.quality_judge import (
    PairedPhaseQualityScore,
    PhaseQualityScore,
    _build_judge_prompt,
    _build_paired_judge_prompt,
    score_phase_output,
    score_phase_output_pair,
)
from branding_team.tests.conftest import make_mission
from llm_service import get_client
from llm_service.dummy_provider import force_dummy_llm_provider


def test_phase_quality_score_accepts_boundary_values() -> None:
    """1 and 5 are both valid scores on every dimension (inclusive bounds)."""
    low = PhaseQualityScore(strategic_coherence=1, completeness=1, brand_consistency=1)
    high = PhaseQualityScore(strategic_coherence=5, completeness=5, brand_consistency=5)
    assert low.strategic_coherence == 1
    assert high.brand_consistency == 5


@pytest.mark.parametrize("bad_value", [0, 6, -1])
def test_phase_quality_score_rejects_out_of_range_values(bad_value: int) -> None:
    """Scores outside [1, 5] must fail Pydantic validation for every dimension."""
    with pytest.raises(ValidationError):
        PhaseQualityScore(
            strategic_coherence=bad_value, completeness=3, brand_consistency=3, rationale=""
        )
    with pytest.raises(ValidationError):
        PhaseQualityScore(
            strategic_coherence=3, completeness=bad_value, brand_consistency=3, rationale=""
        )
    with pytest.raises(ValidationError):
        PhaseQualityScore(
            strategic_coherence=3, completeness=3, brand_consistency=bad_value, rationale=""
        )


def test_phase_quality_score_rationale_defaults_to_empty_string() -> None:
    """rationale is optional; omitting it must not raise."""
    score = PhaseQualityScore(strategic_coherence=3, completeness=3, brand_consistency=3)
    assert score.rationale == ""


def test_score_phase_output_returns_valid_score_under_dummy_client() -> None:
    """score_phase_output against the forced dummy client returns a validated
    PhaseQualityScore with no network access, mirroring how run_eval calls it
    in its default (non-live) mode.
    """
    mission = make_mission()
    output = GovernanceOutput()

    with force_dummy_llm_provider():
        client = get_client()
        score = score_phase_output(
            client,
            mission=mission,
            phase=BrandPhase.GOVERNANCE,
            output=output,
            variant_label="selective",
        )

    assert isinstance(score, PhaseQualityScore)
    assert 1 <= score.strategic_coherence <= 5
    assert 1 <= score.completeness <= 5
    assert 1 <= score.brand_consistency <= 5


def test_score_phase_output_dummy_client_is_deterministic() -> None:
    """Two calls against the dummy client for the same phase must return
    identical scores -- this is what guarantees run_eval's default mode
    never reports a false regression.
    """
    mission = make_mission()
    output = GovernanceOutput()

    with force_dummy_llm_provider():
        client = get_client()
        first = score_phase_output(
            client,
            mission=mission,
            phase=BrandPhase.GOVERNANCE,
            output=output,
            variant_label="selective",
        )
        second = score_phase_output(
            client,
            mission=mission,
            phase=BrandPhase.GOVERNANCE,
            output=output,
            variant_label="full",
        )

    assert first == second


def test_build_judge_prompt_embeds_strategic_core_when_supplied() -> None:
    """The generated strategic core must appear in the prompt so the judge can
    score coherence against what was actually generated upstream, not just the
    raw mission brief -- the P1 fix this eval PR applies.
    """
    mission = make_mission()
    strategic_core = StrategicCoreOutput(mission_statement="Ship brand with the product.")
    output = GovernanceOutput()

    prompt = _build_judge_prompt(
        mission=mission,
        phase=BrandPhase.GOVERNANCE,
        output=output,
        strategic_core=strategic_core,
    )

    assert "GENERATED STRATEGIC CORE" in prompt
    assert "Ship brand with the product." in prompt


def test_build_judge_prompt_omits_strategic_core_block_when_none() -> None:
    """No GENERATED STRATEGIC CORE section header is rendered when strategic_core
    is None (e.g. when judging the strategic core phase itself, which has no
    upstream strategic core to compare against) -- only the fixed reminder in
    the TASK section mentions the phrase in that case.
    """
    mission = make_mission()
    output = GovernanceOutput()

    prompt = _build_judge_prompt(
        mission=mission,
        phase=BrandPhase.GOVERNANCE,
        output=output,
        strategic_core=None,
    )

    assert "--- GENERATED STRATEGIC CORE" not in prompt


def test_build_judge_prompt_never_reveals_variant_label() -> None:
    """The judge must be blind to which context variant produced the output --
    revealing "selective"/"full" would let it score on the expected effect of
    context reduction rather than actual quality (Codex P2 finding).
    """
    mission = make_mission()
    strategic_core = StrategicCoreOutput(mission_statement="Ship brand with the product.")
    output = GovernanceOutput()

    prompt = _build_judge_prompt(
        mission=mission,
        phase=BrandPhase.GOVERNANCE,
        output=output,
        strategic_core=strategic_core,
    )

    assert "selective" not in prompt.lower()
    assert "full context" not in prompt.lower()
    assert "full-context" not in prompt.lower()


def test_score_phase_output_pair_returns_valid_scores_under_dummy_client() -> None:
    """score_phase_output_pair against the forced dummy client returns a validated
    PairedPhaseQualityScore, mirroring how run_eval calls it for a diverging phase.
    """
    mission = make_mission()
    output_a = ChannelActivationOutput()
    output_b = ChannelActivationOutput()

    with force_dummy_llm_provider():
        client = get_client()
        paired = score_phase_output_pair(
            client,
            mission=mission,
            phase=BrandPhase.CHANNEL_ACTIVATION,
            output_a=output_a,
            output_b=output_b,
        )

    assert isinstance(paired, PairedPhaseQualityScore)
    for score in (paired.output_a, paired.output_b):
        assert 1 <= score.strategic_coherence <= 5
        assert 1 <= score.completeness <= 5
        assert 1 <= score.brand_consistency <= 5


def test_build_paired_judge_prompt_never_reveals_variant_label() -> None:
    """The paired prompt must use neutral A/B labels only -- never selective/full --
    so a single judge call can't score based on which context variant is which.
    """
    mission = make_mission()
    strategic_core = StrategicCoreOutput(mission_statement="Ship brand with the product.")
    output_a = GovernanceOutput()
    output_b = GovernanceOutput()

    prompt = _build_paired_judge_prompt(
        mission=mission,
        phase=BrandPhase.GOVERNANCE,
        output_a=output_a,
        output_b=output_b,
        strategic_core=strategic_core,
    )

    assert "OUTPUT A" in prompt
    assert "OUTPUT B" in prompt
    assert "selective" not in prompt.lower()
    assert "full context" not in prompt.lower()
    assert "full-context" not in prompt.lower()


def test_build_paired_judge_prompt_embeds_strategic_core_once() -> None:
    """The shared strategic core must appear exactly once, not duplicated per output."""
    mission = make_mission()
    strategic_core = StrategicCoreOutput(mission_statement="Ship brand with the product.")
    output_a = GovernanceOutput()
    output_b = GovernanceOutput()

    prompt = _build_paired_judge_prompt(
        mission=mission,
        phase=BrandPhase.GOVERNANCE,
        output_a=output_a,
        output_b=output_b,
        strategic_core=strategic_core,
    )

    assert prompt.count("--- GENERATED STRATEGIC CORE") == 1
