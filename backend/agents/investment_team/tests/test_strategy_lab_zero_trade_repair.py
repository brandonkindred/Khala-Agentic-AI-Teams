"""Tests for the Strategy Lab specialized zero-trade repair loop (#405).

The orchestrator's main code-refinement loop now branches on a critical
``backtest_anomaly`` whose diagnostics envelope (issue #404) carries a
deterministic ``zero_trade_category``. Instead of routing straight to
the generic ``RefinementAgent``, the orchestrator first asks
:class:`ZeroTradeRepairAgent` for a targeted fix and, if the proposal
clears code-safety + a fresh backtest + the anomaly gates, commits it
in place. Failed proposals fall through to generic refinement. These
tests exercise :meth:`ZeroTradeRepairer.try_repair`
directly with stubs for the agent and ``run_strategy_code``.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Dict, List, Optional, get_args

import pytest

from investment_team.market_data_service import OHLCVBar
from investment_team.models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    CoverageCategory,
    CoverageReport,
    OpenPositionDiagnostic,
    StrategySpec,
    ZeroTradeCategory,
)
from investment_team.strategy_lab import orchestrator as orchestrator_module
from investment_team.strategy_lab._orchestrator_helpers import _DesignAttemptState
from investment_team.strategy_lab.agents._response_schemas import ZERO_TRADE_REPAIR_SCHEMA
from investment_team.strategy_lab.agents.zero_trade_repair import (
    _ZERO_TRADE_REPAIR_SCHEMA_JSON,
    ZeroTradeRepairAgent,
    ZeroTradeRepairReport,
    _coerce_report,
)
from investment_team.strategy_lab.exceptions import SpecImplementabilityError
from investment_team.strategy_lab.orchestrator import (
    RefinementStallTracker,
    StrategyLabOrchestrator,
)
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
)
from investment_team.strategy_lab.zero_trade_repair import (
    ZeroTradeRepairOutcome as _ZeroTradeRepairOutcome,
)
from investment_team.tests.test_strategy_lab_alignment import (
    _benign_sandbox_trades,
    _code_exec,
)
from investment_team.trading_service.modes.sandbox_compat import StrategyRunResult

# ---------------------------------------------------------------------------
# Fixtures / helpers
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


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-zt-repair-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70)
            )
        ],
        risk_limits={"max_position_pct": 5},
        speculative=False,
        strategy_code=(
            "from contract import Strategy\n\n"
            "class S(Strategy):\n"
            "    def on_bar(self, ctx, bar):\n"
            "        pass  # never submits an order — original buggy code\n"
        ),
    )


def _market_data() -> Dict[str, List[OHLCVBar]]:
    bars = [
        OHLCVBar(
            date=f"2023-01-{i + 1:02d}",
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=1_000_000,
        )
        for i in range(20)
    ]
    return {"AAPL": bars}


def _zero_trade_diagnostics(
    category: str = "NO_ORDERS_EMITTED",
) -> BacktestExecutionDiagnostics:
    return BacktestExecutionDiagnostics(
        zero_trade_category=category,  # type: ignore[arg-type]
        summary="strategy never submitted an order across 20 bars",
        bars_processed=20,
        orders_emitted=0,
        orders_accepted=0,
        orders_rejected=0,
        orders_unfilled=0,
        warmup_orders_dropped=0,
        entries_filled=0,
        exits_emitted=0,
        closed_trades=0,
    )


def _zero_trade_exec_result() -> StrategyRunResult:
    """Initial backtest result: zero trades + diagnostics with category."""
    return StrategyRunResult(
        success=True,
        trades=[],
        execution_diagnostics=_zero_trade_diagnostics(),
    )


# Valid Strategy-subclass code that the safety gate accepts. Body intentionally
# trivial — we never actually execute it because ``run_strategy_code`` is
# stubbed via monkeypatch. Entry + exit ``submit_order`` calls satisfy the
# order-flow-shape gate added in #547.
_REPAIRED_CODE = (
    "from contract import Strategy\n\n"
    "class S(Strategy):\n"
    "    def on_bar(self, ctx, bar):\n"
    "        ctx.submit_order(symbol='X', qty=1, side='LONG')\n"
    "        ctx.submit_order(symbol='X', qty=1, side='FLAT')\n"
)


# Code that fails the safety gate (banned import).
_UNSAFE_CODE = (
    "import os\n\n"
    "from contract import Strategy\n\n"
    "class S(Strategy):\n"
    "    def on_bar(self, ctx, bar):\n"
    "        os.system('rm -rf /')  # banned\n"
)


class _StubZeroTradeRepairAgent:
    """Records calls and returns scripted ``ZeroTradeRepairReport`` objects.

    Mirrors ``_StubAlignmentAgent`` from ``test_strategy_lab_alignment.py``
    but for the repair agent's signature.
    """

    def __init__(
        self,
        *,
        reports: Optional[List[ZeroTradeRepairReport]] = None,
        raise_on_call: Optional[Exception] = None,
    ) -> None:
        self._reports = list(reports or [])
        self._raise = raise_on_call
        self.calls: List[Dict[str, Any]] = []

    def run(
        self,
        spec: StrategySpec,
        code: str,
        diagnostics: BacktestExecutionDiagnostics,
        prior_attempts: Optional[List[str]] = None,
        *,
        coverage_report: Optional[CoverageReport] = None,
    ) -> ZeroTradeRepairReport:
        self.calls.append(
            {
                "code": code,
                "category": diagnostics.zero_trade_category,
                "prior_attempts": list(prior_attempts or []),
                "coverage_report": coverage_report,
            }
        )
        if self._raise is not None:
            raise self._raise
        if not self._reports:
            raise AssertionError("repair stub called more times than scripted")
        return self._reports.pop(0)


class _StubSandbox:
    """Stub for ``run_strategy_code``. ``results`` is consumed in order.

    Patched into ``investment_team.strategy_lab.orchestrator.run_strategy_code``
    via monkeypatch so the helper picks up the stub when re-running the
    proposed code.
    """

    def __init__(self, results: List[StrategyRunResult]) -> None:
        self._results = list(results)
        self.calls: List[str] = []

    def __call__(
        self,
        strategy_code: str,
        market_data: Any,
        config: Any,
        *,
        strategy: Any = None,
    ) -> StrategyRunResult:
        self.calls.append(strategy_code)
        if not self._results:
            raise AssertionError("sandbox stub called more times than scripted")
        return self._results.pop(0)


def _make_orchestrator_with_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    repair_reports: Optional[List[ZeroTradeRepairReport]] = None,
    repair_raises: Optional[Exception] = None,
    sandbox_results: Optional[List[StrategyRunResult]] = None,
) -> tuple[StrategyLabOrchestrator, _StubZeroTradeRepairAgent, _StubSandbox]:
    orch = StrategyLabOrchestrator()
    repair_stub = _StubZeroTradeRepairAgent(reports=repair_reports, raise_on_call=repair_raises)
    sandbox_stub = _StubSandbox(sandbox_results or [])
    orch.zero_trade_repair_agent = repair_stub  # type: ignore[assignment]
    monkeypatch.setattr(orchestrator_module, "run_strategy_code", sandbox_stub)
    return orch, repair_stub, sandbox_stub


def _drive_repair(
    orch: StrategyLabOrchestrator,
    *,
    spec: Optional[StrategySpec] = None,
    code: Optional[str] = None,
    exec_result: Optional[StrategyRunResult] = None,
    market_data: Optional[Dict[str, List[OHLCVBar]]] = None,
    config: Optional[BacktestConfig] = None,
    zero_trade_attempts: Optional[List[str]] = None,
    coverage_report: Optional[CoverageReport] = None,
) -> tuple[_ZeroTradeRepairOutcome, List[tuple[str, Dict[str, Any]]], List[str]]:
    """Convenience wrapper around ``orch.zero_trade_repairer.try_repair``.

    Captures emitted phase callbacks and the orchestrator's
    ``zero_trade_attempts`` log so tests can assert on them.
    """
    spec = spec or _spec()
    code = code if code is not None else (spec.strategy_code or "")
    exec_result = exec_result or _zero_trade_exec_result()
    market_data = market_data or _market_data()
    config = config or _config()
    attempts = zero_trade_attempts if zero_trade_attempts is not None else []
    events: List[tuple[str, Dict[str, Any]]] = []

    def emit(phase: str, data: Dict[str, Any]) -> None:
        events.append((phase, data))

    outcome = orch.zero_trade_repairer.try_repair(
        spec=spec,
        code=code,
        exec_result=exec_result,
        market_data=market_data,
        config=config,
        zero_trade_attempts=attempts,
        round_num=0,
        emit=emit,
        coverage_report=coverage_report,
    )
    return outcome, events, attempts


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_zero_trade_repair_succeeds_on_first_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair agent proposes new code; re-backtest produces trades that
    clear the anomaly gates. Outcome is committed with the new state."""
    orch, repair_stub, sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_reports=[
            ZeroTradeRepairReport(
                root_cause_category="NO_ORDERS_EMITTED",
                evidence="orders_emitted=0 with bars_processed=20",
                code_issue="entry guard never true",
                proposed_code=_REPAIRED_CODE,
                expected_order_count_change=12,
                expected_trade_count_change=6,
                changes_made="loosened RSI guard so signals fire",
            ),
        ],
        sandbox_results=[
            _code_exec(success=True, raw_trades=_benign_sandbox_trades()),
        ],
    )

    outcome, events, attempts = _drive_repair(orch)

    assert outcome.committed is True
    assert outcome.new_code == _REPAIRED_CODE
    assert outcome.new_spec is not None
    assert outcome.new_spec.strategy_code == _REPAIRED_CODE
    assert outcome.new_metrics is not None
    assert outcome.new_trades, "committed outcome must carry the post-repair ledger"
    assert outcome.new_exec_result is not None
    assert outcome.new_exec_result.success is True
    assert outcome.changes_made.startswith("loosened RSI guard")

    # The agent and the sandbox were each called exactly once.
    assert len(repair_stub.calls) == 1
    assert sandbox_stub.calls == [_REPAIRED_CODE]

    # The attempts log records the commit so prior_attempts on a future
    # round can read it as evidence.
    assert len(attempts) == 1
    assert attempts[0].startswith("committed (NO_ORDERS_EMITTED)")

    # Phase emits: started → committed.
    sub_phases = [d.get("sub_phase") for _, d in events]
    assert sub_phases == [
        "zero_trade_repair_started",
        "zero_trade_repair_committed",
    ]

    # Gate results include both safety + post-repair anomaly gates.
    assert outcome.new_gates, "committed outcome must surface its quality gates"
    gate_names = {g.gate_name for g in outcome.new_gates}
    assert any(name.startswith("zero_trade_repair_") for name in gate_names)


