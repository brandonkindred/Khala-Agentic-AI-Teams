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
from investment_team.strategy_lab_context import PROMPT_ASSET_CLASSES

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
# Mechanical-repair pre-flight
# ---------------------------------------------------------------------------


# Rule 7 (intraday timeframe with no data) only fires on non-equity classes,
# so ``_mechanical_spec_dict`` must declare forex. Each design attempt is
# pinned to one randomly-drawn allowed category, so every run_cycle feeding
# this payload must restrict the run to forex — otherwise the payload's
# asset_class contradicts the pin and readiness Rule 11 (correctly) rejects it
# before the mechanical repairs under test are ever reached.
_EXCLUDE_ALL_BUT_FOREX = [c for c in PROMPT_ASSET_CLASSES if c != "forex"]


def _mechanical_spec_dict() -> Dict[str, Any]:
    """A spec whose *only* readiness criticals are mechanical: an intraday
    timeframe on forex (Rule 7) and an over-ceiling position cap (Rule 8)."""
    d = _spec_dict()
    d.update(
        {
            "asset_class": "forex",
            "timeframe": "1h",
            "target_symbols": ["EURUSD=X"],
            "hypothesis": "RSI mean reversion on FX",
            "risk_limits": {"max_position_pct": 40, "max_drawdown_pct": 10},
        }
    )
    return d


def test_mechanical_only_spec_reaches_ready_with_zero_revise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec with only mechanical violations is auto-repaired before any LLM
    revise round: the reviewer runs once and readies, ``DesignAgent.revise`` is
    never called, and a ``design_repair`` telemetry event records the edits."""
    from investment_team.strategy_lab import mechanical_repair as mech

    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(
        orch.design_agent, "run", lambda **_kw: (_mechanical_spec_dict(), "scripted")
    )
    review_calls = {"n": 0}

    def _review(*_a, **_kw) -> SpecCritique:
        review_calls["n"] += 1
        return SpecCritique(ready=True, rationale="ok")

    monkeypatch.setattr(orch.design_review_agent, "run", _review)

    revise_calls = {"n": 0}

    def _revise(*_a, **_kw) -> Tuple[Dict[str, Any], str]:
        revise_calls["n"] += 1
        return _mechanical_spec_dict(), "should-not-be-used"

    monkeypatch.setattr(orch.design_agent, "revise", _revise)
    # Keep the pre-flight trial compile deterministic and the synthesis phase cheap.
    monkeypatch.setattr(mech, "compile_strategy", lambda _spec: _VALID_CODE)
    _force_synthesis_skip(monkeypatch, orch, _VALID_CODE)
    _short_circuit_synthesis(monkeypatch)

    events: list = []
    record = orch.run_cycle(
        exclude_asset_classes=_EXCLUDE_ALL_BUT_FOREX,
        prior_records=[],
        config=_config(),
        on_phase=lambda phase, data: events.append((phase, data)),
    )

    # The acceptance criterion: zero LLM revise rounds for a mechanical-only spec.
    assert revise_calls["n"] == 0
    assert review_calls["n"] == 1
    assert record.design_rounds == 1
    # The repair edits were recorded on the callback and in the telemetry summary.
    repair_events = [d for p, d in events if p == "design_repair"]
    assert len(repair_events) == 1
    repaired_rules = {a["rule"] for a in repair_events[0]["actions"]}
    assert {"timeframe_data_availability", "max_position_pct_cap"} <= repaired_rules
    assert repair_events[0]["now_ready"] is True
    assert record.loop_telemetry["mechanical_repairs"] >= 2


def test_mechanical_repair_then_substantive_critical_still_revises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a non-mechanical critical remains after repair (empty entry rules,
    Rule 2), the loop still falls through to the LLM revise path — repair only
    short-circuits the rounds it can fully resolve."""
    from investment_team.strategy_lab import mechanical_repair as mech

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "2")
    orch = StrategyLabOrchestrator()

    def _spec_with_substantive_defect() -> Dict[str, Any]:
        d = _mechanical_spec_dict()
        d["entry_rules"] = []  # Rule 2 critical — the machine cannot invent entries.
        return d

    monkeypatch.setattr(
        orch.design_agent, "run", lambda **_kw: (_spec_with_substantive_defect(), "scripted")
    )

    review_calls = {"n": 0}

    def _review(*_a, **_kw) -> SpecCritique:
        review_calls["n"] += 1
        return SpecCritique(ready=True, rationale="never reached")

    monkeypatch.setattr(orch.design_review_agent, "run", _review)

    revise_calls = {"n": 0}

    def _revise(*_a, **_kw) -> Tuple[Dict[str, Any], str]:
        revise_calls["n"] += 1
        return _spec_with_substantive_defect(), "revised"

    monkeypatch.setattr(orch.design_agent, "revise", _revise)
    monkeypatch.setattr(mech, "compile_strategy", lambda _spec: _VALID_CODE)
    _short_circuit_synthesis(monkeypatch)

    events: list = []
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        on_phase=lambda phase, data: events.append((phase, data)),
    )

    # The remaining (non-mechanical) critical keeps the reviewer skipped and
    # forces at least one LLM revise round.
    assert review_calls["n"] == 0
    assert revise_calls["n"] >= 1
    # The mechanical part was still repaired each round.
    assert any(p == "design_repair" for p, _ in events)
    assert record.backtest.status == "failed: design_not_ready"


