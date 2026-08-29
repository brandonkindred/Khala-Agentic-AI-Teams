"""Cross-attempt resume determination is computed but not consumed.

Companion to ``test_strategy_lab_checkpoint_crash_resumption.py`` (same-attempt
Temporal crash recovery via ``ADR-012``'s ``DesignAttemptCheckpoint``) and
``test_strategy_lab_checkpoints.py`` (pure data-model coverage of the
``PipelineCheckpoint`` family and ``determine_resume_stage``/
``find_latest_checkpoint_for_attempt``). This file locks in that
``run_cycle``'s ``except SpecImplementabilityError`` branch correctly
*computes* ``self.last_resume_determination`` from the just-failed attempt's
captured checkpoints, for both a ``ReviewCheckpoint`` and a
``SynthesisCheckpoint``, but genuinely never acts on it: every subsequent
attempt still re-runs DESIGN+REVIEW(+CODE_SYNTHESIS) from scratch,
regardless of the determination.

A cross-attempt resume mechanism that *did* consume this determination was
implemented and reverted: every current production ``SpecImplementabilityError``
raise site downstream of a checkpoint exists specifically because the
checkpointed spec needs a design-level revision that refinement cannot make
on its own, so resuming with that same, unrevised spec either guarantees
(a deterministic downstream check) or makes likely (an LLM refinement
retry) the identical failure recurring on every re-entry -- burning the
whole re-entry budget with no chance of recovery, worse than the
full-restart behavior this file locks back in. See ``checkpoints.py``'s
module docstring ("cross-attempt amendment was attempted once") for the
full analysis.
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