def test_zero_trade_repair_reexecution_hits_backtest_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_re_execute`` re-checks a ``(code, market_data, config)`` triple the
    alignment loop already ran within the same attempt — it must be served
    from the orchestrator's ``BacktestCache`` rather than spawning a second
    sandbox subprocess (#2573/#2594).
    """
    orch, repair_stub, sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_reports=[
            ZeroTradeRepairReport(
                root_cause_category="NO_ORDERS_EMITTED",
                evidence="orders_emitted=0 with bars_processed=20",
                code_issue="entry guard never true",
                proposed_code=_REPAIRED_CODE,
                expected_order_count_change=12,
                expected_trade_count_change=6,
                changes_made="loosened RSI guard so signals fire",
            ),
        ],
        sandbox_results=[
            _code_exec(success=True, raw_trades=_benign_sandbox_trades()),
        ],
    )
    spec = _spec()
    market_data = _market_data()
    config = _config()

    # Simulate the alignment loop having already re-checked this exact
    # (code, market_data, config, spec) triple earlier in the same attempt —
    # this populates ``orch._backtest_cache`` via the shared cached path and
    # is the only sandbox invocation that should ever occur.
    prewarm_result = orch._cached_run_strategy_code(
        _REPAIRED_CODE, market_data, config, strategy=spec
    )
    assert sandbox_stub.calls == [_REPAIRED_CODE]

    # ``report.proposed_spec_updates`` is unset, so ``_re_execute``'s
    # ``proposed_spec`` differs from ``spec`` only in ``strategy_code`` —
    # excluded from the cache's spec hash — so the key lines up with the
    # pre-warmed entry above.
    outcome, _events, _attempts = _drive_repair(
        orch, spec=spec, market_data=market_data, config=config
    )

    assert outcome.committed is True
    assert outcome.new_exec_result is prewarm_result, (
        "re-execution must return the cached result object, not a fresh run"
    )

    # The sandbox was invoked exactly once total: the repair's re-execution
    # was served from BacktestCache instead of spawning a second subprocess.
    assert sandbox_stub.calls == [_REPAIRED_CODE]
    assert len(repair_stub.calls) == 1

    cache = orch._backtest_cache
    assert cache.hits == 1
    assert cache.misses == 1


def test_zero_trade_repair_failed_proposal_preserves_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-backtest of the proposal still produces zero trades → critical
    anomaly → outcome is not committed and the attempts log records the
    rejection. The caller therefore retains its prior known-good state.
    """
    orch, repair_stub, sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_reports=[
            ZeroTradeRepairReport(
                root_cause_category="NO_ORDERS_EMITTED",
                evidence="orders_emitted=0",
                proposed_code=_REPAIRED_CODE,
                changes_made="attempted RSI loosen",
            ),
        ],
        sandbox_results=[
            # Re-execution yields zero trades again → BacktestAnomalyDetector
            # flags critical, repair must NOT commit.
            _code_exec(success=True, raw_trades=[]),
        ],
    )

    outcome, events, attempts = _drive_repair(orch)

    assert outcome.committed is False
    assert outcome.failure_reason == "anomaly_after_repair"
    assert outcome.new_code == ""
    assert outcome.new_spec is None
    assert outcome.new_metrics is None
    assert outcome.new_exec_result is None

    assert len(repair_stub.calls) == 1
    assert sandbox_stub.calls == [_REPAIRED_CODE]

    assert len(attempts) == 1
    assert attempts[0].startswith("anomaly_after_repair (NO_ORDERS_EMITTED)")

    sub_phases = [d.get("sub_phase") for _, d in events]
    assert sub_phases == [
        "zero_trade_repair_started",
        "zero_trade_repair_rejected",
    ]
    rejected_event = next(
        d for _, d in events if d.get("sub_phase") == "zero_trade_repair_rejected"
    )
    assert rejected_event["reason"] == "anomaly_after_repair"

    # Surfaced gates include the critical anomaly so downstream telemetry
    # can audit the failed attempt.
    critical_gates = [g for g in outcome.new_gates if not g.passed and g.severity == "critical"]
    assert critical_gates, "expected at least one critical anomaly gate"
    assert all(g.gate_name.startswith("zero_trade_repair_") for g in critical_gates)


