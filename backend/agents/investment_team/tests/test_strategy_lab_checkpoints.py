"""Tests for the checkpoint/resume-state model family in
``strategy_lab.checkpoints``: construction, immutability, serialization
round-trip, hash wiring, precondition validation, pinned-stage enforcement,
and stage ordering.

This is pure data-model coverage — nothing here exercises capture points or
consumption, since neither exists yet (see the module docstring in
``strategy_lab/checkpoints.py``).
"""

from __future__ import annotations

from typing import Any, Callable, Dict

import pytest
from pydantic import ValidationError

from investment_team.models import CodeRevision, GateEvent, SpecRevision, StrategySpec
from investment_team.strategy_lab import phases
from investment_team.strategy_lab.checkpoints import (
    PIPELINE_STAGES,
    AlignmentCheckpoint,
    DesignCheckpoint,
    PipelineCheckpoint,
    PipelineStage,
    RefinementCheckpoint,
    ReviewCheckpoint,
    SynthesisCheckpoint,
    parse_checkpoint,
)

_EMPTY_CODE_HASH = phases.hash_code(None)


def _spec(**overrides: Any) -> StrategySpec:
    base: Dict[str, Any] = {
        "strategy_id": "strat-checkpoint-1",
        "authored_by": "DesignAgent",
        "asset_class": "stocks",
        "hypothesis": "test hypothesis",
        "signal_definition": "test signal",
        "timeframe": "1d",
    }
    base.update(overrides)
    return StrategySpec(**base)


def _identity(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "run_id": "run-1",
        "cycle_scope": "cycle-scope-1",
        "design_attempt": 0,
        "generation": 1,
        "captured_at": "2026-08-27T00:00:00Z",
        "budget_calls": 5,
        "gate_results": [{"gate_name": "risk_limits", "passed": True}],
    }
    base.update(overrides)
    return base


def _spec_revision() -> SpecRevision:
    return SpecRevision(
        phase="design",
        agent="DesignAgent",
        timestamp="2026-08-27T00:00:00Z",
        before_hash="a" * 64,
        after_hash="b" * 64,
        diff="--- a\n+++ b\n",
        reason="tightened risk limits",
    )


def _code_revision() -> CodeRevision:
    return CodeRevision(
        phase="synthesis",
        agent="SynthesisAgent",
        timestamp="2026-08-27T00:00:00Z",
        before_hash="c" * 64,
        after_hash="d" * 64,
        diff="--- a\n+++ b\n",
        reason="fixed off-by-one",
    )