# ---------------------------------------------------------------------------
# skip_self_review threading (#6930)
#
# ``deterministic_ready`` describes the round's *incoming* spec — the one the
# reviewer just found insufficient — not the spec ``DesignAgent.revise`` is
# about to produce in response to that critique, so it cannot predict whether
# the rewrite is clean. Self-review's purpose is exactly to catch a
# contradiction the designer introduces while addressing critique, so it must
# never be skipped on a real revise call regardless of the incoming spec's
# readiness state; the three scenarios below (clean incoming spec, mechanical
# repair fired, readiness failed outright) all assert the same invariant.
# ---------------------------------------------------------------------------


def test_revise_always_requests_self_review_when_round_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deterministic ``SpecReadinessGate`` passing (no critical, no
    mechanical repair fired) is a distinct thing from the *round* being
    ready: a readiness-clean spec still reaches the LLM reviewer, which can
    independently decline it on design-quality grounds — exactly what this
    test's stubbed ``design_review_agent.run`` does on round 0
    (``SpecCritique(ready=False, ...)``), triggering the real ``revise``
    call under test. ``_run_design_review_rounds`` calls it with
    ``skip_self_review=False`` regardless: the incoming spec's readiness-gate
    cleanliness says nothing about whether the LLM's upcoming revision will
    be clean."""
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted"))
    review_calls = iter(
        [
            SpecCritique(
                ready=False,
                rationale="round-0",
                issues=[CritiqueIssue(field="exit_rules", description="add take_profit")],
            ),
            SpecCritique(ready=True, rationale="round-1 ok"),
        ]
    )
    monkeypatch.setattr(orch.design_review_agent, "run", lambda *a, **kw: next(review_calls))

    revise_calls: List[Dict[str, Any]] = []

    def _revise(*_a, **kwargs) -> Tuple[Dict[str, Any], str]:
        revise_calls.append(kwargs)
        return _spec_dict(), "revised"

    monkeypatch.setattr(orch.design_agent, "revise", _revise)
    _force_synthesis_skip(monkeypatch, orch, _VALID_CODE)
    _short_circuit_synthesis(monkeypatch)

    orch.run_cycle(prior_records=[], config=_config())

    assert len(revise_calls) == 1
    assert revise_calls[0]["skip_self_review"] is False


def test_revise_does_not_skip_self_review_when_mechanical_repair_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When mechanical repair fires this round — even if the spec ends up
    readiness-clean afterward — ``skip_self_review=False`` is passed so the
    designer's self-review still runs on the resulting revision."""
    from investment_team.strategy_lab import mechanical_repair as mech

    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(
        orch.design_agent, "run", lambda **_kw: (_mechanical_spec_dict(), "scripted")
    )
    review_calls = iter(
        [
            SpecCritique(
                ready=False,
                rationale="not ready yet",
                issues=[CritiqueIssue(field="hypothesis", description="tighten")],
            ),
            SpecCritique(ready=True, rationale="ok"),
        ]
    )
    monkeypatch.setattr(orch.design_review_agent, "run", lambda *a, **kw: next(review_calls))

    revise_calls: List[Dict[str, Any]] = []

    def _revise(*_a, **kwargs) -> Tuple[Dict[str, Any], str]:
        revise_calls.append(kwargs)
        return _mechanical_spec_dict(), "revised"

    monkeypatch.setattr(orch.design_agent, "revise", _revise)
    monkeypatch.setattr(mech, "compile_strategy", lambda _spec: _VALID_CODE)
    _force_synthesis_skip(monkeypatch, orch, _VALID_CODE)
    _short_circuit_synthesis(monkeypatch)

    orch.run_cycle(prior_records=[], config=_config(), exclude_asset_classes=_EXCLUDE_ALL_BUT_FOREX)

    assert len(revise_calls) == 1
    assert revise_calls[0]["skip_self_review"] is False


