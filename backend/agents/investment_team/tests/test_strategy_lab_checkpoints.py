"""Tests for the checkpoint/resume-state model family in
``strategy_lab.checkpoints``: construction, immutability, serialization
round-trip, hash wiring, precondition validation, and stage ordering.

This is pure data-model coverage — nothing here exercises capture points or
consumption, since neither exists yet (see the module docstring in
``strategy_lab/checkpoints.py``).
"""

from __future__ import annotations

from typing import Any, Dict

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
        code=code,
        code_history=[_code_revision()],
    )
    assert cp.stage == PipelineStage.SYNTHESIS
    assert cp.code == code


def test_refinement_checkpoint_construction() -> None:
    spec = _spec()
    code = "def run(): return 1"
    cp = RefinementCheckpoint(
        **_identity(),
        spec_hash=phases.hash_spec(spec),
        code_hash=phases.hash_code(code),
        code=code,
        code_history=[_code_revision()],
        refinement_rounds_completed=1,
    )
    assert cp.stage == PipelineStage.REFINEMENT
    assert cp.refinement_rounds_completed == 1


def test_alignment_checkpoint_construction() -> None:
    spec = _spec()
    code = "def run(): return 2"
    cp = AlignmentCheckpoint(
        **_identity(),
        spec_hash=phases.hash_spec(spec),
        code_hash=phases.hash_code(code),
        code=code,
        alignment_rounds_completed=3,
        gate_timeline=[_gate_event()],
    )
    assert cp.stage == PipelineStage.ALIGNMENT
    assert cp.alignment_rounds_completed == 3
    assert len(cp.gate_timeline) == 1


# ---------------------------------------------------------------------------
# Shared per-subclass builders (reused by both the immutability and the
# serialization round-trip parametrizations below, one entry per stage).
# ---------------------------------------------------------------------------

_STAGE_CHECKPOINT_BUILDERS = [
    lambda: DesignCheckpoint(
        **_identity(),
        spec_hash=phases.hash_spec(_spec()),
        code_hash=_EMPTY_CODE_HASH,
        spec=_spec(),
        rationale="because",
    ),
    lambda: ReviewCheckpoint(
        **_identity(),
        spec_hash=phases.hash_spec(_spec()),
        code_hash=_EMPTY_CODE_HASH,
        spec=_spec(),
        rationale="because",
        spec_history=[_spec_revision()],
        review_rounds_completed=1,
    ),
    lambda: SynthesisCheckpoint(
        **_identity(),
        spec_hash=phases.hash_spec(_spec()),
        code_hash=phases.hash_code("code"),
        code="code",
        code_history=[_code_revision()],
    ),
    lambda: RefinementCheckpoint(
        **_identity(),
        spec_hash=phases.hash_spec(_spec()),
        code_hash=phases.hash_code("code"),
        code="code",
        code_history=[_code_revision()],
        refinement_rounds_completed=1,
    ),
    lambda: AlignmentCheckpoint(
        **_identity(),
        spec_hash=phases.hash_spec(_spec()),
        code_hash=phases.hash_code("code"),
        code="code",
        alignment_rounds_completed=1,
        gate_timeline=[_gate_event()],
    ),
]
_STAGE_CHECKPOINT_IDS = ["design", "review", "synthesis", "refinement", "alignment"]


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("build", _STAGE_CHECKPOINT_BUILDERS, ids=_STAGE_CHECKPOINT_IDS)
def test_checkpoint_is_frozen(build) -> None:
    """Every stage subclass is frozen, not just ``DesignCheckpoint``: attempting
    to set any field — including one common to all subclasses (``stage``) —
    raises on every one of them."""
    cp = build()
    with pytest.raises(ValidationError):
        cp.stage = cp.stage


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
    raw = {
        **_identity(),
        "stage": "synthesis",
        "spec_hash": phases.hash_spec(_spec()),
        "code_hash": phases.hash_code("x"),
        "code": "x",
    }
    restored = parse_checkpoint(raw)
    assert isinstance(restored, SynthesisCheckpoint)


