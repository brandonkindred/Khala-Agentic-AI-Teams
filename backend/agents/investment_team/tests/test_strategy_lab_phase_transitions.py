"""Integration tests for the four-phase ``PhaseTransition`` contract.

These tests drive a real ``StrategyLabOrchestrator`` through
``run_cycle`` with collaborators stubbed, capture the emitted
``phase_transition`` events, and assert:

* On the happy path, exactly four transitions fire in the order
  ``DESIGN → DESIGN_REVIEW → CODE_SYNTHESIS → BACKTEST_AND_VERIFICATION → ∅``.
* The ``spec_hash`` is stable across every transition emitted after
  the design phase exits, **for these happy-path fixtures**. This is not
  a universal invariant: ``PhaseTransition`` documents three carve-outs
  (zero-trade repair committing ``risk_limits``, a tighten-only
  refinement merge, and a ``requires_custom_code`` flip on compiler
  fallback) that none of these stubs exercise. A fixture exercising one
  of them would legitimately produce a different hash — extend rather
  than "fix" the stability assertion if you add such a case.
* The ``code_hash`` recorded at the synthesis exit boundary matches the
  SHA-256 of the synthesised code string.
* Critical ``SpecReadinessGate`` failure blocks the ``DESIGN_REVIEW →
  CODE_SYNTHESIS`` transition entirely.
* Critical ``CodeConformanceGate`` failure prevents the verification
  phase from running any real verification work, even though the
  observability transition still fires.
* ``SpecImplementabilityError`` re-entry increments the transition
  ``attempt`` counter so each design attempt is independently observable.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest

from investment_team.models import BacktestConfig
from investment_team.strategy_lab import orchestrator as orchestrator_module
from investment_team.strategy_lab.agents.design_review import SpecCritique
from investment_team.strategy_lab.checkpoints import PipelineStage
from investment_team.strategy_lab.exceptions import SpecImplementabilityError
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.phases import (
    PHASE_TRANSITION_EVENT_NAME,
    Phase,
    hash_code,
    hash_spec,
)
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.quality_gates.universe_injection import (
    inject_universe_and_guard,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)

pytestmark = pytest.mark.strategy_lab_integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )


def _spec_dict() -> Dict[str, Any]:
    return {
        # No "asset_class": the design loop pins each attempt to one
        # randomly-selected category and an omitted class inherits that
        # pin, so this payload stays valid whichever category is drawn.
        "hypothesis": "RSI mean reversion on a small universe",
        "signal_definition": "RSI(14) crossings",
        "timeframe": "1d",
        "entry_rules": [
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op="<",
                    rhs=30,
                ),
            ).model_dump()
        ],
        "exit_rules": [
            SignalExitRule(
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op=">",
                    rhs=70,
                )
            ).model_dump()
        ],
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10},
        "target_symbols": ["QQQ"],
        "speculative": False,
    }


_VALID_CODE = (
    "from contract import Strategy\n\n"
    "class S(Strategy):\n"
    "    def on_bar(self, ctx, bar):\n"
    "        pass\n"
)


def _drive_cycle_capturing_transitions(
    orch: StrategyLabOrchestrator,
) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[Dict[str, Any]], Any]:
    """Run ``orch.run_cycle`` once and return ``(all_events, transitions, record)``.

    Pre: ``orch`` has already had its collaborators stubbed by the caller.
    Post: ``all_events`` is every ``(phase, data)`` pair emitted through
    ``on_phase``; ``transitions`` is the subset whose phase equals
    :data:`PHASE_TRANSITION_EVENT_NAME` (the typed boundary events);
    ``record`` is the final :class:`StrategyLabRecord`.
    """
    events: List[Tuple[str, Dict[str, Any]]] = []
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        on_phase=lambda phase, data: events.append((phase, data)),
    )
    transitions = [data for phase, data in events if phase == PHASE_TRANSITION_EVENT_NAME]
    return events, transitions, record


def _force_synthesis_skip(
    monkeypatch: pytest.MonkeyPatch, orch: StrategyLabOrchestrator, code: str
) -> None:
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: code)
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: code)


def _short_circuit_synthesis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the synthesis loop short-circuit at the no-market-data path.

    With ``data=None`` the synthesis loop appends a market_data gate and
    breaks; the orchestrator still flows through Boundary 3 and Boundary 4
    (those are observability events, not happy-path gates) but the record
    carries ``execution_succeeded=False`` and verification is a no-op.
    """
    from investment_team.strategy_lab.orchestrator import _MarketDataFetch

    monkeypatch.setattr(
        StrategyLabOrchestrator,
        "_fetch_market_data",
        lambda *_a, **_kw: _MarketDataFetch(data=None, requested_symbols=[], fetched_symbols=[]),
    )