def test_zero_trade_repair_failed_proposal_is_pure_wrt_input_code_and_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected repair leaves the caller's input ``code`` and ``spec``
    untouched — ``try_repair`` is pure with respect to its inputs, so the
    fallback ``RefinementAgent`` runs against the original known-good blob
    rather than a half-mutated one."""
    orch, repair_stub, sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_reports=[
            ZeroTradeRepairReport(
                root_cause_category="NO_ORDERS_EMITTED",
                evidence="orders_emitted=0",
                proposed_code=_REPAIRED_CODE,
                changes_made="attempted RSI loosen",
            ),
        ],
        sandbox_results=[
            _code_exec(success=True, raw_trades=[]),  # zero trades again → reject
        ],
    )

    input_spec = _spec()
    input_code = "INPUT-CODE-BLOB"
    spec_before = input_spec.model_dump()

    outcome, _events, _attempts = _drive_repair(orch, spec=input_spec, code=input_code)

    assert outcome.committed is False
    # The input objects the caller still holds are unchanged.
    assert input_code == "INPUT-CODE-BLOB"
    assert input_spec.model_dump() == spec_before
    # The repairer signals "no commit" via empty/None outcome fields rather
    # than handing back a mutated blob.
    assert outcome.new_code == ""
    assert outcome.new_spec is None


def test_zero_trade_repair_unsafe_code_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proposed code with a banned import fails code-safety; the helper
    short-circuits without invoking ``run_strategy_code``."""
    orch, repair_stub, sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_reports=[
            ZeroTradeRepairReport(
                root_cause_category="ORDERS_REJECTED",
                evidence="orders_rejected=5 with reason insufficient_capital",
                proposed_code=_UNSAFE_CODE,
                changes_made="unsafe rewrite",
            ),
        ],
        sandbox_results=[],  # sandbox MUST NOT be called
    )

    diagnostics = _zero_trade_diagnostics(category="ORDERS_REJECTED")
    outcome, events, attempts = _drive_repair(
        orch,
        exec_result=StrategyRunResult(success=True, trades=[], execution_diagnostics=diagnostics),
    )

    assert outcome.committed is False
    assert outcome.failure_reason == "unsafe_code"
    assert sandbox_stub.calls == []  # short-circuited before re-execution
    assert len(repair_stub.calls) == 1

    assert len(attempts) == 1
    assert attempts[0].startswith("unsafe_code (ORDERS_REJECTED)")

    sub_phases = [d.get("sub_phase") for _, d in events]
    assert sub_phases == [
        "zero_trade_repair_started",
        "zero_trade_repair_rejected",
    ]
    rejected_event = next(
        d for _, d in events if d.get("sub_phase") == "zero_trade_repair_rejected"
    )
    assert rejected_event["reason"] == "unsafe_code"

    # Safety gates include the critical failure.
    critical_safety = [
        g
        for g in outcome.new_gates
        if not g.passed
        and g.severity == "critical"
        and g.gate_name == "zero_trade_repair_code_safety"
    ]
    assert critical_safety, "expected the code_safety gate to fire on banned import"


def test_zero_trade_repair_no_proposed_code_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent declined to propose code (e.g. evidence too thin). The helper
    reports not-committed and the sandbox is never invoked. The caller is
    expected to fall through to the generic refinement agent."""
    orch, repair_stub, sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_reports=[
            ZeroTradeRepairReport(
                root_cause_category="UNKNOWN_ZERO_TRADE_PATH",
                evidence="diagnostics envelope was unclassified; not enough signal",
                proposed_code=None,
            ),
        ],
        sandbox_results=[],
    )

    outcome, events, attempts = _drive_repair(
        orch,
        exec_result=StrategyRunResult(
            success=True,
            trades=[],
            execution_diagnostics=_zero_trade_diagnostics(category="UNKNOWN_ZERO_TRADE_PATH"),
        ),
    )

    assert outcome.committed is False
    assert outcome.failure_reason == "no_proposed_code"
    assert sandbox_stub.calls == []
    assert len(repair_stub.calls) == 1

    assert len(attempts) == 1
    assert attempts[0].startswith("no_proposal (UNKNOWN_ZERO_TRADE_PATH)")

    sub_phases = [d.get("sub_phase") for _, d in events]
    assert sub_phases == [
        "zero_trade_repair_started",
        "zero_trade_repair_skipped",
    ]


def test_zero_trade_repair_invalid_spec_updates_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitelisted ``proposed_spec_updates`` key with the wrong shape
    (e.g. ``risk_limits`` as a bare string) must be rejected as a
    not-committed outcome — the helper must NOT let the resulting
    Pydantic ``ValidationError`` abort the entire Strategy Lab cycle."""
    orch, repair_stub, sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_reports=[
            ZeroTradeRepairReport(
                root_cause_category="ENTRY_WITH_NO_EXIT",
                evidence="entries_filled=4 closed_trades=0",
                proposed_code=_REPAIRED_CODE,
                # Post-#530, ``risk_limits`` is the only whitelisted key.
                # A bare string is the realistic LLM error mode that
                # previously crashed the cycle.
                proposed_spec_updates={"risk_limits": "loosen drawdown please"},
                changes_made="malformed risk_limits",
            ),
        ],
        sandbox_results=[],  # sandbox MUST NOT be called
    )

    outcome, events, attempts = _drive_repair(
        orch,
        exec_result=StrategyRunResult(
            success=True,
            trades=[],
            execution_diagnostics=_zero_trade_diagnostics(category="ENTRY_WITH_NO_EXIT"),
        ),
    )

    assert outcome.committed is False
    assert outcome.failure_reason == "invalid_spec_updates"
    assert sandbox_stub.calls == []  # short-circuited before re-execution
    assert len(repair_stub.calls) == 1

    assert len(attempts) == 1
    assert attempts[0].startswith("invalid_spec_updates (ENTRY_WITH_NO_EXIT)")

    sub_phases = [d.get("sub_phase") for _, d in events]
    assert sub_phases == [
        "zero_trade_repair_started",
        "zero_trade_repair_rejected",
    ]
    rejected_event = next(
        d for _, d in events if d.get("sub_phase") == "zero_trade_repair_rejected"
    )
    assert rejected_event["reason"] == "invalid_spec_updates"


