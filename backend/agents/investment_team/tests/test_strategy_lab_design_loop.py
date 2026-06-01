"""Integration tests for the design ↔ design-review loop.

These tests drive a real ``StrategyLabOrchestrator`` through ``run_cycle``
with the design and review agents stubbed. They lock in:

* Round-1 pass — review returns ``ready=True`` immediately; no revise call.
* N rounds then pass — review returns False for N-1 rounds then True;
  ``record.design_rounds == N`` and ``revise`` was called N-1 times.
* Never ready → short-circuit with ``status="failed: design_not_ready"``,
  ``critiques`` length equals the round cap, and the synthesis loop is
  never entered (sandbox / market data are never touched).
* When ``SpecReadinessGate`` fires a critical, the reviewer is *not*
  called for that round — the synthetic critique stands in.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from investment_team.models import BacktestConfig
from investment_team.strategy_lab import orchestrator as orchestrator_module
from investment_team.strategy_lab.agents._llm_budget import charge_active_budget
from investment_team.strategy_lab.agents.design_review import (
    CritiqueIssue,
    SpecCritique,
)
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)

# These tests drive a real orchestrator end-to-end; the marker auto-applies
# the readiness fetch stub from conftest.
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
        "asset_class": "stocks",
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


def _short_circuit_synthesis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the synthesis loop short-circuit immediately by returning no
    market data. The design loop is what's under test; the rest of the
    pipeline only needs to not crash on an empty cycle.
    """
    from investment_team.strategy_lab.orchestrator import _MarketDataFetch

    monkeypatch.setattr(
        StrategyLabOrchestrator,
        "_fetch_market_data",
        lambda *_a, **_kw: _MarketDataFetch(data=None, requested_symbols=[], fetched_symbols=[]),
    )


def _force_synthesis_skip(
    monkeypatch: pytest.MonkeyPatch, orch: StrategyLabOrchestrator, code: str
) -> None:
    """Stub ``compile_strategy`` so we don't depend on the deterministic
    compiler's actual behaviour and ``code_synthesis_agent`` so the
    custom-code fallback never calls a real LLM.
    """
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: code)
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: code)


