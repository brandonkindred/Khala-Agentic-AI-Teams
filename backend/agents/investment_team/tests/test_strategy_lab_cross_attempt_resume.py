"""Cross-attempt resume determination is computed, and consumed only when
the raising exception declares ``spec_implicated=False``.

Companion to ``test_strategy_lab_checkpoint_crash_resumption.py`` (same-attempt
Temporal crash recovery via ``ADR-012``'s ``DesignAttemptCheckpoint``) and
``test_strategy_lab_checkpoints.py`` (pure data-model coverage of the
``PipelineCheckpoint`` family and ``determine_resume_stage``/
``find_latest_checkpoint_for_attempt``). This file locks in two things:

1. ``run_cycle``'s ``except SpecImplementabilityError`` branch correctly
   *computes* ``self.last_resume_determination`` from the just-failed
   attempt's captured checkpoints, for both a ``ReviewCheckpoint`` and a
   ``SynthesisCheckpoint`` -- and, when the raising exception's
   ``spec_implicated`` is ``True`` (every current production raise site),
   never acts on it: every subsequent attempt still re-runs
   DESIGN+REVIEW(+CODE_SYNTHESIS) from scratch, regardless of the
   determination.
2. When a raise site instead declares ``spec_implicated=False`` (no
   production site does today -- this is exercised via a directly
   constructed exception, standing in for a future site that has proven
   its failure doesn't implicate the checkpointed spec) AND the
   determination is ``PipelineStage.SYNTHESIS`` (checkpoint converged
   through REVIEW), the next attempt *does* resume from that checkpoint's
   spec/design_context, skipping the now-redundant re-derivation -- and
   carries the checkpoint's own spec/code/gate history into the resumed
   attempt's drift collector, so the final record's provenance still shows
   how the reused spec was actually derived. A checkpoint that converged
   through SYNTHESIS is never used to resume (even with
   ``spec_implicated=False``): that would additionally reuse the
   checkpoint's *code*, and a spec-not-implicated exception makes no claim
   about the code's soundness -- see ``checkpoints.py``.

An unconditional version of (2) -- resuming regardless of what the raising
exception said -- was implemented and reverted: every current production
``SpecImplementabilityError`` raise site downstream of a checkpoint exists
specifically because the checkpointed spec needs a design-level revision
that refinement cannot make on its own, so resuming with that same,
unrevised spec either guarantees (a deterministic downstream check) or
makes likely (an LLM refinement retry) the identical failure recurring on
every re-entry -- burning the whole re-entry budget with no chance of
recovery, worse than the full-restart behavior this file locks in for
every real (``spec_implicated=True``) raise site. See ``checkpoints.py``'s
module docstring ("cross-attempt amendment was attempted once") for the
full analysis, and ``exceptions.py`` for the ``spec_implicated`` contract.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from investment_team.models import BacktestConfig
from investment_team.strategy_lab.checkpoints import PipelineStage
from investment_team.strategy_lab.exceptions import SpecImplementabilityError
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.phases import PHASE_TRANSITION_EVENT_NAME, Phase

from .strategy_lab_reentry_fixtures import (
    REENTRY_REVIEW_NOT_READY_ROUNDS,
    _review_loop_stubs,
    synthesis_boundary_spec_implementability_error,
)
from .test_strategy_lab_phase_transitions import (
    _spec_dict,
    _stub_pipeline_for_happy_path,
)

pytestmark = pytest.mark.strategy_lab_integration


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )


def _drive_cycle(orch: StrategyLabOrchestrator) -> tuple[List[Dict[str, Any]], Any]:
    events: List[Dict[str, Any]] = []
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        on_phase=lambda phase, data: events.append((phase, data)),
    )
    transitions = [data for phase, data in events if phase == PHASE_TRANSITION_EVENT_NAME]
    return transitions, record


def _wrap_with_call_counter(monkeypatch: pytest.MonkeyPatch, obj: Any, attr: str) -> Dict[str, int]:
    """Monkeypatch ``obj.attr`` with a call-counting wrapper around itself."""
    counts = {"n": 0}
    original = getattr(obj, attr)

    def _counting(*args: Any, **kwargs: Any) -> Any:
        counts["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(obj, attr, _counting)
    return counts


def test_determination_computed_at_review_checkpoint_but_full_restart_still_happens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempt 0 converges DESIGN+REVIEW then fails at synthesis --
    ``last_resume_determination`` correctly reflects ``SYNTHESIS`` (the
    checkpoint converged through REVIEW), but attempt 1 still re-runs
    DESIGN+REVIEW+CODE_SYNTHESIS+... from scratch: the full four-boundary
    transition sequence fires again, and the design agent runs once per
    attempt, not once for the whole cycle.
    """
    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)
    design_run_calls = _wrap_with_call_counter(monkeypatch, orch.design_agent, "run")

    real_synthesize = orch._synthesize_initial_code

    def _synthesize_raise_on_first_attempt(**kwargs: Any) -> Any:
        if kwargs["design_attempt"] == 0:
            raise SpecImplementabilityError(
                "forced fail at synthesis boundary",
                failure_phase="synthesis",
                last_spec=kwargs["spec"],
                last_code="",
            )
        return real_synthesize(**kwargs)

    monkeypatch.setattr(orch, "_synthesize_initial_code", _synthesize_raise_on_first_attempt)

    transitions, record = _drive_cycle(orch)

    # The determination is computed correctly...
    assert orch.last_resume_determination is PipelineStage.SYNTHESIS
    # ...but nothing consumes it: the design agent ran once per attempt (not
    # once for the whole cycle), and attempt 1 re-fires the full transition
    # sequence from DESIGN, exactly like attempt 0 did.
    assert design_run_calls["n"] == 2
    seq = [(t["from_phase"], t["to_phase"], t["attempt"]) for t in transitions]
    assert seq == [
        (Phase.DESIGN.value, Phase.DESIGN_REVIEW.value, 0),
        (Phase.DESIGN_REVIEW.value, Phase.CODE_SYNTHESIS.value, 0),
        (Phase.DESIGN.value, Phase.DESIGN_REVIEW.value, 1),
        (Phase.DESIGN_REVIEW.value, Phase.CODE_SYNTHESIS.value, 1),
        (Phase.CODE_SYNTHESIS.value, Phase.BACKTEST_AND_VERIFICATION.value, 1),
        (Phase.BACKTEST_AND_VERIFICATION.value, None, 1),
    ]
    assert record.backtest.status.startswith("failed")