# ---------------------------------------------------------------------------
# Hash wiring: spec_hash/code_hash line up with phases.hash_spec/hash_code
# ---------------------------------------------------------------------------


def test_spec_hash_matches_phases_hash_spec() -> None:
    spec = _spec()
    cp = DesignCheckpoint(
        **_identity(),
        spec_hash=phases.hash_spec(spec),
        code_hash=_EMPTY_CODE_HASH,
        spec=spec,
        rationale="because",
    )
    assert cp.spec_hash == phases.hash_spec(cp.spec)


def test_code_hash_matches_phases_hash_code() -> None:
    code = "def run(): return 42"
    spec = _spec()
    cp = SynthesisCheckpoint(
        **_identity(),
        spec_hash=phases.hash_spec(spec),
        code_hash=phases.hash_code(code),
        code=code,
    )
    assert cp.code_hash == phases.hash_code(cp.code)


def test_code_hash_before_synthesis_is_empty_string_digest() -> None:
    """A design-stage checkpoint (no code yet) carries the empty-string
    SHA-256, matching ``phases.PhaseTransition``'s pre-synthesis convention."""
    spec = _spec()
    cp = DesignCheckpoint(
        **_identity(),
        spec_hash=phases.hash_spec(spec),
        code_hash=phases.hash_code(None),
        spec=spec,
        rationale="because",
    )
    assert cp.code_hash == _EMPTY_CODE_HASH


# ---------------------------------------------------------------------------
# Precondition validation
# ---------------------------------------------------------------------------


def test_non_positive_generation_rejected() -> None:
    spec = _spec()
    with pytest.raises(ValidationError):
        DesignCheckpoint(
            **_identity(generation=0),
            spec_hash=phases.hash_spec(spec),
            code_hash=_EMPTY_CODE_HASH,
            spec=spec,
            rationale="because",
        )


def test_negative_design_attempt_rejected() -> None:
    spec = _spec()
    with pytest.raises(ValidationError):
        DesignCheckpoint(
            **_identity(design_attempt=-1),
            spec_hash=phases.hash_spec(spec),
            code_hash=_EMPTY_CODE_HASH,
            spec=spec,
            rationale="because",
        )


def test_empty_cycle_scope_rejected() -> None:
    spec = _spec()
    with pytest.raises(ValidationError):
        DesignCheckpoint(
            **_identity(cycle_scope=""),
            spec_hash=phases.hash_spec(spec),
            code_hash=_EMPTY_CODE_HASH,
            spec=spec,
            rationale="because",
        )


def test_malformed_spec_hash_rejected() -> None:
    spec = _spec()
    with pytest.raises(ValidationError):
        DesignCheckpoint(
            **_identity(),
            spec_hash="too-short",
            code_hash=_EMPTY_CODE_HASH,
            spec=spec,
            rationale="because",
        )


def test_negative_rounds_completed_rejected() -> None:
    spec = _spec()
    with pytest.raises(ValidationError):
        ReviewCheckpoint(
            **_identity(),
            spec_hash=phases.hash_spec(spec),
            code_hash=_EMPTY_CODE_HASH,
            spec=spec,
            rationale="because",
            review_rounds_completed=-1,
        )


def test_pipeline_checkpoint_base_class_still_constructible_directly() -> None:
    """The base class isn't meant to be used directly, but nothing prevents
    it mechanically (Pydantic has no abstract-model enforcement) — document
    that via a direct construction, matching its own docstring's caveat."""
    spec = _spec()
    cp = PipelineCheckpoint(
        **_identity(),
        stage=PipelineStage.DESIGN,
        spec_hash=phases.hash_spec(spec),
        code_hash=_EMPTY_CODE_HASH,
    )
    assert cp.stage == PipelineStage.DESIGN