_VALID_CODE = (
    "from contract import Strategy\n\n"
    "class S(Strategy):\n"
    "    def on_bar(self, ctx, bar):\n"
    "        pass\n"
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_round_one_pass_no_revise_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review returns ``ready=True`` on the first call → design_rounds=1 and
    ``DesignAgent.revise`` is never called."""
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(
        orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted rationale")
    )
    monkeypatch.setattr(
        orch.design_review_agent, "run", lambda *a, **kw: SpecCritique(ready=True, rationale="ok")
    )

    revise_calls: List[Tuple[Any, ...]] = []

    def _revise(*args, **kwargs) -> Tuple[Dict[str, Any], str]:
        revise_calls.append((args, kwargs))
        return _spec_dict(), "should-not-be-used"

    monkeypatch.setattr(orch.design_agent, "revise", _revise)
    _force_synthesis_skip(monkeypatch, orch, _VALID_CODE)
    _short_circuit_synthesis(monkeypatch)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert revise_calls == []
    assert record.design_rounds == 1
    assert len(record.critiques) == 1
    assert record.critiques[0]["ready"] is True


def test_n_rounds_then_pass_records_round_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer returns False for two rounds, then True. Final record carries
    ``design_rounds == 3`` and ``revise`` was called twice."""
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted"))

    review_calls = iter(
        [
            SpecCritique(
                ready=False,
                rationale="round-0",
                issues=[CritiqueIssue(field="exit_rules", description="add take_profit")],
            ),
            SpecCritique(
                ready=False,
                rationale="round-1",
                issues=[CritiqueIssue(field="sizing", description="too aggressive")],
            ),
            SpecCritique(ready=True, rationale="round-2 ok"),
        ]
    )
    monkeypatch.setattr(orch.design_review_agent, "run", lambda *a, **kw: next(review_calls))

    revise_counter = {"n": 0}

    def _revise(*_a, **_kw) -> Tuple[Dict[str, Any], str]:
        revise_counter["n"] += 1
        return _spec_dict(), "revised"

    monkeypatch.setattr(orch.design_agent, "revise", _revise)
    _force_synthesis_skip(monkeypatch, orch, _VALID_CODE)
    _short_circuit_synthesis(monkeypatch)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert revise_counter["n"] == 2
    assert record.design_rounds == 3
    assert len(record.critiques) == 3
    assert record.critiques[-1]["ready"] is True


def test_never_ready_short_circuits_with_design_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer never readies but raises a *different* issue each round (no
    stall) → cycle exhausts the round cap and short-circuits with
    ``status="failed: design_not_ready"``, never entering the synthesis
    loop (market data is never fetched)."""
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "3")
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted"))

    # A distinct issue per round keeps the open-issue set changing so the
    # within-loop stall guard does NOT trip — this exercises the honest
    # round-cap exhaustion path, distinct from the stall path below.
    review_round = {"n": 0}

    def _review(*_a, **_kw) -> SpecCritique:
        review_round["n"] += 1
        return SpecCritique(
            ready=False,
            rationale="incoherent",
            issues=[CritiqueIssue(field="hypothesis", description=f"vague-{review_round['n']}")],
        )

    monkeypatch.setattr(orch.design_review_agent, "run", _review)
    monkeypatch.setattr(orch.design_agent, "revise", lambda *_a, **_kw: (_spec_dict(), "revised"))

    def _market_must_not_run(self, *_a, **_kw):
        raise AssertionError("synthesis loop must not be entered when design fails to ready")

    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _market_must_not_run)

    def _sandbox_must_not_run(*_a, **_kw):
        raise AssertionError("sandbox must not run when design fails to ready")

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _sandbox_must_not_run)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert record.backtest.status == "failed: design_not_ready"
    assert record.is_winning is False
    assert record.design_rounds == 3
    assert len(record.critiques) == 3
    # Acceptance-reason audit-trail must self-document the cause.
    ar = record.backtest.result.acceptance_reason or ""
    assert "publication_disabled" in ar and "design_not_ready" in ar


def test_readiness_critical_skips_reviewer_for_that_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the deterministic readiness gate returns a critical, the design
    loop synthesises a critique from the readiness findings and does NOT
    call the LLM reviewer that round."""
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "2")
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted"))
    monkeypatch.setattr(orch.design_agent, "revise", lambda *a, **kw: (_spec_dict(), "revised"))

    review_calls = {"n": 0}

    def _review(*_a, **_kw) -> SpecCritique:
        review_calls["n"] += 1
        return SpecCritique(ready=True, rationale="never reached in this test")

    monkeypatch.setattr(orch.design_review_agent, "run", _review)

    # Force readiness to always emit a critical so the reviewer is skipped
    # on every round.
    def _always_critical(*_a, **_kw) -> List[QualityGateResult]:
        return [
            QualityGateResult(
                gate_name="spec_readiness",
                passed=False,
                severity="critical",
                phase="design",
                details="forced critical for test",
            )
        ]

    monkeypatch.setattr(orch.spec_readiness_gate, "validate", _always_critical)

    record = orch.run_cycle(prior_records=[], config=_config())

    # Reviewer never called.
    assert review_calls["n"] == 0
    # Loop exhausted because no critique ever flipped to ready.
    assert record.backtest.status == "failed: design_not_ready"
    # Synthetic critique stamped each round.
    assert record.design_rounds == 2
    for entry in record.critiques:
        assert entry["ready"] is False
        # The synthetic critique carries the readiness findings.
        assert entry["readiness_findings"]
        assert "forced critical" in entry["readiness_findings"][0]


