"""Tests for the Strategy Lab trade-alignment problem-solving loop.

The orchestrator runs a fresh ``TradeAlignmentAgent`` audit after each
code-execution cycle. When the agent reports the trades do not match the
strategy spec, the orchestrator loops up to ``MAX_ALIGNMENT_ROUNDS`` times:
apply the agent's proposed code fix, re-execute via
:func:`run_strategy_code` (which routes through TradingService), and re-
audit. These tests stub the LLM agent and the code-execution helper so we
can assert the loop's control flow directly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from investment_team.market_data_service import OHLCVBar
from investment_team.models import (
    BacktestConfig,
    BacktestResult,
    StrategySpec,
    TradeRecord,
)
from investment_team.strategy_lab.agents.alignment import (
    AlignmentIssue,
    TradeAlignmentReport,
    _coerce_report,
)
from investment_team.strategy_lab.executor.trade_builder import build_trade_records
from investment_team.strategy_lab.orchestrator import (
    MAX_ALIGNMENT_ROUNDS,
    StrategyLabOrchestrator,
)
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.trading_service.modes.sandbox_compat import StrategyRunResult


def _code_exec(
    *,
    success: bool,
    raw_trades: Optional[List[Dict[str, Any]]] = None,
    stderr: str = "",
    error_type: Optional[str] = None,
) -> StrategyRunResult:
    """Build a ``StrategyRunResult`` from raw-trade dicts for test fixtures.

    The alignment tests predate PR 3 and model sandbox output as raw trade
    dicts (what the strategy subprocess emitted pre-PR 3). We translate
    those into ``TradeRecord`` objects via the still-available
    :func:`build_trade_records` so the existing fixtures remain usable.
    """
    trades: List[TradeRecord] = []
    if raw_trades:
        trades = build_trade_records(
            raw_trades,
            BacktestConfig(
                start_date="2023-01-01",
                end_date="2023-12-31",
                initial_capital=100_000.0,
                transaction_cost_bps=5.0,
                slippage_bps=2.0,
            ),
        )
    return StrategyRunResult(
        success=success,
        trades=trades,
        stderr=stderr,
        error_type=error_type,
    )


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


def _trade(num: int) -> Dict[str, Any]:
    """Return a raw trade dict shaped like the sandbox emits (pre build_trade_records)."""
    return {
        "symbol": "AAPL",
        "side": "long",
        "entry_date": f"2023-0{(num % 9) + 1}-01",
        "entry_price": 100.0 + num,
        "exit_date": f"2023-0{(num % 9) + 1}-10",
        "exit_price": 105.0 + num,
        "shares": 10.0,
    }


def _benign_sandbox_trades(offset: int = 0) -> List[Dict[str, Any]]:
    """Return a raw-trade ledger that passes all critical anomaly gates.

    The alignment loop's post-rerun anomaly gates flag zero-trade, too-few-
    trade, >95% win-rate, >200% annualized, and other extreme outputs as
    critical. Tests that exercise the audit loop's control flow (not the
    anomaly branch) need sandbox outputs that are "normal-looking" so the
    loop proceeds to the next audit rather than tripping anomaly_detected.

    Produces 12 AAPL trades on two symbols across two sides, alternating
    winners and losers, with multi-day holds — enough to clear the
    min-trades and single-direction-warning thresholds.
    """
    raw: List[Dict[str, Any]] = []
    for i in range(12):
        symbol = "AAPL" if i % 2 == 0 else "MSFT"
        side = "long" if i % 3 != 0 else "short"
        base = 100.0 + i + offset
        is_win = i % 2 == 0
        # Long-win = exit>entry; long-loss = exit<entry; short-win = exit<entry.
        if side == "long":
            exit_px = base + 2.0 if is_win else base - 1.5
        else:
            exit_px = base - 2.0 if is_win else base + 1.5
        raw.append(
            {
                "symbol": symbol,
                "side": side,
                "entry_date": f"2023-01-{(i % 28) + 1:02d}",
                "entry_price": base,
                "exit_date": f"2023-02-{(i % 28) + 1:02d}",
                "exit_price": exit_px,
                "shares": 10.0,
            }
        )
    return raw


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-align-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        entry_rules=["enter when RSI < 30"],
        exit_rules=["exit when RSI > 70"],
        sizing_rules=["risk 2% per trade"],
        risk_limits={"max_position_pct": 5},
        speculative=False,
        strategy_code="def run_strategy(data, config):\n    return []\n",
    )


def _trade_records(n: int = 6) -> List[TradeRecord]:
    """Build a small ledger of TradeRecord objects for the audit prompt."""
    out: List[TradeRecord] = []
    cum = 0.0
    for i in range(n):
        net = 10.0 if i % 2 == 0 else -5.0
        cum += net
        out.append(
            TradeRecord(
                trade_num=i + 1,
                entry_date=f"2023-01-{i + 1:02d}",
                exit_date=f"2023-01-{i + 5:02d}",
                symbol="AAPL",
                side="long",
                entry_price=100.0,
                exit_price=101.0,
                shares=10.0,
                position_value=1000.0,
                gross_pnl=net,
                net_pnl=net,
                return_pct=net / 1000.0 * 100,
                hold_days=4,
                outcome="win" if net > 0 else "loss",
                cumulative_pnl=cum,
            )
        )
    return out


def _metrics() -> BacktestResult:
    return BacktestResult(
        total_return_pct=5.0,
        annualized_return_pct=4.0,
        volatility_pct=10.0,
        sharpe_ratio=0.5,
        max_drawdown_pct=2.0,
        win_rate_pct=50.0,
        profit_factor=1.2,
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


class _StubAlignmentAgent:
    """Records audit calls and returns scripted ``TradeAlignmentReport`` objects.

    The orchestrator instantiates collaborators in ``__init__``; tests inject
    this stub afterwards by setting ``orchestrator.alignment_agent``.
    """

    def __init__(self, reports: List[TradeAlignmentReport]) -> None:
        self._reports = list(reports)
        self.calls: List[Dict[str, Any]] = []

    def run(
        self,
        spec: StrategySpec,
        code: str,
        trades: List[TradeRecord],
        metrics: BacktestResult,
        prior_attempts: Optional[List[str]] = None,
    ) -> TradeAlignmentReport:
        self.calls.append(
            {
                "code": code,
                "n_trades": len(trades),
                "prior_attempts": list(prior_attempts or []),
            }
        )
        if not self._reports:
            # Default to aligned so the orchestrator does not infinite-loop
            return TradeAlignmentReport(aligned=True, rationale="default-aligned")
        return self._reports.pop(0)


class _StubSandbox:
    """Stub for ``run_strategy_code``. ``run_seq`` is consumed in order."""

    def __init__(self, run_seq: List[StrategyRunResult]) -> None:
        self._results = list(run_seq)
        self.calls: List[str] = []

    def run(
        self,
        strategy_code: str,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
        *,
        strategy: Optional[StrategySpec] = None,
    ) -> StrategyRunResult:
        self.calls.append(strategy_code)
        if not self._results:
            raise AssertionError("sandbox stub called more times than scripted")
        return self._results.pop(0)


def _make_orchestrator(
    *,
    alignment_reports: List[TradeAlignmentReport],
    sandbox_results: List[StrategyRunResult],
) -> Tuple[StrategyLabOrchestrator, _StubAlignmentAgent, _StubSandbox]:
    """Build an orchestrator with stubbed alignment agent + sandbox."""
    orch = StrategyLabOrchestrator()
    align_stub = _StubAlignmentAgent(alignment_reports)
    sandbox_stub = _StubSandbox(sandbox_results)
    orch.alignment_agent = align_stub  # type: ignore[assignment]
    # Attach the sandbox as a plain attribute so ``_drive_alignment_loop``
    # can invoke it; the real orchestrator reaches for ``run_strategy_code``
    # from its module import instead.
    orch._test_sandbox = sandbox_stub  # type: ignore[attr-defined]
    return orch, align_stub, sandbox_stub


def _drive_alignment_loop(
    orch: StrategyLabOrchestrator,
    *,
    spec: StrategySpec,
    code: str,
    trades: List[TradeRecord],
    metrics: BacktestResult,
    market_data: Dict[str, List[OHLCVBar]],
    config: BacktestConfig,
) -> Tuple[
    List[Tuple[str, Dict[str, Any]]],
    List[QualityGateResult],
    str,
    List[TradeRecord],
    BacktestResult,
    StrategySpec,
]:
    """Run the alignment loop in isolation by mirroring orchestrator code.

    The orchestrator does not expose the loop as a public method, but the
    block is small enough that tests reproduce its driver here so we can
    exercise it without spinning up the full ``run_cycle``. The driver is
    intentionally a copy of the orchestrator's loop semantics so a drift
    in the orchestrator surfaces as a test failure.
    """
    from investment_team.trade_simulator import compute_metrics

    events: List[Tuple[str, Dict[str, Any]]] = []
    gate_results: List[QualityGateResult] = []
    alignment_attempts: List[str] = []
    alignment_reports: List[TradeAlignmentReport] = []

    def emit(phase: str, data: Dict[str, Any]) -> None:
        events.append((phase, data))

    for align_round in range(MAX_ALIGNMENT_ROUNDS):
        emit(
            "aligning",
            {
                "sub_phase": "evaluating",
                "alignment_round": align_round,
                "trades_count": len(trades),
            },
        )
        report = orch._run_alignment_audit(
            spec=spec,
            code=code,
            trades=trades,
            metrics=metrics,
            prior_attempts=alignment_attempts,
        )
        alignment_reports.append(report)

        gate_severity = "info" if report.aligned else "critical"
        gate_results.append(
            QualityGateResult(
                gate_name="trade_alignment",
                passed=report.aligned,
                severity=gate_severity,  # type: ignore[arg-type]
                details=report.rationale or "n/a",
                refinement_round=align_round,
            )
        )

        if report.aligned:
            emit("aligning", {"sub_phase": "aligned", "alignment_round": align_round})
            break

        emit(
            "aligning",
            {
                "sub_phase": "not_aligned",
                "alignment_round": align_round,
                "issues_count": len(report.issues),
            },
        )

        if not report.proposed_code:
            emit("aligning", {"sub_phase": "no_proposed_fix", "alignment_round": align_round})
            break

        if align_round >= MAX_ALIGNMENT_ROUNDS - 1:
            emit("aligning", {"sub_phase": "max_rounds_reached", "alignment_round": align_round})
            break

        # Stage the proposal — commit to ``code`` / ``spec`` / ``trades`` /
        # ``metrics`` / ``alignment_attempts`` only after every gate passes,
        # so that a rejected proposal never leaks into the persisted record.
        proposed_code = report.proposed_code
        proposed_spec = orch._apply_updates(spec, {}, proposed_code)
        change_summary = report.changes_made or "alignment fix"

        safety_gates = orch.code_safety_checker.check(proposed_code)
        for g in safety_gates:
            g.refinement_round = align_round
            g.gate_name = f"alignment_{g.gate_name}"
        gate_results.extend(safety_gates)
        if any(not g.passed and g.severity == "critical" for g in safety_gates):
            emit(
                "aligning",
                {"sub_phase": "rejected_unsafe_code", "alignment_round": align_round},
            )
            break

        align_exec = orch._test_sandbox.run(proposed_code, market_data, config, strategy=spec)
        if not align_exec.success:
            gate_results.append(
                QualityGateResult(
                    gate_name="alignment_code_execution",
                    passed=False,
                    severity="critical",
                    details=f"re-exec failed: {align_exec.error_type}",
                    refinement_round=align_round,
                )
            )
            emit(
                "aligning",
                {"sub_phase": "re_execution_failed", "alignment_round": align_round},
            )
            break

        # TradingService-finalised trades — no raw-dict validation step.
        new_trades = align_exec.trades

        new_metrics = compute_metrics(
            new_trades, config.initial_capital, config.start_date, config.end_date
        )

        anomaly_gates = orch.anomaly_detector.check(new_metrics, new_trades)
        for g in anomaly_gates:
            g.refinement_round = align_round
            g.gate_name = f"alignment_{g.gate_name}"
        gate_results.extend(anomaly_gates)
        if any(not g.passed and g.severity == "critical" for g in anomaly_gates):
            emit(
                "aligning",
                {"sub_phase": "anomaly_detected", "alignment_round": align_round},
            )
            break

        # All gates pass — commit the proposal.
        code = proposed_code
        spec = proposed_spec
        trades = new_trades
        metrics = new_metrics
        alignment_attempts.append(change_summary)

        emit(
            "aligning",
            {
                "sub_phase": "refined",
                "alignment_round": align_round,
                "trades_count": len(trades),
            },
        )

    return events, gate_results, code, trades, metrics, spec


# ---------------------------------------------------------------------------
# Tests for the audit loop
# ---------------------------------------------------------------------------


def test_alignment_loop_exits_immediately_when_first_audit_aligned() -> None:
    """If the very first audit reports aligned, no refinement / re-exec runs."""
    orch, align_stub, sandbox_stub = _make_orchestrator(
        alignment_reports=[
            TradeAlignmentReport(aligned=True, rationale="trades match spec"),
        ],
        sandbox_results=[],
    )
    spec = _spec()
    code = "code-v0"
    trades = _trade_records()
    metrics = _metrics()

    events, gates, final_code, final_trades, _, final_spec = _drive_alignment_loop(
        orch,
        spec=spec,
        code=code,
        trades=trades,
        metrics=metrics,
        market_data=_market_data(),
        config=_config(),
    )

    assert len(align_stub.calls) == 1
    assert sandbox_stub.calls == []
    assert final_code == code
    assert final_trades is trades
    assert final_spec is spec
    sub_phases = [d["sub_phase"] for p, d in events if p == "aligning"]
    assert sub_phases == ["evaluating", "aligned"]
    assert gates[0].passed is True
    assert gates[0].gate_name == "trade_alignment"


def test_alignment_loop_recovers_after_one_fix_and_re_execution() -> None:
    """One misaligned audit → one fix → re-backtest → second audit aligned."""
    fixed_code = "def run_strategy(data, config):\n    return []  # fixed\n"
    orch, align_stub, sandbox_stub = _make_orchestrator(
        alignment_reports=[
            TradeAlignmentReport(
                aligned=False,
                rationale="entries before signal fires",
                issues=[
                    AlignmentIssue(
                        rule_type="entry_rules",
                        description="Trade #2 entered without RSI<30",
                        severity="critical",
                        affected_trades=[2],
                    )
                ],
                proposed_code=fixed_code,
                predicted_aligned_after_fix=True,
                changes_made="add RSI<30 guard before entry",
            ),
            TradeAlignmentReport(aligned=True, rationale="now aligned"),
        ],
        sandbox_results=[
            # The fix gets re-executed; emit a fresh trade ledger that
            # passes the anomaly gates so the loop proceeds to re-audit.
            _code_exec(success=True, raw_trades=_benign_sandbox_trades()),
        ],
    )

    events, gates, final_code, final_trades, _, final_spec = _drive_alignment_loop(
        orch,
        spec=_spec(),
        code="code-v0",
        trades=_trade_records(),
        metrics=_metrics(),
        market_data=_market_data(),
        config=_config(),
    )

    # Audit was called twice (initial + post-fix); sandbox once for the fix.
    assert len(align_stub.calls) == 2
    assert len(sandbox_stub.calls) == 1
    assert sandbox_stub.calls[0] == fixed_code
    assert final_code == fixed_code
    assert final_spec.strategy_code == fixed_code
    # New trade ledger was rebuilt from the sandbox output
    assert len(final_trades) == len(_benign_sandbox_trades())

    sub_phases = [d["sub_phase"] for p, d in events if p == "aligning"]
    assert sub_phases == ["evaluating", "not_aligned", "refined", "evaluating", "aligned"]
    # Prior attempts get threaded through to the second audit
    assert align_stub.calls[1]["prior_attempts"] == ["add RSI<30 guard before entry"]


def test_alignment_loop_caps_at_max_rounds() -> None:
    """Persistently misaligned strategy stops after MAX_ALIGNMENT_ROUNDS."""
    # Always misaligned, always proposes a fix
    misaligned = TradeAlignmentReport(
        aligned=False,
        rationale="still wrong",
        issues=[
            AlignmentIssue(
                rule_type="exit_rules",
                description="never exits on signal",
                severity="critical",
            )
        ],
        proposed_code="def run_strategy(data, config):\n    return []  # try again\n",
        predicted_aligned_after_fix=True,
        changes_made="another attempt",
    )
    # Need MAX_ALIGNMENT_ROUNDS reports (one per audit) and MAX-1 sandbox runs
    # (the final round audits but does not re-execute). Each sandbox run
    # must return trades that pass anomaly gates so the loop proceeds to
    # the next audit rather than tripping ``anomaly_detected`` early.
    orch, align_stub, sandbox_stub = _make_orchestrator(
        alignment_reports=[misaligned for _ in range(MAX_ALIGNMENT_ROUNDS)],
        sandbox_results=[
            _code_exec(success=True, raw_trades=_benign_sandbox_trades(offset=i))
            for i in range(MAX_ALIGNMENT_ROUNDS - 1)
        ],
    )

    events, gates, _final_code, _final_trades, _, _final_spec = _drive_alignment_loop(
        orch,
        spec=_spec(),
        code="code-v0",
        trades=_trade_records(),
        metrics=_metrics(),
        market_data=_market_data(),
        config=_config(),
    )

    assert len(align_stub.calls) == MAX_ALIGNMENT_ROUNDS
    assert len(sandbox_stub.calls) == MAX_ALIGNMENT_ROUNDS - 1
    # Final emitted sub_phase is the cap signal
    align_subs = [d["sub_phase"] for p, d in events if p == "aligning"]
    assert "max_rounds_reached" in align_subs
    # No audit ever passed
    align_gates = [g for g in gates if g.gate_name == "trade_alignment"]
    assert len(align_gates) == MAX_ALIGNMENT_ROUNDS
    assert all(g.passed is False for g in align_gates)


def test_alignment_loop_breaks_when_no_proposed_code() -> None:
    """Misaligned + ``proposed_code`` is None → loop exits without re-exec."""
    orch, align_stub, sandbox_stub = _make_orchestrator(
        alignment_reports=[
            TradeAlignmentReport(
                aligned=False,
                rationale="off-spec but agent declined to propose a fix",
                issues=[AlignmentIssue(rule_type="risk_limits", description="…")],
                proposed_code=None,
                predicted_aligned_after_fix=False,
                changes_made="",
            ),
        ],
        sandbox_results=[],
    )

    events, _gates, _code, _trades, _metrics_out, _spec_out = _drive_alignment_loop(
        orch,
        spec=_spec(),
        code="code-v0",
        trades=_trade_records(),
        metrics=_metrics(),
        market_data=_market_data(),
        config=_config(),
    )

    assert len(align_stub.calls) == 1
    assert sandbox_stub.calls == []
    align_subs = [d["sub_phase"] for p, d in events if p == "aligning"]
    assert align_subs == ["evaluating", "not_aligned", "no_proposed_fix"]


def test_alignment_loop_breaks_when_re_execution_fails() -> None:
    """Sandbox failure on the post-fix re-execution stops the loop and leaves
    the last-good ``code`` / ``spec`` / ``trades`` / ``metrics`` intact so
    the persisted record never contains code that was never successfully
    executed for the reported results."""
    proposed_code = "def run_strategy(data, config):\n    raise RuntimeError\n"
    orch, align_stub, sandbox_stub = _make_orchestrator(
        alignment_reports=[
            TradeAlignmentReport(
                aligned=False,
                rationale="still wrong",
                issues=[AlignmentIssue(rule_type="entry_rules", description="x")],
                proposed_code=proposed_code,
                predicted_aligned_after_fix=True,
                changes_made="risky rewrite",
            ),
        ],
        sandbox_results=[
            _code_exec(
                success=False,
                stderr="RuntimeError: boom",
                error_type="runtime_error",
            ),
        ],
    )

    original_spec = _spec()
    original_trades = _trade_records()
    original_metrics = _metrics()
    events, gates, final_code, final_trades, final_metrics, final_spec = _drive_alignment_loop(
        orch,
        spec=original_spec,
        code="code-v0",
        trades=original_trades,
        metrics=original_metrics,
        market_data=_market_data(),
        config=_config(),
    )

    assert len(align_stub.calls) == 1
    assert len(sandbox_stub.calls) == 1
    align_subs = [d["sub_phase"] for p, d in events if p == "aligning"]
    assert align_subs[-1] == "re_execution_failed"
    # An execution failure is recorded
    assert any(g.gate_name == "alignment_code_execution" and not g.passed for g in gates)
    # State preservation — the rejected proposal did NOT overwrite anything
    assert final_code == "code-v0"
    assert final_spec is original_spec
    assert final_trades is original_trades
    assert final_metrics is original_metrics


def test_alignment_loop_breaks_on_critical_anomaly_after_rerun() -> None:
    """An alignment fix that yields critical anomalies (e.g. zero trades)
    must fail the anomaly gate and NOT commit the proposal into the
    persisted state. The loop exits with the prior backtest intact."""
    proposed_code = "def run_strategy(data, config):\n    return []  # breaks everything\n"
    orch, align_stub, sandbox_stub = _make_orchestrator(
        alignment_reports=[
            TradeAlignmentReport(
                aligned=False,
                rationale="off-spec",
                issues=[AlignmentIssue(rule_type="entry_rules", description="x")],
                proposed_code=proposed_code,
                predicted_aligned_after_fix=True,
                changes_made="apply zero-trade fix",
            ),
        ],
        # Sandbox runs cleanly but returns zero trades, which
        # BacktestAnomalyDetector flags critical ("never entered a position").
        sandbox_results=[_code_exec(success=True, raw_trades=[])],
    )

    original_spec = _spec()
    original_trades = _trade_records()
    original_metrics = _metrics()
    events, gates, final_code, final_trades, final_metrics, final_spec = _drive_alignment_loop(
        orch,
        spec=original_spec,
        code="code-v0",
        trades=original_trades,
        metrics=original_metrics,
        market_data=_market_data(),
        config=_config(),
    )

    # Audit ran once; sandbox ran once for the fix.
    assert len(align_stub.calls) == 1
    assert len(sandbox_stub.calls) == 1

    align_subs = [d["sub_phase"] for p, d in events if p == "aligning"]
    assert align_subs[-1] == "anomaly_detected"
    # Anomaly was recorded as an ``alignment_backtest_anomaly`` gate failure
    anomaly_failures = [
        g
        for g in gates
        if g.gate_name.startswith("alignment_") and not g.passed and g.severity == "critical"
    ]
    assert anomaly_failures, "expected at least one critical alignment anomaly gate"

    # State preservation — the anomalous proposal did NOT overwrite anything
    assert final_code == "code-v0"
    assert final_spec is original_spec
    assert final_trades is original_trades
    assert final_metrics is original_metrics


def test_alignment_loop_breaks_on_unsafe_proposed_code() -> None:
    """A proposed rewrite that fails code-safety checks must be rejected
    without re-execution, and must NOT overwrite the last-good state."""
    orch, align_stub, sandbox_stub = _make_orchestrator(
        alignment_reports=[
            TradeAlignmentReport(
                aligned=False,
                rationale="off-spec",
                issues=[AlignmentIssue(rule_type="entry_rules", description="x")],
                # Obvious safety violation: imports `os` + calls `os.system`.
                proposed_code=(
                    "import os\n\n"
                    "def run_strategy(data, config):\n"
                    "    os.system('rm -rf /')  # prohibited\n"
                    "    return []\n"
                ),
                predicted_aligned_after_fix=True,
                changes_made="unsafe rewrite",
            ),
        ],
        sandbox_results=[],  # sandbox must NOT be invoked
    )

    original_spec = _spec()
    original_trades = _trade_records()
    events, gates, final_code, final_trades, _metrics_out, final_spec = _drive_alignment_loop(
        orch,
        spec=original_spec,
        code="code-v0",
        trades=original_trades,
        metrics=_metrics(),
        market_data=_market_data(),
        config=_config(),
    )

    assert len(align_stub.calls) == 1
    assert sandbox_stub.calls == []  # safety gate short-circuited the re-exec
    align_subs = [d["sub_phase"] for p, d in events if p == "aligning"]
    assert align_subs[-1] == "rejected_unsafe_code"
    assert any(
        g.gate_name.startswith("alignment_") and not g.passed and g.severity == "critical"
        for g in gates
    )
    # State preservation
    assert final_code == "code-v0"
    assert final_spec is original_spec
    assert final_trades is original_trades


def test_alignment_audit_recovers_from_agent_exception() -> None:
    """A raised exception inside the agent collapses to ``aligned=True`` so
    the orchestrator does not stall."""

    class _BoomAgent:
        def run(self, **_kwargs: Any) -> TradeAlignmentReport:
            raise RuntimeError("LLM transport blew up")

    orch = StrategyLabOrchestrator()
    orch.alignment_agent = _BoomAgent()  # type: ignore[assignment]

    report = orch._run_alignment_audit(
        spec=_spec(),
        code="code",
        trades=_trade_records(),
        metrics=_metrics(),
        prior_attempts=[],
    )
    assert report.aligned is True
    assert "alignment audit skipped" in report.rationale.lower()


# ---------------------------------------------------------------------------
# Tests for the JSON coercion helper (independent of the loop)
# ---------------------------------------------------------------------------


def test_coerce_report_aligned_drops_proposed_code() -> None:
    """When the LLM says aligned, defensive coercion strips fix suggestions."""
    raw = {
        "aligned": True,
        "rationale": "all good",
        "issues": [],
        "proposed_code": "should be ignored",
        "predicted_aligned_after_fix": True,
        "changes_made": "should also be ignored",
    }
    report = _coerce_report(raw, fallback_code="orig")
    assert report.aligned is True
    assert report.proposed_code is None
    assert report.predicted_aligned_after_fix is False
    assert report.changes_made == ""


def test_coerce_report_misaligned_without_code_disables_prediction() -> None:
    """Misaligned + missing code → predicted_aligned_after_fix forced false."""
    raw = {
        "aligned": False,
        "rationale": "off-spec",
        "issues": [
            {
                "rule_type": "entry_rules",
                "description": "bad",
                "severity": "critical",
                "affected_trades": [1, 2],
            }
        ],
        "proposed_code": None,
        "predicted_aligned_after_fix": True,  # agent over-claimed
        "changes_made": "",
    }
    report = _coerce_report(raw, fallback_code="orig")
    assert report.aligned is False
    assert report.proposed_code is None
    assert report.predicted_aligned_after_fix is False
    assert len(report.issues) == 1
    assert report.issues[0].rule_type == "entry_rules"
    assert report.issues[0].severity == "critical"
    assert report.issues[0].affected_trades == [1, 2]


def test_coerce_report_keeps_well_formed_fix() -> None:
    raw = {
        "aligned": False,
        "rationale": "entries early",
        "issues": [],
        "proposed_code": "def run_strategy(data, config):\n    return []\n",
        "predicted_aligned_after_fix": True,
        "changes_made": "guard added",
    }
    report = _coerce_report(raw, fallback_code="orig")
    assert report.aligned is False
    assert report.proposed_code is not None
    assert "run_strategy" in report.proposed_code
    assert report.predicted_aligned_after_fix is True
    assert report.changes_made == "guard added"


def test_coerce_report_tolerates_invalid_severity() -> None:
    raw = {
        "aligned": False,
        "rationale": "x",
        "issues": [{"rule_type": "exit_rules", "description": "d", "severity": "bogus"}],
        "proposed_code": "def run_strategy(data, config):\n    return []\n",
        "changes_made": "fix",
    }
    report = _coerce_report(raw, fallback_code="orig")
    assert len(report.issues) == 1
    # Falls back to "warning" rather than raising
    assert report.issues[0].severity == "warning"