def test_revise_does_not_skip_self_review_when_readiness_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the round's readiness gate does not pass (and mechanical repair
    cannot fix it), ``skip_self_review=False`` is passed as well."""
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "2")
    orch = StrategyLabOrchestrator()

    def _spec_with_substantive_defect() -> Dict[str, Any]:
        d = _spec_dict()
        d["entry_rules"] = []  # Rule 2 critical — not mechanically repairable.
        return d

    monkeypatch.setattr(
        orch.design_agent, "run", lambda **_kw: (_spec_with_substantive_defect(), "scripted")
    )
    monkeypatch.setattr(
        orch.design_review_agent,
        "run",
        lambda *_a, **_kw: SpecCritique(ready=True, rationale="never reached"),
    )

    revise_calls: List[Dict[str, Any]] = []

    def _revise(*_a, **kwargs) -> Tuple[Dict[str, Any], str]:
        revise_calls.append(kwargs)
        return _spec_with_substantive_defect(), "revised"

    monkeypatch.setattr(orch.design_agent, "revise", _revise)
    _short_circuit_synthesis(monkeypatch)

    orch.run_cycle(prior_records=[], config=_config())

    assert len(revise_calls) >= 1
    assert revise_calls[0]["skip_self_review"] is False


def test_mechanical_repair_toggle_off_skips_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``STRATEGY_LAB_MECHANICAL_REPAIR_ENABLED=false`` the pre-flight is
    skipped: a mechanical-only critical falls straight through to the LLM revise
    path and no ``design_repair`` event is emitted."""
    monkeypatch.setenv("STRATEGY_LAB_MECHANICAL_REPAIR_ENABLED", "false")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "2")
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(
        orch.design_agent, "run", lambda **_kw: (_mechanical_spec_dict(), "scripted")
    )
    monkeypatch.setattr(orch.design_review_agent, "run", lambda *a, **kw: SpecCritique(ready=True))

    revise_calls = {"n": 0}

    def _revise(*_a, **_kw) -> Tuple[Dict[str, Any], str]:
        revise_calls["n"] += 1
        return _mechanical_spec_dict(), "revised"

    monkeypatch.setattr(orch.design_agent, "revise", _revise)
    _short_circuit_synthesis(monkeypatch)

    events: list = []
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        on_phase=lambda phase, data: events.append((phase, data)),
    )

    assert revise_calls["n"] >= 1
    assert not any(p == "design_repair" for p, _ in events)
    assert record.loop_telemetry["mechanical_repairs"] == 0