def _gate_event() -> GateEvent:
    return GateEvent(
        phase="alignment",
        gate_name="trade_alignment",
        passed=True,
        severity="info",
        details="aligned",
        timestamp="2026-08-27T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# PipelineStage ordering
# ---------------------------------------------------------------------------


def test_pipeline_stages_membership_and_order() -> None:
    assert PIPELINE_STAGES == (
        PipelineStage.DESIGN,
        PipelineStage.REVIEW,
        PipelineStage.SYNTHESIS,
        PipelineStage.REFINEMENT,
        PipelineStage.ALIGNMENT,
    )
    assert len(PIPELINE_STAGES) == 5


# ---------------------------------------------------------------------------
# Construction, one per subclass
# ---------------------------------------------------------------------------


def test_design_checkpoint_construction() -> None:
    spec = _spec()
    cp = DesignCheckpoint(
        **_identity(),
        spec_hash=phases.hash_spec(spec),
        code_hash=_EMPTY_CODE_HASH,
        spec=spec,
        rationale="because",
        design_context={"k": "v"},
    )
    assert cp.stage == PipelineStage.DESIGN


def test_review_checkpoint_construction() -> None:
    spec = _spec()
    cp = ReviewCheckpoint(
        **_identity(),
        spec_hash=phases.hash_spec(spec),
        code_hash=_EMPTY_CODE_HASH,
        spec=spec,
        rationale="because",
        design_context={},
        spec_history=[_spec_revision()],
        review_rounds_completed=2,
    )
    assert cp.stage == PipelineStage.REVIEW
    assert cp.review_rounds_completed == 2
    assert len(cp.spec_history) == 1


def test_synthesis_checkpoint_construction() -> None:
    spec = _spec()
    code = "def run(): pass"
    cp = SynthesisCheckpoint(
        **_identity(),
        spec_hash=phases.hash_spec(spec),
        code_hash=phases.hash_code(code),
        spec=spec,
        rationale="because",
        code=code,
        code_history=[_code_revision()],
    )
    assert cp.stage == PipelineStage.SYNTHESIS
    assert cp.code == code
    assert cp.spec == spec


def test_refinement_checkpoint_construction() -> None:
    spec = _spec()
    code = "def run(): return 1"
    cp = RefinementCheckpoint(
        **_identity(),
        spec_hash=phases.hash_spec(spec),
        code_hash=phases.hash_code(code),
        spec=spec,
        rationale="because",
        code=code,
        code_history=[_code_revision()],
        refinement_rounds_completed=1,
    )
    assert cp.stage == PipelineStage.REFINEMENT
    assert cp.refinement_rounds_completed == 1
    assert cp.spec == spec


def test_alignment_checkpoint_construction() -> None:
    spec = _spec()
    code = "def run(): return 2"
    cp = AlignmentCheckpoint(
        **_identity(),
        spec_hash=phases.hash_spec(spec),
        code_hash=phases.hash_code(code),
        spec=spec,
        rationale="because",
        code=code,
        alignment_rounds_completed=3,
        gate_timeline=[_gate_event()],
    )
    assert cp.stage == PipelineStage.ALIGNMENT
    assert cp.alignment_rounds_completed == 3
    assert len(cp.gate_timeline) == 1
    assert cp.spec == spec


# ---------------------------------------------------------------------------
# Shared per-subclass builders (reused by the immutability, serialization
# round-trip, and pinned-stage parametrizations below, one entry per stage).
# Each builder captures a single ``spec``/``code`` value and reuses it for
# both the hash and the payload, rather than constructing a fresh one for
# each — so a hash/payload mismatch is impossible even if these helpers ever
# gain non-deterministic fields.
# ---------------------------------------------------------------------------


def _build_design_checkpoint(**overrides: Any) -> DesignCheckpoint:
    spec = _spec()
    kwargs = dict(
        **_identity(),
        spec_hash=phases.hash_spec(spec),
        code_hash=_EMPTY_CODE_HASH,
        spec=spec,
        rationale="because",
    )
    kwargs.update(overrides)
    return DesignCheckpoint(**kwargs)


def _build_review_checkpoint(**overrides: Any) -> ReviewCheckpoint:
    spec = _spec()
    kwargs = dict(
        **_identity(),
        spec_hash=phases.hash_spec(spec),
        code_hash=_EMPTY_CODE_HASH,
        spec=spec,
        rationale="because",
        spec_history=[_spec_revision()],
        review_rounds_completed=1,
    )
    kwargs.update(overrides)
    return ReviewCheckpoint(**kwargs)


def _build_synthesis_checkpoint(**overrides: Any) -> SynthesisCheckpoint:
    spec = _spec()
    code = "code"
    kwargs = dict(
        **_identity(),
        spec_hash=phases.hash_spec(spec),
        code_hash=phases.hash_code(code),
        spec=spec,
        rationale="because",
        code=code,
        code_history=[_code_revision()],
    )
    kwargs.update(overrides)
    return SynthesisCheckpoint(**kwargs)


def _build_refinement_checkpoint(**overrides: Any) -> RefinementCheckpoint:
    spec = _spec()
    code = "code"
    kwargs = dict(
        **_identity(),
        spec_hash=phases.hash_spec(spec),
        code_hash=phases.hash_code(code),
        spec=spec,
        rationale="because",
        code=code,
        code_history=[_code_revision()],
        refinement_rounds_completed=1,
    )
    kwargs.update(overrides)
    return RefinementCheckpoint(**kwargs)


def _build_alignment_checkpoint(**overrides: Any) -> AlignmentCheckpoint:
    spec = _spec()
    code = "code"
    kwargs = dict(
        **_identity(),
        spec_hash=phases.hash_spec(spec),
        code_hash=phases.hash_code(code),
        spec=spec,
        rationale="because",
        code=code,
        alignment_rounds_completed=1,
        gate_timeline=[_gate_event()],
    )
    kwargs.update(overrides)
    return AlignmentCheckpoint(**kwargs)


_STAGE_CHECKPOINT_BUILDERS = [
    _build_design_checkpoint,
    _build_review_checkpoint,
    _build_synthesis_checkpoint,
    _build_refinement_checkpoint,
    _build_alignment_checkpoint,
]
_STAGE_CHECKPOINT_IDS = ["design", "review", "synthesis", "refinement", "alignment"]


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("build", _STAGE_CHECKPOINT_BUILDERS, ids=_STAGE_CHECKPOINT_IDS)
def test_checkpoint_is_frozen(build: Callable[..., PipelineCheckpoint]) -> None:
    """Every stage subclass is frozen: assigning the common ``stage`` field
    raises a ``ValidationError`` on each subclass (Pydantic's ``frozen=True``
    rejects rebinding any field, ``stage`` is just the one exercised here)."""
    cp = build()
    with pytest.raises(ValidationError):
        cp.stage = cp.stage


# ---------------------------------------------------------------------------
# Pinned-stage enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build,mismatched_stage",
    [
        (_build_design_checkpoint, PipelineStage.REVIEW),
        (_build_review_checkpoint, PipelineStage.DESIGN),
        (_build_synthesis_checkpoint, PipelineStage.ALIGNMENT),
        (_build_refinement_checkpoint, PipelineStage.SYNTHESIS),
        (_build_alignment_checkpoint, PipelineStage.REFINEMENT),
    ],
    ids=_STAGE_CHECKPOINT_IDS,
)
def test_mismatched_stage_rejected(build: Callable[..., PipelineCheckpoint], mismatched_stage: PipelineStage) -> None:
    with pytest.raises(ValidationError):
        build(stage=mismatched_stage)