def test_determination_computed_at_synthesis_checkpoint_but_full_restart_still_happens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempt 0 converges through synthesis then fails downstream of it
    (the realistic shape: every production ``SpecImplementabilityError``
    raiser fires from inside the refinement loop) -- ``last_resume_determination``
    correctly reflects ``REFINEMENT`` (the checkpoint converged through
    SYNTHESIS), but attempt 1 still re-runs DESIGN+REVIEW+CODE_SYNTHESIS
    from scratch, same as the REVIEW-checkpoint case above.
    """
    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)
    design_run_calls = _wrap_with_call_counter(monkeypatch, orch.design_agent, "run")
    synthesize_calls = _wrap_with_call_counter(monkeypatch, orch, "_synthesize_initial_code")

    real_refine_align = orch._orchestrate_refinement_and_alignment

    def _refine_align_raise_on_first_attempt(**kwargs: Any) -> Any:
        if kwargs["design_attempt"] == 0:
            raise SpecImplementabilityError(
                "forced fail at refinement boundary",
                failure_phase="refinement",
                last_spec=kwargs["spec"],
                last_code=kwargs["code"],
            )
        return real_refine_align(**kwargs)

    monkeypatch.setattr(
        orch, "_orchestrate_refinement_and_alignment", _refine_align_raise_on_first_attempt
    )

    transitions, record = _drive_cycle(orch)

    assert orch.last_resume_determination is PipelineStage.REFINEMENT
    # Design and synthesis both ran once per attempt -- no skip happened.
    assert design_run_calls["n"] == 2
    assert synthesize_calls["n"] == 2
    seq = [(t["from_phase"], t["to_phase"], t["attempt"]) for t in transitions]
    assert seq == [
        (Phase.DESIGN.value, Phase.DESIGN_REVIEW.value, 0),
        (Phase.DESIGN_REVIEW.value, Phase.CODE_SYNTHESIS.value, 0),
        (Phase.DESIGN.value, Phase.DESIGN_REVIEW.value, 1),
        (Phase.DESIGN_REVIEW.value, Phase.CODE_SYNTHESIS.value, 1),
        (Phase.CODE_SYNTHESIS.value, Phase.BACKTEST_AND_VERIFICATION.value, 1),
        (Phase.BACKTEST_AND_VERIFICATION.value, None, 1),
    ]
    assert record.backtest.status.startswith("failed")


def test_determination_is_none_when_no_checkpoint_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the failed attempt captured no checkpoint at all, the
    determination is ``None`` ("no usable checkpoint") -- and, as always,
    every subsequent attempt gets a full restart.
    """
    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)

    real_run_design_attempt = orch._run_design_attempt
    resume_specs_seen: List[Any] = []

    def _flaky_run_design_attempt(**kwargs: Any) -> Any:
        resume_specs_seen.append(kwargs.get("resume_spec"))
        attempt = kwargs["design_attempt"]
        if attempt == 0:
            raise SpecImplementabilityError(
                "forced fail attempt 0, no checkpoint captured",
                failure_phase="refinement",
                last_spec=orch._build_spec_from_dict(_spec_dict(), strategy_id="s-0"),
                last_code="",
            )
        return real_run_design_attempt(**kwargs)

    monkeypatch.setattr(orch, "_run_design_attempt", _flaky_run_design_attempt)

    _transitions, record = _drive_cycle(orch)

    # No checkpoint was ever captured for attempt 0 (the fake raiser never
    # calls into the real pipeline), so the determination is "no usable
    # checkpoint" and every call -- including attempt 1's -- receives
    # resume_spec=None (there is no resume parameter passed at all).
    assert orch.last_resume_determination is None
    assert resume_specs_seen == [None, None]
    assert record.backtest.status.startswith("failed")