def _stub_design_path(monkeypatch: pytest.MonkeyPatch, orch: StrategyLabOrchestrator) -> None:
    """Stub the design loop so it converges on the first review round."""
    monkeypatch.setattr(
        orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted rationale")
    )
    monkeypatch.setattr(
        orch.design_review_agent,
        "run",
        lambda *_a, **_kw: SpecCritique(ready=True, rationale="ok"),
    )
    monkeypatch.setattr(orch.design_agent, "revise", lambda *_a, **_kw: (_spec_dict(), "revised"))


def _stub_pipeline_for_happy_path(
    monkeypatch: pytest.MonkeyPatch, orch: StrategyLabOrchestrator
) -> None:
    """Wire the full pipeline so all four phase transitions fire.

    The synthesis loop short-circuits at the no-market-data path; this
    keeps the test small while still driving the orchestrator through
    every boundary emission site. Alignment + verification are
    structurally no-ops when ``execution_succeeded=False`` so this is
    enough to exercise the boundary contract end-to-end.
    """
    _stub_design_path(monkeypatch, orch)
    _force_synthesis_skip(monkeypatch, orch, _VALID_CODE)
    # The deterministic CodeConformanceGate rejects the placeholder
    # ``_VALID_CODE`` (it has no real entry/exit logic); neutralise it so
    # the synthesis refinement loop converges on the first round instead
    # of churning through 50 rounds of LLM-less refinement calls.
    monkeypatch.setattr(orch.code_conformance_gate, "check", lambda *_a, **_kw: [])
    _short_circuit_synthesis(monkeypatch)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_cycle_emits_exactly_four_phase_transitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: ``run_cycle`` emits exactly four ``phase_transition``
    events, one per phase exit, in the order documented by :data:`PHASES`."""
    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)

    _events, transitions, _record = _drive_cycle_capturing_transitions(orch)

    expected = [
        (Phase.DESIGN.value, Phase.DESIGN_REVIEW.value),
        (Phase.DESIGN_REVIEW.value, Phase.CODE_SYNTHESIS.value),
        (Phase.CODE_SYNTHESIS.value, Phase.BACKTEST_AND_VERIFICATION.value),
        (Phase.BACKTEST_AND_VERIFICATION.value, None),
    ]
    actual = [(t["from_phase"], t["to_phase"]) for t in transitions]
    assert actual == expected, (
        f"phase_transition sequence mismatch — expected {expected!r}, got {actual!r}"
    )

    # Every transition carries 64-character SHA-256 hex digests and a
    # non-negative attempt counter (zero on the first design attempt).
    for t in transitions:
        assert len(t["spec_hash"]) == 64
        assert len(t["code_hash"]) == 64
        assert t["attempt"] == 0


def test_unconverged_refinement_does_not_capture_later_stage_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-market-data exit crosses observability phase transitions but does
    not falsely claim that refinement or alignment converged."""

    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)

    _drive_cycle_capturing_transitions(orch)

    assert [checkpoint.stage for checkpoint in orch.pipeline_checkpoints] == [
        PipelineStage.DESIGN,
        PipelineStage.REVIEW,
        PipelineStage.SYNTHESIS,
    ]


