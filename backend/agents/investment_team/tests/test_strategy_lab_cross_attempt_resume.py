"""Cross-attempt resume: ``run_cycle`` consuming a REVIEW or SYNTHESIS checkpoint.

Companion to ``test_strategy_lab_checkpoint_crash_resumption.py`` (same-attempt
Temporal crash recovery via ``ADR-012``'s ``DesignAttemptCheckpoint``) and
``test_strategy_lab_checkpoints.py`` (pure data-model coverage of the
``PipelineCheckpoint`` family and ``determine_resume_stage``/
``find_latest_checkpoint_for_attempt``). This file exercises the two narrow
cross-attempt exceptions documented in ``checkpoints.py`` and
``RETRY_STATE_ISOLATION.md``: when a failed design attempt's most-converged
checkpoint is a ``ReviewCheckpoint``, ``run_cycle``'s
``except SpecImplementabilityError`` branch resumes the next attempt straight
into synthesis via ``_run_design_attempt``'s existing ``resume_spec``/
``resume_rationale``/``resume_design_context`` parameters; when it is a
``SynthesisCheckpoint``, the next attempt additionally resumes straight into
refinement via ``resume_code`` -- the case that actually fires on real
phase-backs, since every current production ``SpecImplementabilityError``
raiser fires from inside the refinement loop, after synthesis has already
converged (see ``test_strategy_lab_refinement_freeze.py``'s
``test_run_cycle_reroutes_on_stray_key_threshold`` /
``test_run_cycle_reroutes_then_short_circuits_on_persistent_loosening`` for
the realistic, non-injected end-to-end regression coverage of that path).
Every other case (no checkpoint, a DESIGN-only checkpoint, or a malformed
payload) falls back to the pre-existing full-restart behavior.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from investment_team.models import BacktestConfig
from investment_team.strategy_lab import orchestrator as orchestrator_module
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


def test_resume_skips_design_review_when_review_checkpoint_converged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempt 0 converges DESIGN+REVIEW then fails at synthesis; attempt 1
    resumes straight into synthesis instead of re-running the design loop.

    Proven two ways: ``design_agent.run`` fires exactly once for the whole
    cycle (not once per attempt), and the emitted ``phase_transition``
    sequence shows attempt 1 starting at ``CODE_SYNTHESIS`` with no
    ``DESIGN``/``DESIGN_REVIEW`` boundaries for that attempt.
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

    # Phase 1 (DESIGN + REVIEW) ran for real exactly once -- attempt 1 never
    # re-invoked the design agent.
    assert design_run_calls["n"] == 1

    # The failed attempt's checkpoint converged through REVIEW, so the
    # resumed attempt is actionable.
    assert orch.last_resume_determination is PipelineStage.SYNTHESIS

    # Attempt 0 crosses DESIGN->DESIGN_REVIEW and DESIGN_REVIEW->CODE_SYNTHESIS
    # before failing; attempt 1 resumes straight into CODE_SYNTHESIS and
    # crosses only the two remaining boundaries.
    seq = [(t["from_phase"], t["to_phase"], t["attempt"]) for t in transitions]
    assert seq == [
        (Phase.DESIGN.value, Phase.DESIGN_REVIEW.value, 0),
        (Phase.DESIGN_REVIEW.value, Phase.CODE_SYNTHESIS.value, 0),
        (Phase.CODE_SYNTHESIS.value, Phase.BACKTEST_AND_VERIFICATION.value, 1),
        (Phase.BACKTEST_AND_VERIFICATION.value, None, 1),
    ]

    # Evidence-chain integrity: the resumed attempt's REVIEW checkpoint (its
    # own, re-captured on the resumed path) carries the same spec the failed
    # attempt's REVIEW checkpoint captured -- resume did not fabricate or
    # corrupt the spec it skipped re-deriving.
    review_checkpoints = [c for c in orch.pipeline_checkpoints if c.stage is PipelineStage.REVIEW]
    assert len(review_checkpoints) == 2
    assert review_checkpoints[0].design_attempt == 0
    assert review_checkpoints[1].design_attempt == 1
    assert review_checkpoints[1].spec.strategy_id == review_checkpoints[0].spec.strategy_id
    assert review_checkpoints[1].spec_hash == review_checkpoints[0].spec_hash

    # The record itself is well-formed -- resume did not corrupt record
    # assembly (same status family the non-resumed happy-path fixture
    # produces, since synthesis still short-circuits at no-market-data).
    assert record.backtest.status.startswith("failed")


def test_resume_falls_back_to_full_restart_when_no_checkpoint_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the failed attempt captured no checkpoint at all, every
    subsequent attempt still gets a full restart -- unchanged behavior.
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
    # resume_spec=None.
    assert orch.last_resume_determination is None
    assert resume_specs_seen == [None, None]
    assert record.backtest.status.startswith("failed")


def test_resume_fails_open_to_full_restart_on_malformed_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REVIEW checkpoint exists, but its design_context cannot be
    reconstructed -- resume must fail open, not crash the cycle.
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

    def _broken_reconstruction(_data: Dict[str, Any]) -> Any:
        raise ValueError("simulated malformed checkpoint payload")

    monkeypatch.setattr(
        orchestrator_module, "_design_context_from_checkpoint", _broken_reconstruction
    )

    # Must not raise -- fail-open to full restart.
    transitions, record = _drive_cycle(orch)

    # The determination was still actionable (REVIEW converged)...
    assert orch.last_resume_determination is PipelineStage.SYNTHESIS
    # ...but reconstruction failed, so attempt 1 fell back to a full restart:
    # the design agent ran again.
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


def test_resume_skips_design_review_and_synthesis_when_synthesis_checkpoint_converged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempt 0 converges through synthesis then fails downstream of it
    (the realistic shape: every production ``SpecImplementabilityError``
    raiser fires from inside the refinement loop); attempt 1 resumes
    straight into refinement, skipping DESIGN+REVIEW+CODE_SYNTHESIS
    entirely -- not just DESIGN+REVIEW.
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

    # Phase 1 (DESIGN + REVIEW) and Phase 1b (CODE SYNTHESIS) both ran for
    # real exactly once -- attempt 1 never re-invoked either.
    assert design_run_calls["n"] == 1
    assert synthesize_calls["n"] == 1

    # The failed attempt's checkpoint converged through SYNTHESIS, so the
    # resumed attempt skips synthesis too.
    assert orch.last_resume_determination is PipelineStage.REFINEMENT

    # Attempt 0 crosses all the way to CODE_SYNTHESIS before failing;
    # attempt 1 resumes straight past DESIGN+REVIEW+CODE_SYNTHESIS (no
    # DESIGN/DESIGN_REVIEW transitions fire for it) but the
    # CODE_SYNTHESIS -> BACKTEST_AND_VERIFICATION boundary still fires --
    # that transition is emitted by _orchestrate_refinement_and_alignment
    # itself (still called on resume), not by the synthesis step being
    # skipped.
    seq = [(t["from_phase"], t["to_phase"], t["attempt"]) for t in transitions]
    assert seq == [
        (Phase.DESIGN.value, Phase.DESIGN_REVIEW.value, 0),
        (Phase.DESIGN_REVIEW.value, Phase.CODE_SYNTHESIS.value, 0),
        (Phase.CODE_SYNTHESIS.value, Phase.BACKTEST_AND_VERIFICATION.value, 1),
        (Phase.BACKTEST_AND_VERIFICATION.value, None, 1),
    ]

    # Evidence-chain integrity: the resumed attempt's own SynthesisCheckpoint
    # carries the same spec and code the failed attempt's did -- resume did
    # not fabricate or corrupt the code it skipped re-deriving.
    synthesis_checkpoints = [
        c for c in orch.pipeline_checkpoints if c.stage is PipelineStage.SYNTHESIS
    ]
    assert len(synthesis_checkpoints) == 2
    assert synthesis_checkpoints[0].design_attempt == 0
    assert synthesis_checkpoints[1].design_attempt == 1
    assert synthesis_checkpoints[1].code == synthesis_checkpoints[0].code
    assert synthesis_checkpoints[1].code_hash == synthesis_checkpoints[0].code_hash
    assert synthesis_checkpoints[1].spec_hash == synthesis_checkpoints[0].spec_hash

    assert record.backtest.status.startswith("failed")


def test_run_design_attempt_rejects_resume_code_without_resume_spec() -> None:
    """``resume_code`` without ``resume_spec`` is an invalid combination --
    there is no boundary that skips synthesis but not design+review.
    """
    orch = StrategyLabOrchestrator()
    with pytest.raises(ValueError, match="resume_code requires resume_spec"):
        orch._run_design_attempt(
            prior_records=[],
            config=_config(),
            signal_briefs=None,
            emit=lambda *_a, **_kw: None,
            exclude_asset_classes=None,
            directives=[],
            resume_code="# some code",
        )
