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
   its failure doesn't implicate the checkpointed spec), the next attempt
   *does* resume from that checkpoint's spec/design_context (and code, for
   a ``SynthesisCheckpoint``), skipping the now-redundant re-derivation.

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

from .test_strategy_lab_phase_transitions import (
    _VALID_CODE,
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
    design agent runs only once for the whole cycle, and only two transition
    boundaries fire for attempt 1 (CODE_SYNTHESIS onward), not the full four.
    """
    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)
    design_run_calls = _wrap_with_call_counter(monkeypatch, orch.design_agent, "run")

    real_synthesize = orch._synthesize_initial_code

    def _synthesize_raise_on_first_attempt(**kwargs: Any) -> Any:
        if kwargs["design_attempt"] == 0:
            raise SpecImplementabilityError(
                "forced fail at synthesis boundary, not spec-implicated",
                failure_phase="synthesis",
                last_spec=kwargs["spec"],
                last_code="",
                spec_implicated=False,
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


def test_spec_implicated_false_resumes_from_synthesis_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the raising exception declares ``spec_implicated=False``, and the
    just-failed attempt's checkpoint converged through SYNTHESIS (the
    realistic shape -- every production raiser fires from inside the
    refinement loop), the next attempt resumes into refinement, skipping
    both DESIGN+REVIEW and CODE_SYNTHESIS: neither the design agent nor
    ``_synthesize_initial_code`` runs a second time.
    """
    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)
    design_run_calls = _wrap_with_call_counter(monkeypatch, orch.design_agent, "run")
    synthesize_calls = _wrap_with_call_counter(monkeypatch, orch, "_synthesize_initial_code")

    real_refine_align = orch._orchestrate_refinement_and_alignment
    resume_codes_seen: List[Any] = []
    real_run_design_attempt = orch._run_design_attempt

    def _tracking_run_design_attempt(**kwargs: Any) -> Any:
        resume_codes_seen.append(kwargs.get("resume_code"))
        return real_run_design_attempt(**kwargs)

    monkeypatch.setattr(orch, "_run_design_attempt", _tracking_run_design_attempt)

    def _refine_align_raise_on_first_attempt(**kwargs: Any) -> Any:
        if kwargs["design_attempt"] == 0:
            raise SpecImplementabilityError(
                "forced fail at refinement boundary, not spec-implicated",
                failure_phase="refinement",
                last_spec=kwargs["spec"],
                last_code=kwargs["code"],
                spec_implicated=False,
            )
        return real_refine_align(**kwargs)

    monkeypatch.setattr(
        orch, "_orchestrate_refinement_and_alignment", _refine_align_raise_on_first_attempt
    )

    transitions, record = _drive_cycle(orch)

    assert orch.last_resume_determination is PipelineStage.REFINEMENT
    # Neither design nor synthesis ran a second time -- attempt 1 resumed
    # past both boundaries using the checkpointed spec/design_context/code.
    assert design_run_calls["n"] == 1
    assert synthesize_calls["n"] == 1
    assert resume_codes_seen == [None, _VALID_CODE]
    seq = [(t["from_phase"], t["to_phase"], t["attempt"]) for t in transitions]
    assert seq == [
        (Phase.DESIGN.value, Phase.DESIGN_REVIEW.value, 0),
        (Phase.DESIGN_REVIEW.value, Phase.CODE_SYNTHESIS.value, 0),
        (Phase.CODE_SYNTHESIS.value, Phase.BACKTEST_AND_VERIFICATION.value, 1),
        (Phase.BACKTEST_AND_VERIFICATION.value, None, 1),
    ]
    assert record.backtest.status.startswith("failed")