def test_spec_hash_stable_after_design_review_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On this happy-path fixture the spec is not mutated post-design, so
    ``spec_hash`` from the ``DESIGN_REVIEW → CODE_SYNTHESIS`` boundary
    onward must be equal. Fixture behaviour, not a universal invariant —
    the module docstring covers the carve-out caveat.
    """
    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)

    _events, transitions, _record = _drive_cycle_capturing_transitions(orch)

    # Skip the first transition (DESIGN → DESIGN_REVIEW); the spec is
    # still being revised inside the design loop at that point. From
    # Boundary 2 onward the spec is frozen.
    post_design_hashes = {t["spec_hash"] for t in transitions[1:]}
    assert len(post_design_hashes) == 1, (
        f"spec_hash drift detected after design review — distinct hashes: {post_design_hashes!r}"
    )


def test_code_hash_at_synthesis_exit_matches_synthesised_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``code_hash`` on the ``CODE_SYNTHESIS → BACKTEST_AND_VERIFICATION``
    transition must equal :func:`hash_code` of the code that actually proceeds
    to backtest. ``_VALID_CODE`` lacks the ``UNIVERSE`` constant and ``on_bar``
    symbol guard that the spec's non-empty ``target_symbols`` require, so the
    synthesis loop's deterministic injector normalizes it before any gate or
    the backtest runs — the transition therefore carries the injected hash."""
    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)

    _events, transitions, _record = _drive_cycle_capturing_transitions(orch)

    expected_code = inject_universe_and_guard(_VALID_CODE, SimpleNamespace(target_symbols=["QQQ"]))
    assert expected_code != _VALID_CODE  # the injector must have changed it
    synthesis_exit = next(t for t in transitions if t["from_phase"] == Phase.CODE_SYNTHESIS.value)
    assert synthesis_exit["code_hash"] == hash_code(expected_code)

    # The two transitions before ``CODE_SYNTHESIS`` exit carry the
    # empty-string SHA-256 (no code synthesised yet).
    pre_synthesis = [
        t
        for t in transitions
        if t["from_phase"] != Phase.CODE_SYNTHESIS.value
        and t["to_phase"] == Phase.DESIGN_REVIEW.value
        or t["to_phase"] == Phase.CODE_SYNTHESIS.value
    ]
    assert all(t["code_hash"] == hash_code("") for t in pre_synthesis)