def test_trial_compile_runs_on_readiness_clean_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spec that *passes* readiness but is outside the deterministic-compiler
    envelope has the custom-code path selected during the pre-flight — not
    deferred to synthesis. ``requires_custom_code`` is flipped, a
    ``design_repair`` event fires, and the reviewer still readies the spec with
    zero LLM revise rounds."""
    from investment_team.strategy_lab import mechanical_repair as mech
    from investment_team.strategy_lab.synthesis import CompilerError

    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted"))

    review_calls = {"n": 0}

    def _review(*_a, **_kw) -> SpecCritique:
        review_calls["n"] += 1
        return SpecCritique(ready=True, rationale="ok")

    monkeypatch.setattr(orch.design_review_agent, "run", _review)

    revise_calls = {"n": 0}

    def _revise(*_a, **_kw) -> Tuple[Dict[str, Any], str]:
        revise_calls["n"] += 1
        return _spec_dict(), "should-not-be-used"

    monkeypatch.setattr(orch.design_agent, "revise", _revise)

    # _spec_dict() passes readiness (stocks/1d/cap5); force the pre-flight trial
    # compile to reject it so the custom-code fallback fires on a clean-readiness
    # spec. The custom-code path then routes through code_synthesis_agent.
    def _compile_rejects(_spec: Any) -> str:
        raise CompilerError("outside compiler envelope")

    monkeypatch.setattr(mech, "compile_strategy", _compile_rejects)
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: _VALID_CODE)
    _short_circuit_synthesis(monkeypatch)

    events: list = []
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        on_phase=lambda phase, data: events.append((phase, data)),
    )

    assert revise_calls["n"] == 0
    assert review_calls["n"] == 1
    repair_events = [d for p, d in events if p == "design_repair"]
    assert len(repair_events) == 1
    assert any(a["rule"] == "compiler_fallback" for a in repair_events[0]["actions"])
    # The custom-code decision was surfaced during design, not in synthesis.
    assert record.strategy.requires_custom_code is True


def test_preflight_demotes_overelected_custom_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spec the LLM flagged ``requires_custom_code=True`` but which compiles
    cleanly is over-elected onto the drift-prone custom path. The pre-flight
    trial-compile demotes it back to the faithful compiled path: a
    ``compiler_demote`` design_repair fires and the persisted spec carries
    ``requires_custom_code=False``."""
    orch = StrategyLabOrchestrator()

    overelected = {**_spec_dict(), "requires_custom_code": True}
    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (overelected, "scripted"))
    monkeypatch.setattr(
        orch.design_review_agent, "run", lambda *a, **kw: SpecCritique(ready=True, rationale="ok")
    )

    revise_calls = {"n": 0}

    def _revise(*_a, **_kw) -> Tuple[Dict[str, Any], str]:
        revise_calls["n"] += 1
        return overelected, "should-not-be-used"

    monkeypatch.setattr(orch.design_agent, "revise", _revise)
    # Real compiler decides demotion (the RSI spec compiles); stub only the
    # downstream synthesis + market-data so the cycle completes cheaply.
    _force_synthesis_skip(monkeypatch, orch, _VALID_CODE)
    _short_circuit_synthesis(monkeypatch)

    events: list = []
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        on_phase=lambda phase, data: events.append((phase, data)),
    )

    assert revise_calls["n"] == 0
    repair_events = [d for p, d in events if p == "design_repair"]
    assert len(repair_events) == 1
    assert any(a["rule"] == "compiler_demote" for a in repair_events[0]["actions"])
    # Demoted to the faithful compiled path.
    assert record.strategy.requires_custom_code is False