def test_zero_trade_repair_invalid_spec_updates_still_carries_dropped_keys_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #530: when the proposal both (a) has off-list keys the agent
    already filtered AND (b) the surviving ``risk_limits`` is malformed
    enough to raise ``ValidationError`` in ``_apply_zero_trade_spec_updates``,
    the early-return path must still surface the
    ``zero_trade_repair_dropped_spec_keys`` audit gate. Previously the
    ValidationError path returned ``new_gates=safety_gates`` only and the
    dropped-keys gate was lost — the warning fired but the audit trail
    was missing from ``quality_gate_results``."""
    orch, repair_stub, sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_reports=[
            ZeroTradeRepairReport(
                root_cause_category="ENTRY_WITH_NO_EXIT",
                evidence="entries_filled=4 closed_trades=0",
                proposed_code=_REPAIRED_CODE,
                # ``risk_limits`` as a bare string → Pydantic
                # ValidationError when ``_apply_zero_trade_spec_updates``
                # tries to coerce into ``RiskLimits``.
                proposed_spec_updates={"risk_limits": "loosen drawdown please"},
                # Agent-pre-filtered keys that should still be surfaced
                # on the rejected run's quality gates.
                dropped_spec_update_keys=["entry_rules", "hypothesis"],
                changes_made="malformed risk_limits plus filtered drift",
            ),
        ],
        sandbox_results=[],  # sandbox MUST NOT be called
    )

    outcome, events, attempts = _drive_repair(
        orch,
        exec_result=StrategyRunResult(
            success=True,
            trades=[],
            execution_diagnostics=_zero_trade_diagnostics(category="ENTRY_WITH_NO_EXIT"),
        ),
    )

    assert outcome.committed is False
    assert outcome.failure_reason == "invalid_spec_updates"
    assert sandbox_stub.calls == []

    # The dropped-keys gate is preserved on the rejected outcome so the
    # persisted ``quality_gate_results`` still reflects the attempted
    # off-list mutation.
    dropped_gates = [
        g for g in outcome.new_gates if g.gate_name == "zero_trade_repair_dropped_spec_keys"
    ]
    assert len(dropped_gates) == 1, outcome.new_gates
    gate = dropped_gates[0]
    assert gate.severity == "warning"
    assert gate.passed is False
    for key in ("entry_rules", "hypothesis"):
        assert key in gate.details


def test_zero_trade_repair_agent_exception_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raised exception inside the agent collapses to a not-committed
    outcome and is logged in attempts so the caller can fall through."""
    orch, repair_stub, sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_raises=RuntimeError("LLM provider timeout"),
        sandbox_results=[],
    )

    outcome, events, attempts = _drive_repair(orch)

    assert outcome.committed is False
    assert outcome.failure_reason.startswith("agent_error")
    assert sandbox_stub.calls == []
    assert len(repair_stub.calls) == 1

    assert len(attempts) == 1
    assert attempts[0].startswith("agent_error: RuntimeError")

    sub_phases = [d.get("sub_phase") for _, d in events]
    assert sub_phases == [
        "zero_trade_repair_started",
        "zero_trade_repair_skipped",
    ]


def test_zero_trade_repair_applies_proposed_spec_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitelisted ``proposed_spec_updates`` flow into the committed spec;
    off-list keys are silently dropped so an LLM hallucination cannot
    rewrite arbitrary fields. Post-#530 the whitelist is ``risk_limits``
    only."""
    orch, repair_stub, sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_reports=[
            ZeroTradeRepairReport(
                root_cause_category="ENTRY_WITH_NO_EXIT",
                evidence="entries_filled=4 closed_trades=0",
                proposed_code=_REPAIRED_CODE,
                proposed_spec_updates={
                    "risk_limits": {"max_position_pct": 5},
                    # Off-list keys MUST NOT mutate the spec (#530).
                    "exit_rules": [StopLossRule(pct=0.05, note="added stop").model_dump()],
                    "strategy_id": "hijacked",
                    "asset_class": "crypto",
                },
                changes_made="adjusted position cap",
            ),
        ],
        sandbox_results=[
            _code_exec(success=True, raw_trades=_benign_sandbox_trades()),
        ],
    )

    diagnostics = _zero_trade_diagnostics(category="ENTRY_WITH_NO_EXIT")
    outcome, _events, _attempts = _drive_repair(
        orch,
        exec_result=StrategyRunResult(success=True, trades=[], execution_diagnostics=diagnostics),
    )

    assert outcome.committed is True
    assert outcome.new_spec is not None
    # Whitelisted update applied …
    assert outcome.new_spec.risk_limits.max_position_pct == 5
    # … and off-list mutations were silently dropped (rule + immutable keys).
    original_exit_rules = _spec().exit_rules
    assert outcome.new_spec.exit_rules == original_exit_rules
    assert outcome.new_spec.strategy_id == "strat-zt-repair-test"
    assert outcome.new_spec.asset_class == "stocks"
    # And a dropped-keys quality gate was surfaced on the committed run.
    dropped_gates = [
        g for g in outcome.new_gates if g.gate_name == "zero_trade_repair_dropped_spec_keys"
    ]
    assert dropped_gates, outcome.new_gates
    assert dropped_gates[0].severity == "warning"
    assert dropped_gates[0].passed is False
    assert "exit_rules" in dropped_gates[0].details
    assert "strategy_id" in dropped_gates[0].details
    assert "asset_class" in dropped_gates[0].details


def test_zero_trade_repair_drops_off_list_spec_keys_protects_thesis(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #530: when the repair agent proposes ``hypothesis``,
    ``entry_rules`` (or any other thesis-defining key), those mutations
    MUST be dropped silently — never reach the committed spec — and the
    drop MUST be surfaced as a warning gate plus a ``logger.warning`` so
    the thesis cannot quietly mutate across refinement rounds."""
    pre_repair_spec = _spec()

    orch, repair_stub, sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_reports=[
            ZeroTradeRepairReport(
                root_cause_category="ENTRY_WITH_NO_EXIT",
                evidence="entries_filled=4 closed_trades=0",
                proposed_code=_REPAIRED_CODE,
                # All of these MUST be dropped — only ``risk_limits`` is
                # honoured post-#530.
                proposed_spec_updates={
                    "hypothesis": "QQQ → TSLA bait",
                    "signal_definition": "loosened",
                    "entry_rules": [
                        EntryRule(
                            side="long",
                            when=Predicate(
                                lhs=IndicatorRef(name="rsi", params={"period": 14}),
                                op="<",
                                rhs=90,
                            ),
                        ).model_dump()
                    ],
                    "exit_rules": [StopLossRule(pct=0.05, note="x").model_dump()],
                    "sizing": {"kind": "fixed_fraction", "fraction": 0.5},
                },
                changes_made="attempted to rewrite the thesis",
            ),
        ],
        sandbox_results=[
            _code_exec(success=True, raw_trades=_benign_sandbox_trades()),
        ],
    )

    with caplog.at_level(
        "WARNING",
        logger="investment_team.strategy_lab.orchestrator",
    ):
        outcome, _events, _attempts = _drive_repair(
            orch,
            spec=pre_repair_spec,
            exec_result=StrategyRunResult(
                success=True,
                trades=[],
                execution_diagnostics=_zero_trade_diagnostics(category="ENTRY_WITH_NO_EXIT"),
            ),
        )

    assert outcome.committed is True
    assert outcome.new_spec is not None
    # Thesis-defining keys are NEVER overwritten by zero-trade repair.
    assert outcome.new_spec.hypothesis == pre_repair_spec.hypothesis
    assert outcome.new_spec.signal_definition == pre_repair_spec.signal_definition
    assert outcome.new_spec.entry_rules == pre_repair_spec.entry_rules
    assert outcome.new_spec.exit_rules == pre_repair_spec.exit_rules
    assert outcome.new_spec.sizing == pre_repair_spec.sizing
    # risk_limits was untouched by the proposal so it must also be unchanged.
    assert outcome.new_spec.risk_limits == pre_repair_spec.risk_limits

    # The dropped-keys gate is surfaced on the committed run.
    dropped_gates = [
        g for g in outcome.new_gates if g.gate_name == "zero_trade_repair_dropped_spec_keys"
    ]
    assert len(dropped_gates) == 1, outcome.new_gates
    gate = dropped_gates[0]
    assert gate.severity == "warning"
    assert gate.passed is False
    for key in ("hypothesis", "signal_definition", "entry_rules", "exit_rules", "sizing"):
        assert key in gate.details

    # And the logger.warning fired with the dropped keys.
    drop_logs = [
        rec for rec in caplog.records if "discarded spec-mutating keys" in rec.getMessage()
    ]
    assert drop_logs, [rec.getMessage() for rec in caplog.records]
    msg = drop_logs[0].getMessage()
    for key in ("hypothesis", "signal_definition", "entry_rules", "exit_rules", "sizing"):
        assert key in msg