def test_design_to_synthesis_blocked_when_readiness_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critical ``SpecReadinessGate`` failure short-circuits the cycle
    before the ``DESIGN_REVIEW → CODE_SYNTHESIS`` transition fires. Only
    the first transition (``DESIGN → DESIGN_REVIEW``) is emitted; the
    record carries ``status='failed: design_not_ready'``."""
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "2")
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted"))
    monkeypatch.setattr(orch.design_agent, "revise", lambda *_a, **_kw: (_spec_dict(), "revised"))
    # Reviewer is never called when readiness fires critical; stub it
    # anyway so a regression that flips the order surfaces loudly.
    monkeypatch.setattr(
        orch.design_review_agent,
        "run",
        lambda *_a, **_kw: SpecCritique(ready=False, rationale="should-not-run"),
    )
    # Force every readiness call to fail critical.
    monkeypatch.setattr(
        orch.spec_readiness_gate,
        "validate",
        lambda *_a, **_kw: [
            QualityGateResult(
                gate_name="spec_readiness:rule_5_price_anchor",
                passed=False,
                severity="critical",
                phase="design",
                details="forced readiness failure for test",
            )
        ],
    )

    # If the cycle ever crossed Boundary 2, market-data fetch would fire
    # next — assert it doesn't.
    def _market_must_not_run(self, *_a, **_kw):
        raise AssertionError("synthesis loop must not be entered when readiness gate fails")

    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _market_must_not_run)

    _events, transitions, record = _drive_cycle_capturing_transitions(orch)

    assert len(transitions) == 1
    assert transitions[0]["from_phase"] == Phase.DESIGN.value
    assert transitions[0]["to_phase"] == Phase.DESIGN_REVIEW.value
    assert record.backtest.status == "failed: design_not_ready"


def test_synthesis_to_verification_observability_event_fires_but_verification_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critical ``CodeConformanceGate`` failure forces the synthesis loop
    to exhaust without converging. The ``CODE_SYNTHESIS →
    BACKTEST_AND_VERIFICATION`` transition still fires for observability,
    but verification cannot do real work: no acceptance gate runs and
    the record reflects a failure status. AC3 invariant: a successful
    verification is impossible without ``CodeConformanceGate.passed``.
    """
    orch = StrategyLabOrchestrator()
    _stub_design_path(monkeypatch, orch)
    _force_synthesis_skip(monkeypatch, orch, _VALID_CODE)
    _short_circuit_synthesis(monkeypatch)

    # Force conformance to critical-fail on every check. The synthesis
    # loop will refine + retry until max-rounds exhausts (refinement is
    # a no-op given our stubs) and return ``execution_succeeded=False``.
    monkeypatch.setattr(
        orch.code_conformance_gate,
        "check",
        lambda *_a, **_kw: [
            QualityGateResult(
                gate_name="code_conformance",
                passed=False,
                severity="critical",
                phase="synthesis",
                details="forced conformance failure for test",
            )
        ],
    )
    # Pin refinement to a no-op so the loop deterministically exhausts.
    monkeypatch.setattr(
        orch.refinement_agent, "run", lambda **_kw: ({"changes_made": "no-op"}, _VALID_CODE)
    )

    _events, transitions, record = _drive_cycle_capturing_transitions(orch)

    # All four boundaries still fire — transitions are observability events.
    seq = [(t["from_phase"], t["to_phase"]) for t in transitions]
    assert seq == [
        (Phase.DESIGN.value, Phase.DESIGN_REVIEW.value),
        (Phase.DESIGN_REVIEW.value, Phase.CODE_SYNTHESIS.value),
        (Phase.CODE_SYNTHESIS.value, Phase.BACKTEST_AND_VERIFICATION.value),
        (Phase.BACKTEST_AND_VERIFICATION.value, None),
    ]

    # But verification did no real work: no acceptance gate ran. AC3 — a
    # successful verification cannot be produced when conformance fails.
    acceptance_gates = [
        g
        for g in record.quality_gate_results
        if str(g.get("gate_name", "")).startswith("acceptance_")
        or "oos_deflated_sharpe" in str(g.get("gate_name", ""))
    ]
    assert acceptance_gates == [], (
        f"verification ran acceptance-phase gates despite conformance failure: {acceptance_gates!r}"
    )
    assert record.is_winning is False
    assert record.backtest.status.startswith("failed:")