def test_compiler_error_falls_back_to_code_synthesis_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``compile_strategy`` raises ``CompilerError``, the orchestrator
    flips the spec to ``requires_custom_code`` and asks the LLM synthesis
    agent for code instead of short-circuiting."""
    from investment_team.strategy_lab.synthesis import CompilerError

    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted"))
    monkeypatch.setattr(orch.design_review_agent, "run", lambda *a, **kw: SpecCritique(ready=True))

    def _compile_fails(_spec):
        raise CompilerError("unsupported indicator combo")

    monkeypatch.setattr(orchestrator_module, "compile_strategy", _compile_fails)
    custom_code_calls = {"n": 0}

    def _synth(spec):
        custom_code_calls["n"] += 1
        return _VALID_CODE

    monkeypatch.setattr(orch.code_synthesis_agent, "run", _synth)
    _short_circuit_synthesis(monkeypatch)

    record = orch.run_cycle(prior_records=[], config=_config())

    # CodeSynthesisAgent was invoked exactly once after compile_strategy raised.
    assert custom_code_calls["n"] == 1
    # The persisted spec carries the requires_custom_code=True flag the
    # fallback flipped on so a later re-load can replay the same path.
    assert record.strategy.requires_custom_code is True


def test_code_synthesis_failure_short_circuits_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both ``compile_strategy`` and ``code_synthesis_agent.run`` fail
    after the design loop converged, the orchestrator short-circuits with
    ``status="failed: code_synthesis"`` rather than entering the synthesis
    loop with no code."""
    from investment_team.strategy_lab.agents.code_synthesis import CodeSynthesisError
    from investment_team.strategy_lab.synthesis import CompilerError

    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted"))
    monkeypatch.setattr(orch.design_review_agent, "run", lambda *a, **kw: SpecCritique(ready=True))

    def _compile_fails(_spec):
        raise CompilerError("compiler down")

    monkeypatch.setattr(orchestrator_module, "compile_strategy", _compile_fails)

    def _synth_fails(_spec):
        raise CodeSynthesisError("LLM unreachable")

    monkeypatch.setattr(orch.code_synthesis_agent, "run", _synth_fails)

    def _sandbox_must_not_run(*_a, **_kw):
        raise AssertionError(
            "sandbox must not run when code synthesis fails after design converges"
        )

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _sandbox_must_not_run)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert record.backtest.status == "failed: code_synthesis"
    assert record.is_winning is False
    ar = record.backtest.result.acceptance_reason or ""
    assert "publication_disabled" in ar and "code_synthesis" in ar


def test_design_review_rounds_env_override_floors_to_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``STRATEGY_LAB_DESIGN_REVIEW_ROUNDS=0`` (or sub-1) is floored to 1
    so the design loop always runs at least once."""
    from investment_team.strategy_lab.orchestrator import _design_review_rounds

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "0")
    assert _design_review_rounds() == 1

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "garbage")
    assert _design_review_rounds() == 20  # falls back to default

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "7")
    assert _design_review_rounds() == 7


def _charging_run(*_a, **_kw) -> Tuple[Dict[str, Any], str]:
    """Stub ``DesignAgent.run``/``revise`` that consumes one unit of the
    active budget per call (simulating one real LLM round-trip) before
    returning a spec — exactly as the real agents charge."""
    charge_active_budget()
    return _spec_dict(), "scripted"


def test_budget_exhausted_short_circuits_with_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the per-cycle LLM-call budget trips before the round cap, the
    cycle short-circuits with ``status="failed: budget_exhausted"`` and
    never enters synthesis. The round cap is set high so the budget — not
    the rounds — is what stops the loop."""
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", "2")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "10")
    orch = StrategyLabOrchestrator()

    # Each stub charges the budget exactly as the real agents would: run()=1,
    # review()=1 → budget (limit 2) is spent; the first revise() trips it.
    monkeypatch.setattr(orch.design_agent, "run", _charging_run)
    monkeypatch.setattr(orch.design_agent, "revise", _charging_run)

    def _review(*_a, **_kw) -> SpecCritique:
        charge_active_budget()
        return SpecCritique(
            ready=False,
            rationale="incoherent",
            issues=[CritiqueIssue(field="hypothesis", description="vague")],
        )

    monkeypatch.setattr(orch.design_review_agent, "run", _review)

    def _market_must_not_run(self, *_a, **_kw):
        raise AssertionError("synthesis must not run when the budget is exhausted")

    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _market_must_not_run)

    def _sandbox_must_not_run(*_a, **_kw):
        raise AssertionError("sandbox must not run when the budget is exhausted")

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _sandbox_must_not_run)

    telemetry_events: list = []

    def _on_phase(phase: str, data: dict) -> None:
        if phase == "telemetry":
            telemetry_events.append(data)

    record = orch.run_cycle(prior_records=[], config=_config(), on_phase=_on_phase)

    assert record.backtest.status == "failed: budget_exhausted"
    assert record.is_winning is False
    ar = record.backtest.result.acceptance_reason or ""
    assert "publication_disabled" in ar and "budget_exhausted" in ar
    # The budget-exhaustion path must carry forward the critique-ledger
    # counters (so a budget exit after real review is distinguishable from one
    # that never reached review) AND emit the per-cycle design_loop summary on
    # the callback, mirroring the normal-exit path.
    telemetry = record.loop_telemetry
    assert telemetry["stop_reason"] == "budget_exhausted"
    assert "critique_ledger" in telemetry
    loop_summaries = [e for e in telemetry_events if e.get("scope") == "design_loop"]
    assert len(loop_summaries) == 1
    assert loop_summaries[0]["stop_reason"] == "budget_exhausted"