def test_zero_trade_repair_surfaces_agent_filtered_drops_in_production_flow(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #530 (codex review follow-up): in the real production flow the
    agent's ``_coerce_report`` strips off-list keys from ``proposed_spec_updates``
    before the report reaches the orchestrator, so the orchestrator never sees
    raw drift on ``proposed_spec_updates``. The orchestrator must still emit
    the ``logger.warning`` and the ``zero_trade_repair_dropped_spec_keys`` gate
    by reading ``report.dropped_spec_update_keys`` populated by the agent —
    otherwise the visibility added in #530 only fires in tests with stubs."""
    pre_repair_spec = _spec()

    orch, repair_stub, sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_reports=[
            ZeroTradeRepairReport(
                root_cause_category="ENTRY_WITH_NO_EXIT",
                evidence="entries_filled=4 closed_trades=0",
                proposed_code=_REPAIRED_CODE,
                # ``proposed_spec_updates`` is what the production agent
                # would return after its own filter ran — only the
                # whitelisted ``risk_limits`` survived. The off-list keys
                # are reported separately via ``dropped_spec_update_keys``.
                proposed_spec_updates=None,
                dropped_spec_update_keys=["entry_rules", "hypothesis", "sizing"],
                changes_made="risk_limits tweak only after agent filter",
            ),
        ],
        sandbox_results=[
            _code_exec(success=True, raw_trades=_benign_sandbox_trades()),
        ],
    )

    with caplog.at_level(
        "WARNING",
        logger="investment_team.strategy_lab.orchestrator",
    ):
        outcome, _events, _attempts = _drive_repair(
            orch,
            spec=pre_repair_spec,
            exec_result=StrategyRunResult(
                success=True,
                trades=[],
                execution_diagnostics=_zero_trade_diagnostics(category="ENTRY_WITH_NO_EXIT"),
            ),
        )

    assert outcome.committed is True
    assert outcome.new_spec is not None
    # No off-list mutation reached the committed spec.
    assert outcome.new_spec.hypothesis == pre_repair_spec.hypothesis
    assert outcome.new_spec.entry_rules == pre_repair_spec.entry_rules
    assert outcome.new_spec.sizing == pre_repair_spec.sizing

    # Even though ``proposed_spec_updates`` was already sanitised by the
    # agent, the orchestrator surfaces what was dropped.
    dropped_gates = [
        g for g in outcome.new_gates if g.gate_name == "zero_trade_repair_dropped_spec_keys"
    ]
    assert len(dropped_gates) == 1, outcome.new_gates
    gate = dropped_gates[0]
    assert gate.severity == "warning"
    assert gate.passed is False
    for key in ("entry_rules", "hypothesis", "sizing"):
        assert key in gate.details

    drop_logs = [
        rec for rec in caplog.records if "discarded spec-mutating keys" in rec.getMessage()
    ]
    assert drop_logs, [rec.getMessage() for rec in caplog.records]


def test_zero_trade_repair_rejects_spec_updates_failing_post_repair_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitelisted spec_updates that pass Pydantic but fail StrategySpecValidator
    (e.g. ``risk_limits.max_position_pct=99`` — Pydantic-valid but above the
    25% safe range) must be rejected by the post-repair revalidation gate
    added for #547 so the spec mutation never bypasses validation."""
    orch, repair_stub, sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_reports=[
            ZeroTradeRepairReport(
                root_cause_category="ENTRY_WITH_NO_EXIT",
                evidence="entries_filled=4 closed_trades=0",
                proposed_code=_REPAIRED_CODE,
                # ``max_position_pct=99`` is Pydantic-valid but the
                # StrategySpecValidator marks anything outside [1, 25] as
                # critical (safety_validator.py).
                proposed_spec_updates={"risk_limits": {"max_position_pct": 99}},
                changes_made="bumped position size",
            ),
        ],
        sandbox_results=[],  # sandbox MUST NOT be called
    )

    outcome, events, attempts = _drive_repair(
        orch,
        exec_result=StrategyRunResult(
            success=True,
            trades=[],
            execution_diagnostics=_zero_trade_diagnostics(category="ENTRY_WITH_NO_EXIT"),
        ),
    )

    assert outcome.committed is False
    assert outcome.failure_reason == "invalid_spec_after_repair"
    # The sandbox must not run when the spec validator rejects the proposal.
    assert sandbox_stub.calls == []
    assert len(repair_stub.calls) == 1

    assert len(attempts) == 1
    assert attempts[0].startswith("invalid_spec_after_repair (ENTRY_WITH_NO_EXIT)")

    rejected_event = next(
        d for _, d in events if d.get("sub_phase") == "zero_trade_repair_rejected"
    )
    assert rejected_event["reason"] == "invalid_spec_after_repair"
    # The post-repair spec gates are returned so the orchestrator can persist
    # them on the failed-cycle record.
    gate_names = [g.gate_name for g in outcome.new_gates]
    assert any(
        name.startswith("zero_trade_repair_strategy_spec_validator") for name in gate_names
    ), gate_names


def test_zero_trade_repair_accepted_carries_post_repair_spec_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a repair mutates the spec and the post-repair validator emits
    only warnings (no criticals), the warnings must reach the accepted
    outcome's ``new_gates`` so they appear in the persisted
    ``quality_gate_results``. Previously these were discarded on accept."""
    orch, _repair_stub, _sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_reports=[
            ZeroTradeRepairReport(
                root_cause_category="ENTRY_WITH_NO_EXIT",
                evidence="entries_filled=4 closed_trades=0",
                proposed_code=_REPAIRED_CODE,
                # The risk_limits mutation is Pydantic-valid and non-critical
                # (max_position_pct in the safe [1, 25] band). The base spec's
                # hypothesis ("hyp") names no indicator while its rules use RSI,
                # so the post-repair StrategySpecValidator emits a non-critical
                # hypothesis/rules-consistency WARNING — accepted, but the gate
                # must still be carried forward onto the committed outcome.
                proposed_spec_updates={"risk_limits": {"max_position_pct": 8}},
                changes_made="adjusted position cap",
            ),
        ],
        sandbox_results=[
            _code_exec(success=True, raw_trades=_benign_sandbox_trades()),
        ],
    )

    outcome, _events, _attempts = _drive_repair(
        orch,
        exec_result=StrategyRunResult(
            success=True,
            trades=[],
            execution_diagnostics=_zero_trade_diagnostics(category="ENTRY_WITH_NO_EXIT"),
        ),
    )

    assert outcome.committed is True
    # Look for the post-repair validator's warning in the carried gates.
    spec_gate_names = [g.gate_name for g in outcome.new_gates]
    assert any(
        name.startswith("zero_trade_repair_strategy_spec_validator") for name in spec_gate_names
    ), spec_gate_names
    # And confirm it is a warning, not critical (else the repair would have
    # been rejected, not accepted).
    spec_warnings = [
        g
        for g in outcome.new_gates
        if g.gate_name.startswith("zero_trade_repair_strategy_spec_validator")
        and g.severity == "warning"
    ]
    assert spec_warnings, outcome.new_gates


def test_zero_trade_repair_no_category_is_defensive_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive path: if the orchestrator's routing guard ever lets a
    diagnostics-without-category through to the helper, we report
    not-committed without calling the agent."""
    orch, repair_stub, sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_reports=[],
        sandbox_results=[],
    )

    no_category_diag = BacktestExecutionDiagnostics(zero_trade_category=None)
    outcome, events, attempts = _drive_repair(
        orch,
        exec_result=StrategyRunResult(
            success=True, trades=[], execution_diagnostics=no_category_diag
        ),
    )

    assert outcome.committed is False
    assert "zero_trade_category" in outcome.failure_reason
    assert repair_stub.calls == []
    assert sandbox_stub.calls == []
    assert attempts == []
    assert events == []