def test_phase_transition_attempt_increments_on_design_reentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``SpecImplementabilityError`` routes the cycle back into the
    design phase, every transition fired on the retry attempt carries
    ``attempt`` incremented by one. This lets log consumers separate
    transitions belonging to distinct design attempts."""
    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)

    # First attempt raises ``SpecImplementabilityError`` from inside
    # ``_run_design_attempt`` after Boundary 1 fires; ``run_cycle`` then
    # re-enters with ``design_attempt=1`` and the second attempt
    # completes normally. We patch ``_run_design_attempt`` to interleave
    # one raise + one successful delegation to the real method.
    real_run_design_attempt = orch._run_design_attempt
    call_state = {"n": 0}

    def _flaky_run_design_attempt(**kwargs: Any) -> Any:
        call_state["n"] += 1
        if call_state["n"] == 1:
            # Emit Boundary 1 manually so the attempt=0 transition is
            # observable, then raise — mirrors what would happen if the
            # design phase converged but refinement later tripped
            # ``SpecImplementabilityError`` after re-emitting Boundary 1.
            from investment_team.strategy_lab.orchestrator import _emit_phase_transition

            # Build a throwaway spec snapshot for the boundary event.
            scratch_spec = orch._build_spec_from_dict(_spec_dict(), strategy_id="strat-fake")
            _emit_phase_transition(
                kwargs["emit"],
                from_phase=Phase.DESIGN,
                to_phase=Phase.DESIGN_REVIEW,
                spec=scratch_spec,
                code="",
                attempt=kwargs["design_attempt"],
            )
            raise SpecImplementabilityError(
                "forced re-entry for test",
                failure_phase="refinement",
                last_spec=scratch_spec,
                last_code="",
            )
        return real_run_design_attempt(**kwargs)

    monkeypatch.setattr(orch, "_run_design_attempt", _flaky_run_design_attempt)

    _events, transitions, _record = _drive_cycle_capturing_transitions(orch)

    # Group transitions by attempt counter.
    attempts: Dict[int, List[Dict[str, Any]]] = {}
    for t in transitions:
        attempts.setdefault(t["attempt"], []).append(t)

    # First attempt fired only Boundary 1 before raising; second attempt
    # fired the full four-transition sequence.
    assert set(attempts.keys()) == {0, 1}
    assert len(attempts[0]) == 1
    assert attempts[0][0]["from_phase"] == Phase.DESIGN.value
    assert len(attempts[1]) == 4
    assert [(t["from_phase"], t["to_phase"]) for t in attempts[1]] == [
        (Phase.DESIGN.value, Phase.DESIGN_REVIEW.value),
        (Phase.DESIGN_REVIEW.value, Phase.CODE_SYNTHESIS.value),
        (Phase.CODE_SYNTHESIS.value, Phase.BACKTEST_AND_VERIFICATION.value),
        (Phase.BACKTEST_AND_VERIFICATION.value, None),
    ]


def test_run_design_attempt_checkpoint_hook_fires_once_at_design_synthesis_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``checkpoint_hook`` (ADR-012) fires exactly once, right after Phase 1
    converges and before any Phase 1b (code synthesis) work runs -- the exact
    design/synthesis boundary the checkpoint contract targets."""
    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)

    call_order: List[str] = []

    def _tracked_compile(spec: Any) -> str:
        call_order.append("compile_strategy")
        return _VALID_CODE

    monkeypatch.setattr(orchestrator_module, "compile_strategy", _tracked_compile)

    hook_calls: List[Tuple[str, Dict[str, Any]]] = []

    def _hook(phase: str, data: Dict[str, Any]) -> None:
        call_order.append("checkpoint_hook")
        hook_calls.append((phase, data))

    orch._run_design_attempt(
        prior_records=[],
        config=_config(),
        signal_briefs=None,
        emit=lambda *_a, **_kw: None,
        exclude_asset_classes=None,
        directives=[],
        checkpoint_hook=_hook,
    )

    assert len(hook_calls) == 1
    phase, data = hook_calls[0]
    assert phase == "design_synthesis_boundary"
    assert data["spec"] is not None
    assert data["design_context"] is not None
    assert call_order == ["checkpoint_hook", "compile_strategy"]