def test_budget_spans_design_reentries(monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget is per-cycle, not per-attempt: a high round cap plus a
    budget smaller than one attempt's worth of calls trips inside the first
    attempt rather than resetting on re-entry."""
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", "3")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "10")
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", _charging_run)
    monkeypatch.setattr(orch.design_agent, "revise", _charging_run)

    def _review(*_a, **_kw) -> SpecCritique:
        charge_active_budget()
        return SpecCritique(ready=False, rationale="nope")

    monkeypatch.setattr(orch.design_review_agent, "run", _review)
    _short_circuit_synthesis(monkeypatch)

    record = orch.run_cycle(prior_records=[], config=_config())

    # budget 3: run(1) + review(2) + revise(3) succeed, the round-1 review
    # trips. No SpecImplementabilityError re-entry can grant a fresh budget.
    assert record.backtest.status == "failed: budget_exhausted"


def test_budget_not_tripped_on_converging_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec that readies on round 1 under a generous budget proceeds past
    design — the cap must not fire on the happy path (guards charge()
    off-by-one)."""
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", "120")
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", _charging_run)

    def _review(*_a, **_kw) -> SpecCritique:
        charge_active_budget()
        return SpecCritique(ready=True, rationale="ok")

    monkeypatch.setattr(orch.design_review_agent, "run", _review)
    monkeypatch.setattr(orch.design_agent, "revise", _charging_run)
    _force_synthesis_skip(monkeypatch, orch, _VALID_CODE)
    _short_circuit_synthesis(monkeypatch)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert record.backtest.status != "failed: budget_exhausted"
    assert record.design_rounds == 1


def test_design_max_llm_calls_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_design_max_llm_calls`` defaults to 120, parses overrides, floors
    sub-1 to 1, and falls back to 120 on garbage."""
    from investment_team.strategy_lab.orchestrator import _design_max_llm_calls

    monkeypatch.delenv("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", raising=False)
    assert _design_max_llm_calls() == 120

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", "50")
    assert _design_max_llm_calls() == 50

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", "0")
    assert _design_max_llm_calls() == 1

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", "-9")
    assert _design_max_llm_calls() == 1

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", "garbage")
    assert _design_max_llm_calls() == 120


# ---------------------------------------------------------------------------
# Stall detection + regression guard + telemetry (critique-ledger work)
# ---------------------------------------------------------------------------


def test_design_review_stall_rounds_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_design_review_stall_rounds`` defaults to 3, parses overrides, floors
    sub-1 to 1, and falls back to 3 on garbage."""
    from investment_team.strategy_lab.orchestrator import _design_review_stall_rounds

    monkeypatch.delenv("STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS", raising=False)
    assert _design_review_stall_rounds() == 3

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS", "5")
    assert _design_review_stall_rounds() == 5

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS", "0")
    assert _design_review_stall_rounds() == 1

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS", "garbage")
    assert _design_review_stall_rounds() == 3


def test_stall_short_circuits_before_round_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reviewer returns the SAME blocking issue every round → the within-loop
    stall guard short-circuits with ``status="failed: design_stalled"`` before
    the (much larger) round cap, and ``revise`` is called fewer than cap-1
    times."""
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "20")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS", "3")
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted"))
    monkeypatch.setattr(
        orch.design_review_agent,
        "run",
        lambda *a, **kw: SpecCritique(
            ready=False,
            rationale="same issue every round",
            issues=[CritiqueIssue(field="hypothesis", description="thesis is vague")],
        ),
    )

    revise_counter = {"n": 0}

    def _revise(*_a, **_kw):
        revise_counter["n"] += 1
        return _spec_dict(), "revised"

    monkeypatch.setattr(orch.design_agent, "revise", _revise)

    def _market_must_not_run(self, *_a, **_kw):
        raise AssertionError("synthesis loop must not be entered on a stall")

    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _market_must_not_run)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert record.backtest.status == "failed: design_stalled"
    assert record.is_winning is False
    # Stall trips at the 3rd identical round (0-indexed round 2) → 3 critiques,
    # well below the cap of 20, and revise ran only on the two pre-stall rounds.
    assert record.design_rounds == 3
    assert revise_counter["n"] == 2
    assert record.loop_telemetry["stop_reason"] == "stalled"
    ar = record.backtest.result.acceptance_reason or ""
    assert "design_stalled" in ar


def test_stall_threshold_equal_to_round_cap_reports_round_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the stall threshold equals the round cap and the same issue stays
    open, the final allowed round consumes the full configured budget rather
    than aborting early — so it must report ``design_not_ready`` / ``round_cap``,
    not ``design_stalled``."""
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "3")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS", "3")
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted"))
    monkeypatch.setattr(
        orch.design_review_agent,
        "run",
        lambda *a, **kw: SpecCritique(
            ready=False,
            rationale="same issue every round",
            issues=[CritiqueIssue(field="hypothesis", description="thesis is vague")],
        ),
    )
    monkeypatch.setattr(orch.design_agent, "revise", lambda *_a, **_kw: (_spec_dict(), "revised"))

    def _market_must_not_run(self, *_a, **_kw):
        raise AssertionError("synthesis loop must not be entered")

    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _market_must_not_run)

    record = orch.run_cycle(prior_records=[], config=_config())

    # The loop ran the full cap (no rounds were saved), so this is honest
    # round-cap exhaustion, not an early stall abort.
    assert record.backtest.status == "failed: design_not_ready"
    assert record.design_rounds == 3
    assert record.loop_telemetry["stop_reason"] == "round_cap"


