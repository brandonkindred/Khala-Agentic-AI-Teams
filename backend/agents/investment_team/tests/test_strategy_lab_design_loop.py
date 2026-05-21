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
    """Reviewer always returns ``ready=False`` → cycle short-circuits with
    ``status="failed: design_not_ready"`` and never enters the synthesis
    loop (market data is never fetched)."""
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "3")
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict(), "scripted"))
    monkeypatch.setattr(
        orch.design_review_agent,
        "run",
        lambda *a, **kw: SpecCritique(
            ready=False,
            rationale="incoherent",
            issues=[CritiqueIssue(field="hypothesis", description="vague")],
        ),
    )
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
    assert _design_review_rounds() == 5  # falls back to default

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "7")
    assert _design_review_rounds() == 7