def test_run_design_attempt_resume_skips_phase_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supplying ``resume_spec``/``resume_rationale``/``resume_design_context``
    causes the design agent to never be invoked, and the returned record
    reflects the supplied resume rationale -- Phase 1's LLM calls are never
    re-issued on a checkpoint resume (ADR-012's no-double-charge requirement
    is structural precisely because this path never calls
    ``_orchestrate_design_and_review``)."""
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext

    orch = StrategyLabOrchestrator()
    _force_synthesis_skip(monkeypatch, orch, _VALID_CODE)
    monkeypatch.setattr(orch.code_conformance_gate, "check", lambda *_a, **_kw: [])
    _short_circuit_synthesis(monkeypatch)

    def _fail_if_called(**_kw: Any) -> Any:
        raise AssertionError("design_agent.run must not be called on a checkpoint resume")

    monkeypatch.setattr(orch.design_agent, "run", _fail_if_called)

    resume_spec = orch._build_spec_from_dict(_spec_dict(), strategy_id="resumed-strategy")
    resume_design_context = _DesignPersistContext(
        rounds=2, critiques=[], stop_reason="ready", loop_telemetry={}
    )

    record = orch._run_design_attempt(
        prior_records=[],
        config=_config(),
        signal_briefs=None,
        emit=lambda *_a, **_kw: None,
        exclude_asset_classes=None,
        directives=[],
        resume_spec=resume_spec,
        resume_rationale="resumed rationale",
        resume_design_context=resume_design_context,
    )

    assert record.strategy_rationale == "resumed rationale"


def test_run_design_attempt_rejects_resume_spec_without_design_context() -> None:
    orch = StrategyLabOrchestrator()
    resume_spec = orch._build_spec_from_dict(_spec_dict(), strategy_id="resumed-strategy")

    with pytest.raises(ValueError, match="resume_spec and resume_design_context"):
        orch._run_design_attempt(
            prior_records=[],
            config=_config(),
            signal_briefs=None,
            emit=lambda *_a, **_kw: None,
            exclude_asset_classes=None,
            directives=[],
            resume_spec=resume_spec,
            resume_rationale="resumed rationale",
            resume_design_context=None,
        )


def _record_attempt_drift(orch: StrategyLabOrchestrator, collector: Any, attempt: int) -> None:
    """Record one spec revision into ``collector`` tagged for ``attempt``.

    Uses two specs with distinct ``strategy_id`` so the hashes differ and
    ``record_spec_change`` actually appends (it is a no-op on equal hashes).
    """
    collector.record_spec_change(
        phase="design",
        agent="DesignAgent",
        before_spec=orch._build_spec_from_dict(_spec_dict(), strategy_id=f"s-{attempt}-a"),
        after_spec=orch._build_spec_from_dict(_spec_dict(), strategy_id=f"s-{attempt}-b"),
        reason=f"attempt-{attempt} drift",
    )


def test_failed_design_attempt_does_not_poison_next_attempt_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each design attempt gets a clean, isolated ``_DriftCollector``.

    Attempts 0 and 1 record drift then raise ``SpecImplementabilityError``;
    attempt 2 succeeds. Every attempt must enter with an empty collector (a
    distinct object), and the record produced by the successful attempt must
    NOT contain the failed attempts' drift entries.
    """
    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)

    real_run_design_attempt = orch._run_design_attempt
    # (attempt_index, drift_collector, spec_history_len_on_entry). The collector
    # object itself is retained — not merely its id() — because id() is unique
    # only among simultaneously-live objects: a failed attempt's collector would
    # otherwise be garbage-collected and its address reused by the next attempt's
    # collector, making the distinctness check below spuriously fail.
    entries: List[Tuple[int, Any, int]] = []

    def _flaky_run_design_attempt(**kwargs: Any) -> Any:
        collector = kwargs["drift_collector"]
        attempt = kwargs["design_attempt"]
        entries.append((attempt, collector, len(collector.spec_history)))
        if attempt < 2:
            _record_attempt_drift(orch, collector, attempt)
            raise SpecImplementabilityError(
                f"forced fail attempt {attempt}",
                failure_phase="refinement",
                last_spec=orch._build_spec_from_dict(_spec_dict(), strategy_id=f"s-{attempt}"),
                last_code="",
            )
        return real_run_design_attempt(**kwargs)

    monkeypatch.setattr(orch, "_run_design_attempt", _flaky_run_design_attempt)

    _events, _transitions, record = _drive_cycle_capturing_transitions(orch)

    # Three attempts ran, each with a distinct collector object that was
    # empty on entry (copy-on-entry isolation). All three collectors are still
    # referenced via ``entries``, so their id()s are guaranteed non-colliding.
    assert [a for a, _c, _n in entries] == [0, 1, 2]
    assert len({id(c) for _a, c, _n in entries}) == 3
    assert all(n == 0 for _a, _c, n in entries)

    # The successful attempt's record carries none of the failed attempts'
    # drift — no cross-attempt contamination.
    reasons = [r.reason for r in record.spec_history]
    assert "attempt-0 drift" not in reasons
    assert "attempt-1 drift" not in reasons