def test_preflight_demotion_toggle_off_keeps_custom_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``STRATEGY_LAB_DEMOTE_COMPILABLE_CUSTOM_CODE`` disabled, an
    over-elected compilable spec is left on the custom path — the escape hatch
    for a lossy-but-compilable spec an operator wants to keep as custom code."""
    monkeypatch.setenv("STRATEGY_LAB_DEMOTE_COMPILABLE_CUSTOM_CODE", "false")
    orch = StrategyLabOrchestrator()

    overelected = {**_spec_dict(), "requires_custom_code": True}
    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (overelected, "scripted"))
    monkeypatch.setattr(
        orch.design_review_agent, "run", lambda *a, **kw: SpecCritique(ready=True, rationale="ok")
    )
    monkeypatch.setattr(orch.design_agent, "revise", lambda *a, **kw: (overelected, "x"))
    _force_synthesis_skip(monkeypatch, orch, _VALID_CODE)
    _short_circuit_synthesis(monkeypatch)

    events: list = []
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        on_phase=lambda phase, data: events.append((phase, data)),
    )

    repair_events = [d for p, d in events if p == "design_repair"]
    assert not any(a["rule"] == "compiler_demote" for d in repair_events for a in d["actions"])
    assert record.strategy.requires_custom_code is True


def test_budget_exhaustion_after_repair_preserves_repaired_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the budget trips on the review call that follows a mechanical repair,
    the short-circuit record must carry the *repaired* spec (the one readiness
    was revalidated against and a ``design_repair`` event was emitted for), not
    the pre-loop draft."""
    from investment_team.strategy_lab import mechanical_repair as mech

    # Budget 1: design_agent.run charges 1; the round-0 review charge trips.
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", "1")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "10")
    orch = StrategyLabOrchestrator()

    def _run(**_kw) -> Tuple[Dict[str, Any], str]:
        charge_active_budget()
        return _mechanical_spec_dict(), "scripted"

    monkeypatch.setattr(orch.design_agent, "run", _run)

    def _review(*_a, **_kw) -> SpecCritique:
        charge_active_budget()  # trips the budget after the repair
        return SpecCritique(ready=True)

    monkeypatch.setattr(orch.design_review_agent, "run", _review)
    monkeypatch.setattr(
        orch.design_agent, "revise", lambda *a, **kw: (_mechanical_spec_dict(), "x")
    )
    monkeypatch.setattr(mech, "compile_strategy", lambda _spec: _VALID_CODE)

    def _market_must_not_run(self, *_a, **_kw):
        raise AssertionError("synthesis must not run on budget exhaustion")

    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _market_must_not_run)

    record = orch.run_cycle(prior_records=[], config=_config(), exclude_asset_classes=_EXCLUDE_ALL_BUT_FOREX)

    assert record.backtest.status == "failed: budget_exhausted"
    # The repaired spec is preserved (forex/1h/40% draft → 1d/25% after repair).
    assert record.strategy.timeframe == "1d"
    assert record.strategy.risk_limits.max_position_pct == 25.0
    # The mechanical-repair count survives the budget trip (timeframe + cap = 2),
    # rather than defaulting to 0 in the budget-exhaustion telemetry.
    assert record.loop_telemetry["mechanical_repairs"] == 2


def test_trial_compile_skipped_while_readiness_critical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trial compile must not run while a readiness critical remains: a
    malformed-but-readiness-detectable spec can make ``compile_strategy`` raise a
    non-``CompilerError`` (e.g. ``TypeError``), which would abort the loop. With
    the gate the loop instead reaches honest round-cap exhaustion and the
    crashing compiler is never invoked."""
    from investment_team.strategy_lab import mechanical_repair as mech

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "2")
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted"))
    monkeypatch.setattr(orch.design_agent, "revise", lambda *a, **kw: (_spec_dict(), "revised"))
    monkeypatch.setattr(orch.design_review_agent, "run", lambda *a, **kw: SpecCritique(ready=True))

    # A persistent readiness critical keeps deterministic_ready False every round.
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

    # If the trial compile ran on this readiness-critical spec it would crash the
    # whole cycle with a non-CompilerError instead of being skipped.
    def _compile_crashes(_spec: Any) -> str:
        raise TypeError("int() argument must be a string or a number, not 'NoneType'")

    monkeypatch.setattr(mech, "compile_strategy", _compile_crashes)

    def _market_must_not_run(self, *_a, **_kw):
        raise AssertionError("synthesis must not run when design never readies")

    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _market_must_not_run)

    record = orch.run_cycle(prior_records=[], config=_config())

    # The loop completed via the readiness-critique path; the trial compile was
    # skipped, so the crashing compiler was never reached.
    assert record.backtest.status == "failed: design_not_ready"
    assert record.loop_telemetry["mechanical_repairs"] == 0


def test_mechanical_repair_enabled_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_mechanical_repair_enabled`` defaults to True and only the recognised
    truthy tokens enable it; everything else (including garbage) disables."""
    from investment_team.strategy_lab.orchestrator import _mechanical_repair_enabled

    monkeypatch.delenv("STRATEGY_LAB_MECHANICAL_REPAIR_ENABLED", raising=False)
    assert _mechanical_repair_enabled() is True

    for truthy in ("true", "TRUE", "1", "yes", "Yes"):
        monkeypatch.setenv("STRATEGY_LAB_MECHANICAL_REPAIR_ENABLED", truthy)
        assert _mechanical_repair_enabled() is True

    for falsey in ("false", "0", "no", "garbage", ""):
        monkeypatch.setenv("STRATEGY_LAB_MECHANICAL_REPAIR_ENABLED", falsey)
        assert _mechanical_repair_enabled() is False


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
    # The non-conforming flag is present on the telemetry and mirrored on the
    # record; a clean compiled cycle never ran on demoted code.
    assert telemetry["ran_on_non_conforming_code"] is False
    assert record.ran_on_non_conforming_code is False