def test_spec_implicated_false_resumes_from_review_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the raising exception declares ``spec_implicated=False``, and the
    just-failed attempt's checkpoint converged through REVIEW, the next
    attempt resumes into synthesis instead of re-running DESIGN+REVIEW: the
    design agent runs only once for the whole cycle, only two transition
    boundaries fire for attempt 1 (CODE_SYNTHESIS onward, not the full
    four), and the resumed attempt's final record carries the checkpoint's
    own spec-revision history forward (Codex review finding: a resumed
    attempt's drift collector must not silently drop the provenance of the
    design work it's reusing).

    The design loop takes one real revision round before converging (unlike
    the shared happy-path stub's immediate ready=True) specifically so the
    checkpoint's ``spec_history`` is non-empty -- otherwise the seeding
    assertion below would pass trivially whether or not seeding actually
    happened.
    """
    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)
    design_run_calls = _wrap_with_call_counter(monkeypatch, orch.design_agent, "run")

    _review_loop_stubs(monkeypatch, orch, review_not_ready_rounds=REENTRY_REVIEW_NOT_READY_ROUNDS)

    real_synthesize = orch._synthesize_initial_code

    def _synthesize_raise_on_first_attempt(**kwargs: Any) -> Any:
        if kwargs["design_attempt"] == 0:
            raise synthesis_boundary_spec_implementability_error(
                spec=kwargs["spec"], spec_implicated=False
            )
        return real_synthesize(**kwargs)

    monkeypatch.setattr(orch, "_synthesize_initial_code", _synthesize_raise_on_first_attempt)

    transitions, record = _drive_cycle(orch)

    assert orch.last_resume_determination is PipelineStage.SYNTHESIS
    # The design agent ran once for the whole cycle -- attempt 1 resumed
    # past DESIGN+REVIEW instead of re-deriving them.
    assert design_run_calls["n"] == 1
    seq = [(t["from_phase"], t["to_phase"], t["attempt"]) for t in transitions]
    assert seq == [
        (Phase.DESIGN.value, Phase.DESIGN_REVIEW.value, 0),
        (Phase.DESIGN_REVIEW.value, Phase.CODE_SYNTHESIS.value, 0),
        (Phase.CODE_SYNTHESIS.value, Phase.BACKTEST_AND_VERIFICATION.value, 1),
        (Phase.BACKTEST_AND_VERIFICATION.value, None, 1),
    ]
    assert record.backtest.status.startswith("failed")

    # The checkpoint attempt 0 converged with (through REVIEW) carries the
    # one revision round above -- confirm it's non-empty so the prefix-match
    # assertion below is a real regression guard, not a vacuous one.
    review_checkpoints = [
        cp
        for cp in orch.pipeline_checkpoints
        if cp.design_attempt == 0 and cp.stage is PipelineStage.REVIEW
    ]
    assert len(review_checkpoints) == 1
    checkpoint = review_checkpoints[0]
    assert len(checkpoint.spec_history) == 1

    # The resumed attempt's final record carries that checkpointed history
    # forward as a prefix -- the design work that produced the reused spec
    # is not silently dropped from the persisted evidence chain.
    assert list(record.spec_history[: len(checkpoint.spec_history)]) == list(
        checkpoint.spec_history
    )
    assert list(record.code_history[: len(checkpoint.code_history)]) == list(
        checkpoint.code_history
    )


def test_repeated_resume_does_not_duplicate_seeded_drift_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review finding: merging a resumed attempt's *whole* drift
    collector on failure -- including the checkpoint history seeded into it
    at the top of the loop -- would re-add that same history to the parent
    commit log every time a resumed attempt goes on to fail itself, since
    the parent already received it once when the checkpoint-producing
    attempt originally failed. Left unfixed, exhausting the re-entry budget
    after repeated resumes would persist a short-circuit record whose
    ``spec_history`` shows the same design-phase revision duplicated once
    per resumed failure.

    Drives all ``MAX_DESIGN_REENTRIES + 1`` attempts to fail: attempt 0
    fails not-spec-implicated at the synthesis boundary (after one real
    design-revision round, so its checkpoint's ``spec_history`` is
    non-empty), and every subsequent resumed attempt fails the same way,
    re-triggering resume from its own (re-captured, identical) checkpoint
    each time. The exhaustion short-circuit record is built from the parent
    drift collector -- the exact path the duplication would surface on.
    """
    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)

    _review_loop_stubs(monkeypatch, orch, review_not_ready_rounds=REENTRY_REVIEW_NOT_READY_ROUNDS)
    review_call_count = _wrap_with_call_counter(monkeypatch, orch.design_review_agent, "run")

    def _always_raise_not_spec_implicated(**kwargs: Any) -> Any:
        raise synthesis_boundary_spec_implementability_error(
            spec=kwargs["spec"], spec_implicated=False
        )

    monkeypatch.setattr(orch, "_synthesize_initial_code", _always_raise_not_spec_implicated)

    _transitions, record = _drive_cycle(orch)

    # Every attempt failed, so the cycle exhausted its re-entry budget --
    # the returned record is the short-circuit record built from the
    # parent drift collector.
    assert record.backtest.status == "failed: spec_unimplementable"
    # Exactly one design-revision round happened, ever (attempt 0's Phase 1
    # -- every later attempt resumed past it). If a resumed attempt's
    # failure re-merged its seeded history into the parent, this would be
    # ``MAX_DESIGN_REENTRIES + 1`` instead of ``1``.
    assert review_call_count["n"] == 2  # one not-ready round, one ready round
    assert len(record.spec_history) == 1


def test_non_spec_implicated_failure_does_not_mislabel_convergence_directive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review finding: appending ``PREVIOUS SPEC UNIMPLEMENTABLE:
    {evidence}`` to ``directives`` unconditionally -- even for a
    ``spec_implicated=False`` failure -- would mislabel evidence that was
    never about the spec's soundness. If a later attempt falls back to a
    full restart (e.g. because a subsequent resumed attempt goes on to fail
    with ``spec_implicated=True``), that mislabeled directive would steer
    the design agent to needlessly revise a spec that was never at fault.

    Drives attempt 0 to fail not-spec-implicated (triggering resume for
    attempt 1), then attempt 1 (resumed) to fail *spec-implicated*
    (triggering a full restart for attempt 2), then lets attempt 2 run for
    real. Captures the ``directives`` list ``_run_design_attempt`` actually
    receives on each call: attempt 2's should carry only attempt 1's
    (correctly labeled) directive, never attempt 0's.
    """
    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)

    _review_loop_stubs(monkeypatch, orch, review_not_ready_rounds=REENTRY_REVIEW_NOT_READY_ROUNDS)

    real_synthesize = orch._synthesize_initial_code

    def _synthesize_raise_on_first_two_attempts(**kwargs: Any) -> Any:
        design_attempt = kwargs["design_attempt"]
        if design_attempt == 0:
            raise SpecImplementabilityError(
                "attempt-0 evidence, not spec-implicated",
                failure_phase="synthesis",
                last_spec=kwargs["spec"],
                last_code="",
                spec_implicated=False,
            )
        if design_attempt == 1:
            raise SpecImplementabilityError(
                "attempt-1 evidence, spec-implicated",
                failure_phase="synthesis",
                last_spec=kwargs["spec"],
                last_code="",
                spec_implicated=True,
            )
        return real_synthesize(**kwargs)

    monkeypatch.setattr(orch, "_synthesize_initial_code", _synthesize_raise_on_first_two_attempts)

    real_run_design_attempt = orch._run_design_attempt
    directives_seen: List[List[str]] = []

    def _tracking_run_design_attempt(**kwargs: Any) -> Any:
        directives_seen.append(list(kwargs["directives"]))
        return real_run_design_attempt(**kwargs)

    monkeypatch.setattr(orch, "_run_design_attempt", _tracking_run_design_attempt)

    _transitions, record = _drive_cycle(orch)

    assert len(directives_seen) == 3
    # Attempt 0: no prior failure yet.
    assert directives_seen[0] == []
    # Attempt 1 (resumed past attempt 0's not-spec-implicated failure): no
    # directive was added for it.
    assert directives_seen[1] == []
    # Attempt 2 (full restart after attempt 1's spec-implicated failure):
    # exactly one directive, for attempt 1's evidence -- never attempt 0's.
    assert directives_seen[2] == [
        "PREVIOUS SPEC UNIMPLEMENTABLE: attempt-1 evidence, spec-implicated"
    ]
    assert record.backtest.status.startswith("failed")