def test_short_circuit_record_preserves_failed_attempt_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every design attempt fails, the short-circuit record still
    contains the drift each failed attempt produced.

    The parent commit log accumulates each attempt's child collector on
    failure (commit-on-completion), so diagnostics survive even though no
    attempt poisoned its successor's working collector.
    """
    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)

    def _always_fail(**kwargs: Any) -> Any:
        collector = kwargs["drift_collector"]
        attempt = kwargs["design_attempt"]
        # Each attempt entered clean.
        assert collector.spec_history == []
        _record_attempt_drift(orch, collector, attempt)
        raise SpecImplementabilityError(
            f"forced fail attempt {attempt}",
            failure_phase="refinement",
            last_spec=orch._build_spec_from_dict(_spec_dict(), strategy_id=f"s-{attempt}"),
            last_code="",
        )

    monkeypatch.setattr(orch, "_run_design_attempt", _always_fail)

    _events, _transitions, record = _drive_cycle_capturing_transitions(orch)

    assert record.backtest.status == "failed: spec_unimplementable"
    # All three failed attempts' drift is merged into the diagnostic record,
    # in attempt order.
    reasons = [r.reason for r in record.spec_history]
    assert reasons == ["attempt-0 drift", "attempt-1 drift", "attempt-2 drift"]


# ---------------------------------------------------------------------------
# Pure helpers (no orchestrator wiring — small unit tests for phases.py)
# ---------------------------------------------------------------------------


def test_hash_code_empty_and_none_match() -> None:
    """``hash_code(None) == hash_code('')`` — the empty-code sentinel."""
    assert hash_code(None) == hash_code("")
    assert len(hash_code(None)) == 64


def test_hash_spec_deterministic_across_calls() -> None:
    """Two structurally equal specs produce identical ``hash_spec`` output."""
    orch = StrategyLabOrchestrator()
    spec_a = orch._build_spec_from_dict(_spec_dict(), strategy_id="strat-x")
    spec_b = orch._build_spec_from_dict(_spec_dict(), strategy_id="strat-x")
    assert hash_spec(spec_a) == hash_spec(spec_b)
    # Mutating a field changes the hash.
    spec_c = orch._build_spec_from_dict(_spec_dict(), strategy_id="strat-DIFFERENT")
    assert hash_spec(spec_a) != hash_spec(spec_c)


def test_build_spec_from_dict_canonicalizes_accepted_aliases() -> None:
    """Accepted aliases canonicalize before the strict ``StrategySpec`` boundary
    so a clean mapping never trips the validator or aborts the cycle."""
    orch = StrategyLabOrchestrator()

    payload = _spec_dict()
    payload["asset_class"] = "equity"
    assert orch._build_spec_from_dict(payload, strategy_id="strat-coerce").asset_class == "stocks"

    payload = _spec_dict()
    payload["asset_class"] = "cryptocurrency"
    assert orch._build_spec_from_dict(payload, strategy_id="strat-coerce").asset_class == "crypto"


def test_build_spec_from_dict_blank_asset_class_defaults_to_the_pin() -> None:
    """A missing or blank ``asset_class`` is not an unsupported class: it must
    resolve to ``default_asset_class`` — the category the design attempt is
    pinned to — without raising ``SpecImplementabilityError``, so a designer
    that omits or blanks the field is not forced through a spurious redesign
    loop. The default is ``stocks`` when no pin is supplied."""
    orch = StrategyLabOrchestrator()

    for blank in ("", "   ", None):
        payload = dict(_spec_dict())
        payload["asset_class"] = blank
        spec = orch._build_spec_from_dict(payload, strategy_id="strat-blank")
        assert spec.asset_class == "stocks", repr(blank)
        pinned = orch._build_spec_from_dict(
            payload, strategy_id="strat-blank", default_asset_class="crypto"
        )
        assert pinned.asset_class == "crypto", repr(blank)

    # An absent key falls back to the same default.
    payload = dict(_spec_dict())
    payload.pop("asset_class", None)
    assert orch._build_spec_from_dict(payload, strategy_id="strat-missing").asset_class == "stocks"


def test_build_spec_from_dict_routes_unsupported_class_to_redesign() -> None:
    """A genuinely-unsupported class (e.g. ``bonds``) must NOT be silently
    coerced to ``stocks`` and backtested as a stock strategy. It raises
    ``SpecImplementabilityError`` so ``run_cycle`` re-enters design with the
    defect as evidence rather than recording a misleading stock backtest. The
    evidence names the rejected class so the re-entry directive can steer the
    LLM, and ``last_spec`` is well-formed for the short-circuit record."""
    orch = StrategyLabOrchestrator()

    payload = _spec_dict()
    payload["asset_class"] = "bonds"
    with pytest.raises(SpecImplementabilityError) as exc:
        orch._build_spec_from_dict(payload, strategy_id="strat-unsupported")
    assert "bonds" in exc.value.evidence
    assert exc.value.failure_phase == "design"
    assert exc.value.last_spec is not None