def test_orchestrator_constructs_zero_trade_repair_agent_by_default() -> None:
    """The orchestrator wires up a real :class:`ZeroTradeRepairAgent` so
    callers that don't inject one still pick up the specialized branch."""
    from investment_team.strategy_lab.agents.zero_trade_repair import (
        ZeroTradeRepairAgent,
    )

    orch = StrategyLabOrchestrator()
    assert isinstance(orch.zero_trade_repair_agent, ZeroTradeRepairAgent)


def test_quality_gate_results_are_typed() -> None:
    """Sanity check: the helper's surfaced gates must be ``QualityGateResult``s
    so existing telemetry consumers (Strategy Lab dashboards, persisted
    records) don't choke on a foreign payload."""
    # No monkeypatch needed — exercise the defensive no-op path.
    orch = StrategyLabOrchestrator()

    no_category_diag = BacktestExecutionDiagnostics(zero_trade_category=None)
    outcome = orch.zero_trade_repairer.try_repair(
        spec=_spec(),
        code=_spec().strategy_code or "",
        exec_result=StrategyRunResult(
            success=True, trades=[], execution_diagnostics=no_category_diag
        ),
        market_data=_market_data(),
        config=_config(),
        zero_trade_attempts=[],
        round_num=0,
        emit=lambda _phase, _data: None,
    )
    assert all(isinstance(g, QualityGateResult) for g in outcome.new_gates)


# ---------------------------------------------------------------------------
# Issue #452 — orchestrator forwards CoverageReport to the repair agent.
# ---------------------------------------------------------------------------


