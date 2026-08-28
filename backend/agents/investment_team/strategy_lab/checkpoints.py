"""Checkpoint/resume-state model family for same-attempt Strategy Lab crash recovery.

Defines one immutable checkpoint variant per pipeline stage boundary —
``DESIGN``, ``REVIEW``, ``SYNTHESIS``, ``REFINEMENT``, ``ALIGNMENT`` — capturing
what has converged at that boundary so a crash mid-attempt can resume from the
latest converged boundary instead of re-deriving upstream work from scratch
*within that same design attempt*. The shared orchestrator path now captures
instances at the five stage boundaries; consuming them to skip stages on
resume remains a separate, later piece of work.

This family generalizes, and is additive alongside, ``DesignAttemptCheckpoint``
(``..models``, documented by
``system_design/adr/ADR-012-strategy-lab-design-attempt-checkpoint-contract.md``).
``DesignAttemptCheckpoint`` checkpoints exactly one same-attempt boundary
(combined design+review output, handed to code synthesis) to let Temporal
resume the *same* design attempt after a worker crash mid-attempt. This
module's family checkpoints all five stage boundaries of that same
same-attempt scenario, so a crash during synthesis, refinement, or alignment
can also resume without redoing the stages already converged in the current
attempt. Nothing here changes ``DesignAttemptCheckpoint`` or its ADR.

**Same-attempt only — this is not a cross-attempt re-entry mechanism.** A
checkpoint captured for ``design_attempt=N`` is never read while running
``design_attempt=N+1``. When ``SpecImplementabilityError`` triggers a design
re-entry, the new attempt starts fresh with none of this family's
checkpoints consulted, exactly as ``RETRY_STATE_ISOLATION.md`` requires for
every other kind of attempt-local state: a failed attempt's mutations must
never leak into the next attempt's reasoning. Letting a *new* attempt resume
from a *prior* attempt's partial convergence would require an explicit
amendment to that isolation contract (to define which stages, if any, survive
the failure that caused re-entry) and is out of scope for this family.

Serialization relies entirely on Pydantic's built-in ``model_dump(mode="json")``
/ ``model_validate`` — the same mechanism already proven for
``DesignAttemptCheckpoint`` and ``phases.PhaseTransition`` to round-trip
cleanly through both thread-mode (in-process Python objects) and Temporal-mode
(JSON-serializable activity/workflow payloads).

**What determines this family's field set, stated once and generally (the
closing answer to any future "but doesn't a real resume also need field X"
finding — trade-alignment inputs, pre-refinement code snapshots, the
synthesis-time-mutated backtest config, or anything else in the same shape):
a field is added here only when an existing, already-established checkpoint
model in this codebase (``DesignAttemptCheckpoint``) already carries an
analogous field — ``spec``/``rationale``/``design_context``,
``spec_history``/``code_history``/``gate_timeline``, ``budget_calls``/
``gate_results``. Every one of those has been generalized to all five stages
across this model's review history. Fields with no such precedent — anything
that only exists as a local variable or a runtime side effect somewhere
inside ``_run_design_attempt``/``_synthesize_initial_code``/
``_run_trade_alignment_loop`` and has never before been captured into a
checkpoint DTO — remain deliberately outside this model family. The capture
implementation snapshots exactly this established contract; adding a new
field shape without a prior contract would still be guessing, not modeling.
This is not a gap to keep finding instances of; it is this family's stated
scope boundary.**
"""

from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models import CodeRevision, GateEvent, SpecRevision, StrategySpec


class PipelineStage(str, Enum):
    """The five named checkpointable boundaries of a Strategy Lab design attempt.

    Invariants:
      - Membership is exactly these five values, in the listed pipeline order.
      - A checkpoint at stage ``S`` implies every stage before ``S`` in this
        order has already converged for the same ``(run_id, cycle_scope,
        design_attempt)`` — this is what makes the family internally
        consistent for a consumer choosing which stage to resume from.
    """

    DESIGN = "design"
    REVIEW = "review"
    SYNTHESIS = "synthesis"
    REFINEMENT = "refinement"
    ALIGNMENT = "alignment"


PIPELINE_STAGES: tuple[PipelineStage, ...] = (
    PipelineStage.DESIGN,
    PipelineStage.REVIEW,
    PipelineStage.SYNTHESIS,
    PipelineStage.REFINEMENT,
    PipelineStage.ALIGNMENT,
)