def test_parse_checkpoint_rejects_stage_mismatched_payload() -> None:
    """A payload dispatched to ``SynthesisCheckpoint`` by its ``"stage"`` key
    but carrying a mismatched ``stage`` value inside itself is impossible to
    construct — the outer dispatch key and the inner field must agree."""
    raw = _build_synthesis_checkpoint().model_dump(mode="json")
    raw["stage"] = "alignment"
    with pytest.raises(ValidationError):
        parse_checkpoint(raw)


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("build", _STAGE_CHECKPOINT_BUILDERS, ids=_STAGE_CHECKPOINT_IDS)
def test_checkpoint_serialization_round_trip(build) -> None:
    original = build()
    raw = original.model_dump(mode="json")
    restored = parse_checkpoint(raw)
    assert restored == original
    assert type(restored) is type(original)


def test_parse_checkpoint_dispatches_by_stage_field() -> None:
    raw = _build_synthesis_checkpoint().model_dump(mode="json")
    restored = parse_checkpoint(raw)
    assert isinstance(restored, SynthesisCheckpoint)


# ---------------------------------------------------------------------------
# Hash wiring: spec_hash/code_hash line up with phases.hash_spec/hash_code
# ---------------------------------------------------------------------------


def test_spec_hash_matches_phases_hash_spec() -> None:
    cp = _build_design_checkpoint()
    assert cp.spec_hash == phases.hash_spec(cp.spec)


def test_code_hash_matches_phases_hash_code() -> None:
    cp = _build_synthesis_checkpoint()
    assert cp.code_hash == phases.hash_code(cp.code)


def test_code_hash_before_synthesis_is_empty_string_digest() -> None:
    """A design-stage checkpoint (no code yet) carries the empty-string
    SHA-256, matching ``phases.PhaseTransition``'s pre-synthesis convention."""
    cp = _build_design_checkpoint(code_hash=phases.hash_code(None))
    assert cp.code_hash == _EMPTY_CODE_HASH


# ---------------------------------------------------------------------------
# Precondition validation
# ---------------------------------------------------------------------------


def test_non_positive_generation_rejected() -> None:
    with pytest.raises(ValidationError):
        _build_design_checkpoint(**_identity(generation=0))


def test_negative_design_attempt_rejected() -> None:
    with pytest.raises(ValidationError):
        _build_design_checkpoint(**_identity(design_attempt=-1))


def test_empty_cycle_scope_rejected() -> None:
    with pytest.raises(ValidationError):
        _build_design_checkpoint(**_identity(cycle_scope=""))


def test_negative_budget_calls_rejected() -> None:
    with pytest.raises(ValidationError):
        _build_design_checkpoint(**_identity(budget_calls=-1))


def test_malformed_spec_hash_rejected() -> None:
    with pytest.raises(ValidationError):
        _build_design_checkpoint(spec_hash="too-short")


def test_malformed_code_hash_rejected() -> None:
    with pytest.raises(ValidationError):
        _build_design_checkpoint(code_hash="too-short")


@pytest.mark.parametrize(
    "build,field_name",
    [
        (_build_review_checkpoint, "review_rounds_completed"),
        (_build_refinement_checkpoint, "refinement_rounds_completed"),
        (_build_alignment_checkpoint, "alignment_rounds_completed"),
    ],
    ids=["review", "refinement", "alignment"],
)
def test_negative_rounds_completed_rejected(build: Callable[..., PipelineCheckpoint], field_name: str) -> None:
    with pytest.raises(ValidationError):
        build(**{field_name: -1})


def test_pipeline_checkpoint_base_class_still_constructible_directly() -> None:
    """The base class isn't meant to be used directly, but nothing prevents
    it mechanically (Pydantic has no abstract-model enforcement) — document
    that via a direct construction, matching its own docstring's caveat. Its
    unset ``_pinned_stage`` (``None``) also means the pinned-stage validator
    is a no-op here, unlike every concrete subclass."""
    spec = _spec()
    cp = PipelineCheckpoint(
        **_identity(),
        stage=PipelineStage.DESIGN,
        spec_hash=phases.hash_spec(spec),
        code_hash=_EMPTY_CODE_HASH,
    )
    assert cp.stage == PipelineStage.DESIGN