def test_regression_notice_passed_to_revise(monkeypatch: pytest.MonkeyPatch) -> None:
    """An issue resolved on an earlier round that reappears later is surfaced to
    ``DesignAgent.revise`` via a non-empty ``regression_notice`` naming it."""
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "5")
    # Keep stall detection out of the way for this 3-round scenario.
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS", "10")
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted"))

    issue_x = CritiqueIssue(field="exit_rules", description="missing take-profit")
    issue_y = CritiqueIssue(field="sizing", description="position too large")
    review_calls = iter(
        [
            # round 0: raise X
            SpecCritique(ready=False, rationale="r0", issues=[issue_x]),
            # round 1: X resolved, raise Y instead
            SpecCritique(ready=False, rationale="r1", issues=[issue_y]),
            # round 2: X reappears → regression
            SpecCritique(ready=False, rationale="r2", issues=[issue_x]),
            # round 3: ready (so the loop ends cleanly)
            SpecCritique(ready=True, rationale="r3 ok"),
        ]
    )
    monkeypatch.setattr(orch.design_review_agent, "run", lambda *a, **kw: next(review_calls))

    notices: list = []

    def _revise(_spec, _critique, *, prior_critiques=None, regression_notice="", **_kw):
        notices.append(regression_notice)
        return _spec_dict(), "revised"

    monkeypatch.setattr(orch.design_agent, "revise", _revise)
    _force_synthesis_skip(monkeypatch, orch, _VALID_CODE)
    _short_circuit_synthesis(monkeypatch)

    record = orch.run_cycle(prior_records=[], config=_config())

    # revise is called after rounds 0, 1, 2 (round 3 readies → no revise).
    assert len(notices) == 3
    # Rounds 0 and 1 had no regression; round 2 reintroduced X.
    assert notices[0] == ""
    assert notices[1] == ""
    assert "missing take-profit" in notices[2]
    assert record.loop_telemetry["critique_ledger"]["total_regressed"] == 1