class PipelineCheckpoint(BaseModel):
    """Common identity/versioning fields shared by every stage checkpoint variant.

    Not intended to be instantiated directly — always construct one of the
    five ``PipelineStage``-specific subclasses below, each of which pins its
    own ``stage`` value via a class-level default.

    Preconditions:
      - ``run_id``/``cycle_scope``/``design_attempt`` identify exactly the
        attempt this checkpoint was captured during, using the same
        disambiguation semantics as ``DesignAttemptCheckpoint``: ``cycle_scope``
        is opaque (never parsed for substrings), only ever compared for
        equality, and exists because a ``StrategyLabBatchWorkflow`` can run
        multiple concurrent cycles that would otherwise collide on the same
        ``(run_id, design_attempt)`` pair.
      - ``generation`` is the fencing generation active at capture time (see
        ``shared.fencing.check_fencing_token`` /
        ``strategy_lab.run_state.get_run_generation_strict``).
      - ``spec_hash`` / ``code_hash`` are the outputs of
        ``strategy_lab.phases.hash_spec`` / ``strategy_lab.phases.hash_code``
        computed from the same spec/code state the checkpoint's payload
        carries (or, before code exists, ``hash_code(None)``).
      - ``captured_at`` is an ISO-8601 / RFC-3339 UTC timestamp string,
        matching the ``timestamp: str`` convention already used by
        ``SpecRevision``/``CodeRevision``/``GateEvent``.
      - ``budget_calls`` is the cumulative LLM-call count as of the boundary
        (``LLMCallBudget.calls_made``), and ``gate_results`` is the
        cumulative quality-gate result list as of the boundary — both
        carried at every stage, not just design/synthesis, generalizing the
        same two fields ``DesignAttemptCheckpoint`` already persists for
        exactly the reason ADR-012 documents: resuming must seed the budget
        from *this* boundary's count, never from the pre-attempt count,
        or it silently reopens already-spent headroom (a false negative on
        the LLM-call ceiling, not a double charge, but the same practical
        failure mode). ``gate_results`` is typed as ``tuple[dict[str, Any],
        ...]`` rather than a typed gate-result model for the same
        circular-import reason ``DesignAttemptCheckpoint.gate_results``
        documents in ``models.py``.
      - ``spec_history``/``code_history``/``gate_timeline`` are likewise
        carried cumulatively at every stage, matching
        ``DesignAttemptCheckpoint``'s own shape exactly (it carries all
        three regardless of its own single boundary): record assembly
        persists all three histories together
        (``orchestrator_record_assembly.py``), so a checkpoint claiming to
        be a self-sufficient "latest boundary" snapshot must carry every
        history entry produced so far, not only the slice a later stage's
        own loop happens to append to.

    Postconditions:
      - Instances are immutable (``frozen=True``) snapshots: a checkpoint's
        own fields can never be rebound after construction, and its list-typed
        history/timeline fields are ``tuple``s (not ``list``s) so they can't
        be appended to in place either. This is a shallow guarantee, the same
        one every other frozen model in this codebase (``DesignAttemptCheckpoint``,
        ``phases.PhaseTransition``) provides: a caller that reaches into a
        nested, non-frozen field — ``design_context`` (a plain ``dict``) or
        the nested ``StrategySpec``/``SpecRevision``/``CodeRevision``/
        ``GateEvent`` payload objects, none of which are themselves frozen —
        can still mutate their contents in place. Deep-freezing those would
        mean changing models shared across the rest of the codebase, out of
        scope for this additive module; callers that need the stronger
        guarantee should treat a checkpoint's nested payload as read-only by
        convention, as the codebase already does for `DesignAttemptCheckpoint`'s
        identically-shaped ``design_context: Dict[str, Any]`` field.

    Invariants:
      - **Never cross-attempt.** A checkpoint captured while running
        ``design_attempt=N`` is never read or considered while running
        ``design_attempt=N+1`` — a design re-entry (``SpecImplementabilityError``)
        starts a fresh attempt with its own fresh state, exactly as
        ``RETRY_STATE_ISOLATION.md`` already requires for other attempt-local
        state. This family's resume scenario is strictly same-attempt crash
        recovery (see the module docstring); it is never a mechanism for a
        new attempt to reuse a prior attempt's partial convergence.
      - **Never survives a generation bump.** A checkpoint minted under an
        older fencing generation is stale the instant a restart mints a new
        one (``restart_strategy_lab_run``'s full-reset semantics) — the same
        rule ``DesignAttemptCheckpoint`` and ADR-012 already apply.
      - **Versioned against the spec/code it was captured from.** A consumer
        must treat a checkpoint as invalid for reuse the moment
        ``spec_hash``/``code_hash`` no longer match the current upstream
        spec/code state — this is the mechanism a later capture/consume step
        uses to detect that upstream state has drifted since capture. This
        model does not enforce the match at construction time (there is
        nothing to enforce against yet, since the spec/code fields the hash
        was computed from are themselves the checkpoint's own payload); the
        invariant governs how a *consumer* compares a checkpoint's hash
        against independently-known current state.
      - **A payload must match its dispatched subclass exactly, in both
        directions.** ``extra="forbid"`` (alongside ``frozen=True``) means a
        subclass rejects a payload carrying any field it doesn't declare —
        not just a missing required field. Without this, ``parse_checkpoint``
        dispatching a later-stage payload (e.g. an ``AlignmentCheckpoint``
        whose ``"stage"`` was corrupted to ``"synthesis"``) to an
        earlier-stage subclass would silently validate, discarding the
        stage-specific fields the earlier subclass doesn't recognize
        (``alignment_rounds_completed``) rather than raising — exactly the
        inverse mismatch direction ``_enforce_pinned_stage`` alone doesn't
        catch, since that validator only compares the (already-consistent)
        ``stage`` field's value, not the full field set.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    _pinned_stage: ClassVar[PipelineStage | None] = None

    run_id: str
    cycle_scope: str = Field(..., min_length=1)
    design_attempt: int = Field(..., ge=0)
    generation: int = Field(..., ge=1)
    stage: PipelineStage
    spec_hash: str = Field(..., min_length=64, max_length=64)
    code_hash: str = Field(..., min_length=64, max_length=64)
    captured_at: str
    budget_calls: int = Field(..., ge=0)
    gate_results: tuple[dict[str, Any], ...] = Field(default_factory=tuple)
    spec_history: tuple[SpecRevision, ...] = Field(default_factory=tuple)
    code_history: tuple[CodeRevision, ...] = Field(default_factory=tuple)
    gate_timeline: tuple[GateEvent, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _enforce_pinned_stage(self) -> "PipelineCheckpoint":
        """Reject a ``stage`` value other than the subclass's pinned one.

        A subclass's ``stage`` field default only supplies a value when the
        field is omitted — Pydantic does not otherwise restrict what's
        accepted. Without this validator, ``DesignCheckpoint(stage="review",
        ...)`` or ``DesignCheckpoint.model_validate({"stage": "review", ...})``
        would construct successfully despite the class's own documented
        "``stage`` is always ``PipelineStage.DESIGN``" postcondition.
        """
        expected = self._pinned_stage
        if expected is not None and self.stage != expected:
            raise ValueError(f"{type(self).__name__} requires stage={expected.value!r}, got {self.stage.value!r}")
        return self


class DesignCheckpoint(PipelineCheckpoint):
    """Checkpoint at the design-stage boundary: a candidate spec, not yet reviewed.

    Postconditions:
      - ``stage`` is always ``PipelineStage.DESIGN`` — enforced by
        ``PipelineCheckpoint._enforce_pinned_stage``.
    """

    _pinned_stage: ClassVar[PipelineStage] = PipelineStage.DESIGN

    stage: PipelineStage = PipelineStage.DESIGN
    spec: StrategySpec
    rationale: str
    design_context: dict[str, Any] = Field(default_factory=dict)


class ReviewCheckpoint(PipelineCheckpoint):
    """Checkpoint at the review-stage boundary: a spec that has converged through review.

    Postconditions:
      - ``stage`` is always ``PipelineStage.REVIEW`` — enforced by
        ``PipelineCheckpoint._enforce_pinned_stage``.
      - ``spec_history`` (inherited from ``PipelineCheckpoint``) records every
        design-phase spec revision leading to this converged spec, oldest first.
    """

    _pinned_stage: ClassVar[PipelineStage] = PipelineStage.REVIEW

    stage: PipelineStage = PipelineStage.REVIEW
    spec: StrategySpec
    rationale: str
    design_context: dict[str, Any] = Field(default_factory=dict)
    review_rounds_completed: int = Field(..., ge=0)


class SynthesisCheckpoint(PipelineCheckpoint):
    """Checkpoint at the synthesis-stage boundary: strategy code has been generated.

    ``spec``/``rationale``/``design_context`` are carried forward unchanged from
    the review boundary: the spec is frozen post-review (ADR-012's own
    documented invariant — see ``phases.PhaseTransition``), so this checkpoint
    is self-sufficient to resume every stage from synthesis onward without
    reaching back to an earlier checkpoint for the design-time artefacts later
    stages (refinement, alignment) also need.

    Postconditions:
      - ``stage`` is always ``PipelineStage.SYNTHESIS`` — enforced by
        ``PipelineCheckpoint._enforce_pinned_stage``.
      - ``code_history`` (inherited from ``PipelineCheckpoint``) records every
        code revision produced so far, oldest first.
    """

    _pinned_stage: ClassVar[PipelineStage] = PipelineStage.SYNTHESIS

    stage: PipelineStage = PipelineStage.SYNTHESIS
    spec: StrategySpec
    rationale: str
    design_context: dict[str, Any] = Field(default_factory=dict)
    code: str


class RefinementCheckpoint(PipelineCheckpoint):
    """Checkpoint at the refinement-stage boundary: code has converged through refinement.

    Carries ``spec``/``rationale``/``design_context`` forward from the design
    boundary for the same reason ``SynthesisCheckpoint`` does — see that
    class's docstring.

    Postconditions:
      - ``stage`` is always ``PipelineStage.REFINEMENT`` — enforced by
        ``PipelineCheckpoint._enforce_pinned_stage``.
      - ``code_history`` (inherited from ``PipelineCheckpoint``) records every
        code revision produced so far, oldest first.
    """

    _pinned_stage: ClassVar[PipelineStage] = PipelineStage.REFINEMENT

    stage: PipelineStage = PipelineStage.REFINEMENT
    spec: StrategySpec
    rationale: str
    design_context: dict[str, Any] = Field(default_factory=dict)
    code: str
    refinement_rounds_completed: int = Field(..., ge=0)


class AlignmentCheckpoint(PipelineCheckpoint):
    """Checkpoint at the alignment-stage boundary: trade-alignment audit has converged.

    Carries ``spec``/``rationale``/``design_context`` forward from the design
    boundary for the same reason ``SynthesisCheckpoint`` does — see that
    class's docstring.

    **Deliberately excluded, final position** (raised and answered twice
    already in this model's review history — this is the closing word, not
    an open question): trade-alignment-specific resume inputs that
    ``_run_trade_alignment_loop`` also consumes at runtime — executed trades,
    backtest metrics, market data, execution status, conformance state. None
    of these have any precedent anywhere in this codebase to generalize a
    field shape from (unlike ``spec_history``/``code_history``/``gate_timeline``/
    ``budget_calls``/``gate_results``, all of which mirror an existing,
    already-established ``DesignAttemptCheckpoint`` field). The capture path
    therefore snapshots the fields this model declares and does not invent
    representations for those runtime-only values. Any future consumer that
    proves one is resume-critical must first define its durable contract
    explicitly.

    Postconditions:
      - ``stage`` is always ``PipelineStage.ALIGNMENT`` — enforced by
        ``PipelineCheckpoint._enforce_pinned_stage``.
      - ``gate_timeline`` (inherited from ``PipelineCheckpoint``) records every
        quality-gate evaluation so far, oldest first.
    """

    _pinned_stage: ClassVar[PipelineStage] = PipelineStage.ALIGNMENT

    stage: PipelineStage = PipelineStage.ALIGNMENT
    spec: StrategySpec
    rationale: str
    design_context: dict[str, Any] = Field(default_factory=dict)
    code: str
    alignment_rounds_completed: int = Field(..., ge=0)


AnyPipelineCheckpoint = (
    DesignCheckpoint | ReviewCheckpoint | SynthesisCheckpoint | RefinementCheckpoint | AlignmentCheckpoint
)

_CHECKPOINT_CLASSES_BY_STAGE: dict[PipelineStage, type[PipelineCheckpoint]] = {
    PipelineStage.DESIGN: DesignCheckpoint,
    PipelineStage.REVIEW: ReviewCheckpoint,
    PipelineStage.SYNTHESIS: SynthesisCheckpoint,
    PipelineStage.REFINEMENT: RefinementCheckpoint,
    PipelineStage.ALIGNMENT: AlignmentCheckpoint,
}


def parse_checkpoint(raw: dict[str, Any]) -> AnyPipelineCheckpoint:
    """Deserialize a persisted/opaque checkpoint payload to its concrete stage subclass.

    Preconditions:
      - ``raw`` is a ``dict`` (e.g. from ``json.loads`` or a durable-store
        read) containing a ``"stage"`` key whose value is a valid
        ``PipelineStage`` member.
    Postconditions:
      - Returns an instance of the ``PipelineCheckpoint`` subclass matching
        ``raw["stage"]``, constructed via that subclass's own validation.
    Raises:
      - ``KeyError`` if ``raw`` has no ``"stage"`` key.
      - ``ValueError`` if ``raw["stage"]`` is not a valid ``PipelineStage`` member.
      - ``pydantic.ValidationError`` if the payload fails validation for the
        selected subclass (missing required fields, malformed hashes, a
        ``stage`` value that doesn't match the dispatched subclass, etc.) —
        the common failure mode for invalid persisted checkpoint data.
    """
    stage = PipelineStage(raw["stage"])
    checkpoint_cls = _CHECKPOINT_CLASSES_BY_STAGE[stage]
    return checkpoint_cls.model_validate(raw)