def test_record_ran_on_non_conforming_flag_round_trips() -> None:
    """The top-level record field carries (and defaults) the non-conforming flag."""
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        StrategyLabRecord,
        StrategySpec,
    )
    from investment_team.trade_simulator import compute_metrics

    spec = StrategySpec(
        strategy_id="s1",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="sig",
        timeframe="1d",
        requires_custom_code=True,
    )
    config = BacktestConfig(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )
    backtest = BacktestRecord(
        backtest_id="bt-1",
        strategy_id=spec.strategy_id,
        strategy=spec,
        config=config,
        submitted_by="test",
        submitted_at="2026-06-02T00:00:00+00:00",
        completed_at="2026-06-02T00:00:00+00:00",
        status="completed",
        result=compute_metrics([], config.initial_capital, config.start_date, config.end_date),
        trades=[],
    )

    flagged = StrategyLabRecord(
        lab_record_id="lab-1",
        strategy=spec,
        backtest=backtest,
        is_winning=False,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2026-06-02T00:00:00+00:00",
        loop_telemetry={"ran_on_non_conforming_code": True},
        ran_on_non_conforming_code=True,
    )
    assert flagged.ran_on_non_conforming_code is True

    # Legacy / default construction leaves the flag False.
    default = StrategyLabRecord(
        lab_record_id="lab-2",
        strategy=spec,
        backtest=backtest,
        is_winning=False,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2026-06-02T00:00:00+00:00",
    )
    assert default.ran_on_non_conforming_code is False


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


def _non_conforming_ctx():
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext

    return _DesignPersistContext(
        rounds=1,
        critiques=[],
        stop_reason="ready",
        loop_telemetry={"design_review_rounds": 1, "stop_reason": "ready"},
    )


class _CustomSpec:
    requires_custom_code = True


def _pc_result(*, passed: bool, severity: str, details: str) -> QualityGateResult:
    return QualityGateResult(
        gate_name="predicate_conformance",
        passed=passed,
        severity=severity,
        phase="synthesis",
        details=details,
    )


class TestRoundDemotedConformance:
    """`_round_demoted_conformance` attributes the verdict to a single round."""

    def test_demoted_warning_is_true(self) -> None:
        from investment_team.strategy_lab.orchestrator import _round_demoted_conformance

        demoted = _pc_result(
            passed=False,
            severity="warning",
            details="rule_id=entry[0]: predicate conformance failed.",
        )
        assert _round_demoted_conformance([demoted]) is True

    def test_critical_is_false(self) -> None:
        from investment_team.strategy_lab.orchestrator import _round_demoted_conformance

        critical = _pc_result(
            passed=False,
            severity="critical",
            details="rule_id=entry[0]: predicate conformance failed.",
        )
        assert _round_demoted_conformance([critical]) is False

    def test_unsynthesizable_warning_is_false(self) -> None:
        from investment_team.strategy_lab.orchestrator import _round_demoted_conformance

        unsynth = _pc_result(
            passed=False,
            severity="warning",
            details="Fixture unsynthesizable: no forcing sequence",
        )
        assert _round_demoted_conformance([unsynth]) is False

    def test_passing_conformance_is_false(self) -> None:
        from investment_team.strategy_lab.orchestrator import _round_demoted_conformance

        ok = _pc_result(
            passed=True, severity="info", details="Predicate conformance OK (60 bars checked)."
        )
        assert _round_demoted_conformance([ok]) is False

    def test_no_conformance_gate_is_false(self) -> None:
        from investment_team.strategy_lab.orchestrator import _round_demoted_conformance

        other = QualityGateResult(
            gate_name="code_conformance",
            passed=True,
            severity="info",
            phase="synthesis",
            details="ok",
        )
        assert _round_demoted_conformance([other]) is False