def test_revise_receives_accumulating_prior_critiques(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator hands ``DesignAgent.revise`` the *accumulating* external
    critique lineage each round (current critique included).

    Combined with the DesignAgent-level test that ``_with_self_review`` threads
    that lineage into the internal self-revision prompt, this pins that
    prior-round fixes stay in context across rounds — the upstream half of the
    no-regression guarantee. The lineage grows by one each round and is what the
    self-revision uses to avoid undoing an earlier round's fix.
    """
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "5")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS", "10")
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted"))

    review_calls = iter(
        [
            SpecCritique(
                ready=False,
                rationale="r0",
                issues=[CritiqueIssue(field="exit_rules", description="add take_profit")],
            ),
            SpecCritique(
                ready=False,
                rationale="r1",
                issues=[CritiqueIssue(field="sizing", description="too aggressive")],
            ),
            SpecCritique(ready=True, rationale="r2 ok"),
        ]
    )
    monkeypatch.setattr(orch.design_review_agent, "run", lambda *a, **kw: next(review_calls))

    seen_lineage_lengths: List[int] = []

    def _revise(_spec, _critique, *, prior_critiques=None, regression_notice="", **_kw):
        seen_lineage_lengths.append(len(prior_critiques or []))
        return _spec_dict(), "revised"

    monkeypatch.setattr(orch.design_agent, "revise", _revise)
    _force_synthesis_skip(monkeypatch, orch, _VALID_CODE)
    _short_circuit_synthesis(monkeypatch)

    orch.run_cycle(prior_records=[], config=_config())

    # revise fires after rounds 0 and 1 (round 2 readies → no revise); the
    # lineage accumulates and includes the just-recorded critique each round.
    assert seen_lineage_lengths == [1, 2]


def test_loop_telemetry_persisted_on_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal N-rounds-then-pass cycle persists a ``loop_telemetry`` summary
    with the round count, a ``ready`` stop reason, gate histograms, and the
    compiled-vs-custom flag."""
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted"))
    review_calls = iter(
        [
            SpecCritique(
                ready=False,
                rationale="r0",
                issues=[CritiqueIssue(field="exit_rules", description="add tp")],
            ),
            SpecCritique(ready=True, rationale="r1 ok"),
        ]
    )
    monkeypatch.setattr(orch.design_review_agent, "run", lambda *a, **kw: next(review_calls))
    monkeypatch.setattr(orch.design_agent, "revise", lambda *a, **kw: (_spec_dict(), "revised"))
    _force_synthesis_skip(monkeypatch, orch, _VALID_CODE)
    _short_circuit_synthesis(monkeypatch)

    record = orch.run_cycle(prior_records=[], config=_config())

    telemetry = record.loop_telemetry
    assert telemetry["design_review_rounds"] == 2
    assert telemetry["stop_reason"] == "ready"
    assert telemetry["critique_ledger"]["total_resolved"] == 1
    assert telemetry["requires_custom_code"] is False
    # Code was synthesized (compiled path), so code_path reflects that — not
    # the "not_synthesized" state reserved for pre-synthesis short-circuits.
    assert telemetry["code_path"] == "compiled"
    # Gate histograms are present (readiness gate ran at least once).
    assert isinstance(telemetry["gate_pass_counts"], dict)
    assert isinstance(telemetry["gate_fail_counts"], dict)


def test_telemetry_events_emitted_on_phase_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop emits ``telemetry`` events on the ``on_phase`` callback: one per
    design-review round plus a design-loop summary at exit."""
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted"))
    review_calls = iter(
        [
            SpecCritique(
                ready=False,
                rationale="r0",
                issues=[CritiqueIssue(field="exit_rules", description="add tp")],
            ),
            SpecCritique(ready=True, rationale="r1 ok"),
        ]
    )
    monkeypatch.setattr(orch.design_review_agent, "run", lambda *a, **kw: next(review_calls))
    monkeypatch.setattr(orch.design_agent, "revise", lambda *a, **kw: (_spec_dict(), "revised"))
    _force_synthesis_skip(monkeypatch, orch, _VALID_CODE)
    _short_circuit_synthesis(monkeypatch)

    events: list = []

    def _on_phase(phase: str, data: dict) -> None:
        if phase == "telemetry":
            events.append(data)

    orch.run_cycle(prior_records=[], config=_config(), on_phase=_on_phase)

    scopes = [e.get("scope") for e in events]
    assert scopes.count("design_review_round") == 2
    assert "design_loop" in scopes
    summary = next(e for e in events if e.get("scope") == "design_loop")
    assert summary["design_review_rounds"] == 2
    assert summary["stop_reason"] == "ready"


# ---------------------------------------------------------------------------
# Pure-helper unit coverage (regression notice + telemetry assembly)
# ---------------------------------------------------------------------------


def test_format_regression_notice_empty_when_no_regression() -> None:
    from investment_team.strategy_lab.orchestrator import _format_regression_notice

    critique = SpecCritique(
        ready=False, issues=[CritiqueIssue(field="sizing", description="too big")]
    )
    assert _format_regression_notice(critique, set()) == ""


def test_format_regression_notice_lists_matching_issue() -> None:
    from investment_team.strategy_lab.agents.design_review import compute_issue_id
    from investment_team.strategy_lab.orchestrator import _format_regression_notice

    issue = CritiqueIssue(field="exit_rules", description="missing take-profit")
    critique = SpecCritique(ready=False, issues=[issue])
    notice = _format_regression_notice(critique, {issue.issue_id})
    assert "missing take-profit" in notice
    assert issue.issue_id in notice
    # Sanity: the id is the deterministic one.
    assert issue.issue_id == compute_issue_id("exit_rules", "missing take-profit")


def test_format_regression_notice_defensive_bare_id_branch() -> None:
    """A regressed id with no matching issue object still surfaces as a bare id."""
    from investment_team.strategy_lab.orchestrator import _format_regression_notice

    critique = SpecCritique(
        ready=False, issues=[CritiqueIssue(field="sizing", description="too big")]
    )
    notice = _format_regression_notice(critique, {"exit_rules:deadbeef00"})
    assert "exit_rules:deadbeef00" in notice


def test_design_loop_telemetry_summary_shape() -> None:
    from investment_team.strategy_lab.agents.design_review import CritiqueLedger
    from investment_team.strategy_lab.orchestrator import _design_loop_telemetry_summary

    led = CritiqueLedger()
    led.record_round(
        SpecCritique(ready=False, issues=[CritiqueIssue(field="sizing", description="too big")])
    )
    summary = _design_loop_telemetry_summary(led, rounds=1, stop_reason="round_cap")
    assert summary["design_review_rounds"] == 1
    assert summary["stop_reason"] == "round_cap"
    assert summary["critique_ledger"]["final_open_count"] == 1


def test_finalize_loop_telemetry_merges_gate_counts() -> None:
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext
    from investment_team.strategy_lab.orchestrator import _finalize_loop_telemetry

    ctx = _DesignPersistContext(
        rounds=2,
        critiques=[],
        stop_reason="ready",
        loop_telemetry={"design_review_rounds": 2, "stop_reason": "ready"},
    )
    gates = [
        QualityGateResult(
            gate_name="spec_readiness", passed=True, severity="info", phase="design", details="ok"
        ),
        QualityGateResult(
            gate_name="spec_readiness",
            passed=False,
            severity="critical",
            phase="design",
            details="bad",
        ),
    ]

    class _Spec:
        requires_custom_code = True

    # With synthesized code, code_path follows requires_custom_code.
    telemetry = _finalize_loop_telemetry(ctx, gates, _Spec(), code="def on_bar(): ...")
    assert telemetry["design_review_rounds"] == 2
    assert telemetry["gate_pass_counts"] == {"spec_readiness": 1}
    assert telemetry["gate_fail_counts"] == {"spec_readiness": 1}
    assert telemetry["requires_custom_code"] is True
    assert telemetry["code_path"] == "custom"


def test_finalize_loop_telemetry_marks_unsynthesized_failures() -> None:
    """A pre-synthesis short-circuit (no code) is code_path='not_synthesized',
    not 'compiled' — even though requires_custom_code defaults to False — so the
    funnel metric does not miscount design failures as compiled."""
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext
    from investment_team.strategy_lab.orchestrator import _finalize_loop_telemetry

    ctx = _DesignPersistContext(
        rounds=3,
        critiques=[],
        stop_reason="design_not_ready",
        loop_telemetry={"design_review_rounds": 3, "stop_reason": "round_cap"},
    )

    class _Spec:
        requires_custom_code = False

    telemetry = _finalize_loop_telemetry(ctx, [], _Spec(), code="")
    assert telemetry["code_path"] == "not_synthesized"
    # Empty/whitespace code is also treated as not synthesized.
    assert _finalize_loop_telemetry(ctx, [], _Spec(), code="   \n")["code_path"] == (
        "not_synthesized"
    )