def test_coverage_report_is_forwarded_to_repair_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the orchestrator has attached a CoverageReport (#451) to the
    failing run's metrics, ``ZeroTradeRepairer.try_repair`` must forward it to
    the repair agent so the prompt sees the static probe's verdict
    alongside the executor diagnostics.
    """
    report = CoverageReport(
        coverage_category=CoverageCategory.ENTRY_CONDITION_NEVER_TRUE,
        summary="RSI<30 never true on 20 bars",
        bars_checked=20,
        symbols_checked=1,
    )
    orch, repair_stub, _sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_reports=[
            ZeroTradeRepairReport(
                root_cause_category="NO_ORDERS_EMITTED",
                evidence="entry guard never true",
                proposed_code=None,  # decline; falls through cleanly
            ),
        ],
    )

    _drive_repair(orch, coverage_report=report)

    assert len(repair_stub.calls) == 1
    forwarded = repair_stub.calls[0]["coverage_report"]
    assert forwarded is report


def test_repair_agent_receives_none_when_no_coverage_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: the repair agent must still be invoked with
    ``coverage_report=None`` when the orchestrator did not attach a probe
    output (e.g. when probes are gated off). Strictly additive (#452).
    """
    orch, repair_stub, _sandbox_stub = _make_orchestrator_with_stubs(
        monkeypatch,
        repair_reports=[
            ZeroTradeRepairReport(
                root_cause_category="NO_ORDERS_EMITTED",
                evidence="entry guard never true",
                proposed_code=None,
            ),
        ],
    )

    _drive_repair(orch)  # no coverage_report kwarg

    assert len(repair_stub.calls) == 1
    assert repair_stub.calls[0]["coverage_report"] is None


# ---------------------------------------------------------------------------
# _handle_critical_anomalies re-derives ran_on_non_conforming_code when a
# zero-trade repair commits new code (which replaces the persisted trades but
# is not otherwise conformance-gated). Generic refinement leaves it None.
# ---------------------------------------------------------------------------


class _StubRepairer:
    """Returns a scripted ``ZeroTradeRepairOutcome`` from ``try_repair``."""

    def __init__(self, outcome: _ZeroTradeRepairOutcome) -> None:
        self._outcome = outcome

    def try_repair(self, **_kwargs: Any) -> _ZeroTradeRepairOutcome:
        return self._outcome


def _committed_repair_outcome() -> _ZeroTradeRepairOutcome:
    repair_exec = _code_exec(success=True, raw_trades=_benign_sandbox_trades())
    return _ZeroTradeRepairOutcome(
        committed=True,
        new_code="# repaired code\n",
        new_spec=_spec(),
        new_trades=repair_exec.trades,
        new_metrics=_metrics_for(),
        new_exec_result=repair_exec,
        new_gates=[],
        changes_made="loosened entry threshold",
    )


def _metrics_for():
    from investment_team.trade_simulator import compute_metrics

    cfg = _config()
    return compute_metrics([], cfg.initial_capital, cfg.start_date, cfg.end_date)


def _critical_anomaly() -> QualityGateResult:
    return QualityGateResult(
        gate_name="backtest_anomaly",
        passed=False,
        severity="critical",
        phase="verification",
        details="zero trades emitted",
    )


def _drive_anomaly_recovery(orch: StrategyLabOrchestrator):
    return orch._handle_critical_anomalies(
        state=_DesignAttemptState(spec=_spec(), code="# original code\n", trades=[], metrics=_metrics_for()),
        exec_result=_zero_trade_exec_result(),
        market_data=_market_data(),
        config=_config(),
        critical_anomalies=[_critical_anomaly()],
        all_gate_results=[],
        refinement_attempts=[],
        zero_trade_attempts=[],
        round_num=0,
        stall_tracker=RefinementStallTracker(),
        emit=lambda *a, **k: None,
    )


def test_ztr_commit_flags_non_conforming_repair_code() -> None:
    """A committed zero-trade repair whose code drifts from the predicate sets
    the non-conforming flag on the recovery outcome."""
    orch = StrategyLabOrchestrator()
    orch.zero_trade_repairer = _StubRepairer(_committed_repair_outcome())  # type: ignore[assignment]
    orch.predicate_conformance_gate.check = lambda code, spec, **kw: [
        QualityGateResult(
            gate_name="predicate_conformance",
            passed=False,
            severity="warning",
            phase="verification",
            details="rule_id=entry[0]: predicate conformance failed.",
        )
    ]

    recovery = _drive_anomaly_recovery(orch)
    assert recovery.exhausted is False
    assert recovery.ran_on_non_conforming_code is True


def test_ztr_commit_clears_flag_for_conforming_repair_code() -> None:
    """A committed repair whose code conforms reports the flag as False."""
    orch = StrategyLabOrchestrator()
    orch.zero_trade_repairer = _StubRepairer(_committed_repair_outcome())  # type: ignore[assignment]
    orch.predicate_conformance_gate.check = lambda code, spec, **kw: [
        QualityGateResult(
            gate_name="predicate_conformance",
            passed=True,
            severity="info",
            phase="verification",
            details="Predicate conformance OK (60 bars checked).",
        )
    ]

    recovery = _drive_anomaly_recovery(orch)
    assert recovery.ran_on_non_conforming_code is False


@pytest.mark.parametrize(
    "missing_field,expected_message",
    [
        ("new_spec", "committed ZTR must carry new_spec"),
        ("new_metrics", "committed ZTR must carry new_metrics"),
        ("new_exec_result", "committed ZTR must carry new_exec_result"),
    ],
)
def test_ztr_commit_missing_field_raises_value_error(
    missing_field: str, expected_message: str
) -> None:
    """A committed zero-trade repair outcome that omits a field it must carry
    raises ``ValueError`` rather than silently propagating ``None`` (the
    postcondition check must not be a bare ``assert``, which `-O` strips)."""
    outcome = dataclasses.replace(_committed_repair_outcome(), **{missing_field: None})
    orch = StrategyLabOrchestrator()
    orch.zero_trade_repairer = _StubRepairer(outcome)  # type: ignore[assignment]

    with pytest.raises(ValueError, match=expected_message):
        _drive_anomaly_recovery(orch)


def test_generic_refine_leaves_flag_unset() -> None:
    """When no zero-trade repair commits (diagnostics carry no category), the
    generic-refine path leaves the flag None so the caller keeps the round's
    existing verdict (trades are unchanged)."""
    orch = StrategyLabOrchestrator()
    orch._refine_or_exhaust = lambda **kw: (kw["spec"], kw["code"], False, False)
    conformance_calls: List[int] = []
    orch.predicate_conformance_gate.check = lambda code, spec, **kw: (
        conformance_calls.append(1) or []
    )

    recovery = orch._handle_critical_anomalies(
        state=_DesignAttemptState(spec=_spec(), code="# original code\n", trades=[], metrics=_metrics_for()),
        # No zero_trade_category -> ZTR branch is skipped.
        exec_result=StrategyRunResult(success=True, trades=[]),
        market_data=_market_data(),
        config=_config(),
        critical_anomalies=[_critical_anomaly()],
        all_gate_results=[],
        refinement_attempts=[],
        zero_trade_attempts=[],
        round_num=0,
        stall_tracker=RefinementStallTracker(),
        emit=lambda *a, **k: None,
    )
    assert recovery.ran_on_non_conforming_code is None
    assert conformance_calls == [], "generic-refine path must not re-run conformance"


def test_handle_critical_anomalies_rejects_empty_critical_anomalies() -> None:
    """An empty ``critical_anomalies`` violates the precondition and must
    raise ``ValueError`` rather than being silently skipped (as a bare
    ``assert`` would be under ``python -O``)."""
    orch = StrategyLabOrchestrator()

    with pytest.raises(
        ValueError, match="_handle_critical_anomalies requires at least one critical"
    ):
        orch._handle_critical_anomalies(
            state=_DesignAttemptState(spec=_spec(), code="# original code\n", trades=[], metrics=_metrics_for()),
            exec_result=_zero_trade_exec_result(),
            market_data=_market_data(),
            config=_config(),
            critical_anomalies=[],
            all_gate_results=[],
            refinement_attempts=[],
            zero_trade_attempts=[],
            round_num=0,
            stall_tracker=RefinementStallTracker(),
            emit=lambda *a, **k: None,
        )


@pytest.mark.parametrize("bad_market_data", [{}, None, "not-a-dict"])
def test_handle_critical_anomalies_rejects_invalid_market_data(bad_market_data) -> None:
    """Empty/non-dict ``market_data`` violates the precondition and must
    raise ``ValueError`` rather than being silently skipped (as a bare
    ``assert`` would be under ``python -O``)."""
    orch = StrategyLabOrchestrator()

    with pytest.raises(ValueError, match="market_data must be non-empty"):
        orch._handle_critical_anomalies(
            state=_DesignAttemptState(spec=_spec(), code="# original code\n", trades=[], metrics=_metrics_for()),
            exec_result=_zero_trade_exec_result(),
            market_data=bad_market_data,
            config=_config(),
            critical_anomalies=[_critical_anomaly()],
            all_gate_results=[],
            refinement_attempts=[],
            zero_trade_attempts=[],
            round_num=0,
            stall_tracker=RefinementStallTracker(),
            emit=lambda *a, **k: None,
        )


# ---------------------------------------------------------------------------
# ENTRY_WITH_NO_EXIT routing (#874)
# ---------------------------------------------------------------------------


class _SpyRepairer:
    """Records ``try_repair`` calls and returns a scripted outcome.

    ``outcome=None`` asserts the method is never reached — used by the
    ENTRY_WITH_NO_EXIT routing test, where the orchestrator must phase back
    to redesign *before* the code-only repair loop.
    """

    def __init__(self, outcome: Optional[_ZeroTradeRepairOutcome] = None) -> None:
        self._outcome = outcome
        self.calls = 0

    def try_repair(self, **_kwargs: Any) -> _ZeroTradeRepairOutcome:
        self.calls += 1
        assert self._outcome is not None, "try_repair must not be reached for this category"
        return self._outcome


def _entry_with_no_exit_exec_result() -> StrategyRunResult:
    """Diagnostics for a non-firing engine-owned exit: entries filled, zero
    closed trades, a position still open at the end of the window."""
    diagnostics = BacktestExecutionDiagnostics(
        zero_trade_category="ENTRY_WITH_NO_EXIT",
        summary="3 entries filled but no exit ever fired across 20 bars",
        bars_processed=20,
        orders_emitted=3,
        orders_accepted=3,
        entries_filled=3,
        exits_emitted=0,
        closed_trades=0,
        exit_rule_firings={},
        open_positions_at_end=[
            OpenPositionDiagnostic(
                symbol="AAPL",
                side="long",
                qty=10.0,
                entry_price=101.0,
                entry_timestamp="2023-01-05",
            )
        ],
    )
    return StrategyRunResult(success=True, trades=[], execution_diagnostics=diagnostics)


def test_entry_with_no_exit_routes_to_redesign() -> None:
    """``ENTRY_WITH_NO_EXIT`` has no valid code-level repair, so the
    orchestrator phases back to redesign / spec refinement via
    ``SpecImplementabilityError`` instead of entering the code-only repair
    loop or burning generic-refine rounds."""
    orch = StrategyLabOrchestrator()
    spy = _SpyRepairer(outcome=None)
    orch.zero_trade_repairer = spy  # type: ignore[assignment]
    events: List[tuple] = []

    spec = _spec()
    code = "# original code\n"
    with pytest.raises(SpecImplementabilityError) as excinfo:
        orch._handle_critical_anomalies(
            state=_DesignAttemptState(spec=spec, code=code, trades=[], metrics=_metrics_for()),
            exec_result=_entry_with_no_exit_exec_result(),
            market_data=_market_data(),
            config=_config(),
            critical_anomalies=[_critical_anomaly()],
            all_gate_results=[],
            refinement_attempts=[],
            zero_trade_attempts=[],
            round_num=0,
            stall_tracker=RefinementStallTracker(),
            emit=lambda phase, data: events.append((phase, data)),
        )

    err = excinfo.value
    assert err.failure_phase == "evaluation"
    assert err.last_spec is spec
    assert err.last_code == code
    assert "exit_rules" in err.evidence
    # The code-only repair loop must never be reached for this category.
    assert spy.calls == 0
    # A redesign-routing event is emitted for run-trace observability.
    assert any(
        phase == "coding" and data.get("sub_phase") == "routed_to_redesign"
        for phase, data in events
    )


def test_other_zero_trade_category_still_uses_code_repair() -> None:
    """A non-``ENTRY_WITH_NO_EXIT`` zero-trade category still flows through
    the specialised code-only repair loop — the routing guard is scoped to
    the one non-code-repairable category."""
    orch = StrategyLabOrchestrator()
    spy = _SpyRepairer(outcome=_committed_repair_outcome())
    orch.zero_trade_repairer = spy  # type: ignore[assignment]
    orch.predicate_conformance_gate.check = lambda code, spec, **kw: []

    recovery = orch._handle_critical_anomalies(
        state=_DesignAttemptState(spec=_spec(), code="# original code\n", trades=[], metrics=_metrics_for()),
        # NO_ORDERS_EMITTED -> specialised repair, not redesign routing.
        exec_result=_zero_trade_exec_result(),
        market_data=_market_data(),
        config=_config(),
        critical_anomalies=[_critical_anomaly()],
        all_gate_results=[],
        refinement_attempts=[],
        zero_trade_attempts=[],
        round_num=0,
        stall_tracker=RefinementStallTracker(),
        emit=lambda *a, **k: None,
    )

    assert spy.calls == 1, "code-only repair must run for NO_ORDERS_EMITTED"
    assert recovery.exhausted is False


def test_handle_critical_anomalies_propagates_budget_exhaustion_from_ztr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``zero_trade_repairer.try_repair`` trips ``DesignBudgetExhausted``
    (#1569 — the LLM-backed repair path now charges the active budget),
    ``_handle_critical_anomalies`` attaches the in-progress spec/code to the
    exception and re-raises rather than letting a bare/broader handler
    swallow it."""
    from investment_team.strategy_lab.agents._llm_budget import (
        DesignBudgetExhausted,
        LLMCallBudget,
        charge_active_budget,
        use_budget,
    )

    class _BudgetTrippingRepairer:
        def try_repair(self, **_kwargs: Any) -> _ZeroTradeRepairOutcome:
            charge_active_budget()
            raise AssertionError("must not be reached — charge_active_budget should have raised")

    orch = StrategyLabOrchestrator()
    orch.zero_trade_repairer = _BudgetTrippingRepairer()  # type: ignore[assignment]

    spent_budget = LLMCallBudget(1)
    spent_budget.charge()  # pre-exhaust: the repairer's own charge must trip it

    with use_budget(spent_budget):
        with pytest.raises(DesignBudgetExhausted) as exc_info:
            orch._handle_critical_anomalies(
                state=_DesignAttemptState(spec=_spec(), code="# original code\n", trades=[], metrics=_metrics_for()),
                exec_result=_zero_trade_exec_result(),
                market_data=_market_data(),
                config=_config(),
                critical_anomalies=[_critical_anomaly()],
                all_gate_results=[],
                refinement_attempts=[],
                zero_trade_attempts=[],
                round_num=0,
                stall_tracker=RefinementStallTracker(),
                emit=lambda *a, **k: None,
            )

    assert exc_info.value.latest_spec.strategy_id == _spec().strategy_id
    assert exc_info.value.latest_code == "# original code\n"


# ---------------------------------------------------------------------------
# ZeroTradeRepairAgent.run() — prompt content (direct, not through the
# orchestrator's whole-agent stub used by the tests above)
# ---------------------------------------------------------------------------


class _CapturingAgent:
    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self._payload


def _patch_zero_trade_repair(monkeypatch: pytest.MonkeyPatch, payload: str) -> _CapturingAgent:
    capture = _CapturingAgent(payload)
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents._agent_runner.Agent",
        lambda **_kwargs: capture,
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents._agent_runner.get_strands_model",
        lambda *_a, **_k: object(),
    )
    return capture


def test_prompt_embeds_response_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repair prompt carries the JSON Schema so the wire model and the
    downstream coercer cannot drift apart."""
    payload = '{"root_cause_category": "NO_ORDERS_EMITTED", "evidence": "e"}'
    capture = _patch_zero_trade_repair(monkeypatch, payload)

    ZeroTradeRepairAgent().run(
        spec=_spec(),
        code="# original",
        diagnostics=BacktestExecutionDiagnostics(zero_trade_category="NO_ORDERS_EMITTED"),
    )

    assert len(capture.calls) == 1
    prompt = capture.calls[0]
    assert "MUST conform to this JSON Schema" in prompt
    assert _ZERO_TRADE_REPAIR_SCHEMA_JSON in prompt


def test_embedded_schema_matches_format_constraint() -> None:
    """The schema embedded in the prompt is the same object exported from
    ``_response_schemas`` — the prompt-level contract cannot silently drift
    from whatever is validated elsewhere."""
    assert json.loads(_ZERO_TRADE_REPAIR_SCHEMA_JSON) == ZERO_TRADE_REPAIR_SCHEMA
    assert ZERO_TRADE_REPAIR_SCHEMA["required"] == ["root_cause_category"]


def test_wire_schema_category_enum_matches_canonical_zero_trade_category() -> None:
    """``_ZeroTradeRepairWire.root_cause_category`` is a local Literal (kept
    local so ``_response_schemas`` stays decoupled from ``models``, mirroring
    the ``_ExpectancyForecastWire`` / ``risk_limits`` rationale in that file)
    rather than importing ``ZeroTradeCategory`` directly. This test is the
    guard that keeps the two lists from drifting apart."""
    schema_categories = frozenset(ZERO_TRADE_REPAIR_SCHEMA["properties"]["root_cause_category"]["enum"])
    assert schema_categories == frozenset(get_args(ZeroTradeCategory))


def test_run_propagates_budget_exhaustion_not_fallback_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ZeroTradeRepairAgent.run`` now charges the active per-cycle LLM
    budget (#1569). When the budget is already spent, ``DesignBudgetExhausted``
    must propagate to the caller, NOT be swallowed into the agent's own
    fallback-report ``except Exception`` branch (which would silently mask a
    budget trip as an ordinary parse failure)."""
    from investment_team.strategy_lab.agents._llm_budget import (
        DesignBudgetExhausted,
        LLMCallBudget,
        use_budget,
    )

    _patch_zero_trade_repair(monkeypatch, '{"root_cause_category": "NO_ORDERS_EMITTED"}')

    spent_budget = LLMCallBudget(1)
    spent_budget.charge()  # pre-exhaust: the agent's own charge must trip it

    with use_budget(spent_budget):
        with pytest.raises(DesignBudgetExhausted):
            ZeroTradeRepairAgent().run(
                spec=_spec(),
                code="# original",
                diagnostics=BacktestExecutionDiagnostics(zero_trade_category="NO_ORDERS_EMITTED"),
            )


# ---------------------------------------------------------------------------
# `_coerce_report` category round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", get_args(ZeroTradeCategory))
def test_coerce_report_round_trips_every_canonical_zero_trade_category(
    category: str,
) -> None:
    """Every category in the canonical ZeroTradeCategory Literal must survive
    `_coerce_report` unchanged rather than being silently overwritten by the
    fallback — a hand-maintained allow-list that falls out of sync with the
    Literal would otherwise mask a category the LLM legitimately returns."""
    parsed: Dict[str, Any] = {"root_cause_category": category}

    report = _coerce_report(parsed, fallback_category="UNKNOWN_ZERO_TRADE_PATH")

    assert report.root_cause_category == category


def test_coerce_report_falls_back_on_genuinely_invalid_category() -> None:
    """A category value that is not one of the canonical ZeroTradeCategory
    members must still fall back to `fallback_category`."""
    parsed: Dict[str, Any] = {"root_cause_category": "NOT_A_REAL_CATEGORY"}

    report = _coerce_report(parsed, fallback_category="ORDERS_REJECTED")

    assert report.root_cause_category == "ORDERS_REJECTED"