def test_finalize_loop_telemetry_stores_non_conforming_flag() -> None:
    """`_finalize_loop_telemetry` records the loop-captured flag verbatim."""
    from investment_team.strategy_lab.orchestrator import _finalize_loop_telemetry

    flagged = _finalize_loop_telemetry(
        _non_conforming_ctx(),
        [],
        _CustomSpec(),
        code="def on_bar(): ...",
        ran_on_non_conforming_code=True,
    )
    assert flagged["ran_on_non_conforming_code"] is True

    # Default (e.g. short-circuit records) is False.
    default = _finalize_loop_telemetry(
        _non_conforming_ctx(), [], _CustomSpec(), code="def on_bar(): ..."
    )
    assert default["ran_on_non_conforming_code"] is False


def test_readiness_sizing_coherence_critical_stalls_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real Rule 9 (Check A) critical — sizing.fraction > max_position_pct —
    drives the deterministic synthetic-critique path and, when the reviser
    keeps returning the same incoherent spec, the existing stall guard
    short-circuits with ``status="failed: design_stalled"`` instead of
    churning to the round cap. The LLM reviewer is never consulted (the
    deterministic gate already failed the round)."""
    from investment_team.strategy_lab.spec_dsl import FixedFractionSizing

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "20")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS", "3")
    orch = StrategyLabOrchestrator()

    incoherent = _spec_dict()
    incoherent["sizing"] = FixedFractionSizing(fraction=0.10).model_dump()
    incoherent["risk_limits"] = {"max_position_pct": 5, "max_drawdown_pct": 10}

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (dict(incoherent), "scripted"))

    def _reviewer_must_not_run(*_a, **_kw):
        raise AssertionError("LLM reviewer must not run when readiness fails critical")

    monkeypatch.setattr(orch.design_review_agent, "run", _reviewer_must_not_run)

    revise_counter = {"n": 0}

    def _revise(*_a, **_kw):
        revise_counter["n"] += 1
        return dict(incoherent), "revised"

    monkeypatch.setattr(orch.design_agent, "revise", _revise)

    def _market_must_not_run(self, *_a, **_kw):
        raise AssertionError("synthesis loop must not be entered on a stall")

    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _market_must_not_run)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert record.backtest.status == "failed: design_stalled"
    assert record.is_winning is False
    assert record.design_rounds == 3
    assert revise_counter["n"] == 2
    assert record.loop_telemetry["stop_reason"] == "stalled"
    # The synthetic critique carried the deterministic sizing-coherence finding.
    assert any(
        "max_position_pct" in str(i.get("description", ""))
        for c in record.critiques
        for i in c.get("issues", [])
    ), record.critiques


def test_design_review_receives_hypothesis_rules_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    """S5b: the design reviewer must be shown the hypothesis/rules consistency
    finding so a narrative/DSL mismatch is reconciled in the design loop. Here the
    hypothesis is about volume/OBV while the rules use RSI — a genuine mismatch —
    so the finding must appear in the reviewer's deterministic findings."""
    orch = StrategyLabOrchestrator()

    mismatched = {**_spec_dict(), "hypothesis": "A volume-driven breakout on rising OBV."}
    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (mismatched, "scripted"))

    captured: Dict[str, Any] = {}

    def _review(spec, findings, prior_critiques=None):
        captured["findings"] = list(findings)
        return SpecCritique(ready=True, rationale="ok")

    monkeypatch.setattr(orch.design_review_agent, "run", _review)
    monkeypatch.setattr(orch.design_agent, "revise", lambda *a, **kw: (mismatched, "x"))
    _force_synthesis_skip(monkeypatch, orch, _VALID_CODE)
    _short_circuit_synthesis(monkeypatch)

    orch.run_cycle(prior_records=[], config=_config())

    details = [getattr(f, "details", "") for f in captured.get("findings", [])]
    assert any("Hypothesis/rules consistency" in d for d in details), details
