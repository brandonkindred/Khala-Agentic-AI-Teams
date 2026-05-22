"""Tests for the Strategy Lab trade-alignment problem-solving loop.

The orchestrator runs a deterministic alignment gate
(:class:`DeterministicAlignmentChecker`) after each code-execution
cycle. When the gate reports the trades do not match the strategy spec,
the orchestrator asks :meth:`TradeAlignmentAgent.propose_code_fix` for
a rewritten strategy file, re-executes via :func:`run_strategy_code`,
and re-checks — up to ``MAX_ALIGNMENT_ROUNDS`` times. These tests stub
both the gate and the LLM agent so we can assert the loop's control
flow directly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from investment_team.market_data_service import OHLCVBar
from investment_team.models import (
    BacktestConfig,
    BacktestResult,
    StrategySpec,
    TradeRecord,
)
from investment_team.strategy_lab.agents.alignment import (
    AlignmentAuditError,
    AlignmentIssue,
    TradeAlignmentReport,
    _coerce_report,
)
from investment_team.strategy_lab.alignment_findings import (
    AlignmentFinding,
    NearMissVerdict,
)
from investment_team.strategy_lab.executor.trade_builder import build_trade_records
from investment_team.strategy_lab.orchestrator import (
    MAX_ALIGNMENT_ROUNDS,
    StrategyLabOrchestrator,
)
from investment_team.strategy_lab.quality_gates.alignment_checks import (
    AlignmentCheckResult,
)
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)
from investment_team.trading_service.modes.sandbox_compat import StrategyRunResult


def _code_exec(
    *,
    success: bool,
    raw_trades: Optional[List[Dict[str, Any]]] = None,
    stderr: str = "",
    error_type: Optional[str] = None,
) -> StrategyRunResult:
    """Build a ``StrategyRunResult`` from raw-trade dicts for test fixtures.

    Pre: ``raw_trades`` entries match the shape ``build_trade_records``
    accepts.
    Post: the returned ``StrategyRunResult`` carries
    :class:`TradeRecord` objects (or empty list when ``raw_trades`` is
    ``None``).
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


def _benign_sandbox_trades(offset: int = 0) -> List[Dict[str, Any]]:
    """Raw-trade ledger that passes the post-rerun anomaly gates.

    Twelve AAPL/MSFT trades alternating winners and losers, multi-day
    holds — enough to clear the min-trades and single-direction-warning
    thresholds. Used when the loop's control flow (not the anomaly
    branch) is under test.
    """
    raw: List[Dict[str, Any]] = []
    for i in range(12):
        symbol = "AAPL" if i % 2 == 0 else "MSFT"
        side = "long" if i % 3 != 0 else "short"
        base = 100.0 + i + offset
        is_win = i % 2 == 0
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
            "        pass\n"
        ),
    )


def _trade_records(n: int = 6) -> List[TradeRecord]:
    """Build a small ledger of TradeRecord objects."""
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
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
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


def _aligned_check_result() -> AlignmentCheckResult:
    """Verdict the stub gate returns for ``aligned=True`` paths."""
    findings = [
        AlignmentFinding(
            trade_num=1,
            rule_id="universe",
            check_name="universe",
            passed=True,
            severity="info",
            details="aligned",
        )
    ]
    return AlignmentCheckResult(
        aligned=True,
        findings=findings,
        gate_results=[],
        rationale="all green",
    )


def _misaligned_check_result(severity: str = "critical") -> AlignmentCheckResult:
    """Verdict the stub gate returns for ``aligned=False`` paths."""
    findings = [
        AlignmentFinding(
            trade_num=2,
            rule_id="entry[0]",
            check_name="entry_signal",
            passed=False,
            severity=severity,  # type: ignore[arg-type]
            details="rsi above 30 at entry",
            computed_value=42.0,
            expected_value=30.0,
        )
    ]
    aligned = severity != "critical"
    return AlignmentCheckResult(
        aligned=aligned,
        findings=findings,
        gate_results=[],
        rationale="critical findings" if not aligned else "diagnostic only",
    )


class _StubChecker:
    """Records check() calls and returns scripted ``AlignmentCheckResult``s.

    The orchestrator owns ``deterministic_alignment_checker`` as an
    attribute; tests inject this stub after construction.
    """

    def __init__(self, results: List[AlignmentCheckResult]) -> None:
        self._results = list(results)
        self.calls: List[Dict[str, Any]] = []

    def check(self, **kwargs: Any) -> AlignmentCheckResult:
        self.calls.append({"n_trades": len(kwargs.get("trades", []))})
        if not self._results:
            # Default to aligned so the loop terminates rather than
            # going infinite on under-scripted tests.
            return _aligned_check_result()
        return self._results.pop(0)


class _StubAlignmentAgent:
    """Records propose_code_fix calls; returns scripted reports.

    Also exposes :meth:`adjudicate_near_miss` so the deterministic
    gate's near-miss path is wirable. Tests rarely exercise the
    near-miss arm directly because the stub gate short-circuits with
    pre-canned ``AlignmentCheckResult``s.
    """

    def __init__(
        self,
        propose_results: Optional[List[TradeAlignmentReport]] = None,
        near_miss_verdicts: Optional[List[NearMissVerdict]] = None,
    ) -> None:
        self._propose_results = list(propose_results or [])
        self._near_miss_verdicts = list(near_miss_verdicts or [])
        self.calls: List[Dict[str, Any]] = []
        self.near_miss_calls: List[Dict[str, Any]] = []

    def propose_code_fix(
        self,
        *,
        spec: StrategySpec,
        code: str,
        findings: List[AlignmentFinding],
        prior_attempts: Optional[List[str]] = None,
    ) -> TradeAlignmentReport:
        self.calls.append(
            {
                "code": code,
                "n_findings": len(findings),
                "prior_attempts": list(prior_attempts or []),
            }
        )
        if not self._propose_results:
            return TradeAlignmentReport(
                aligned=False,
                rationale="default-misaligned",
                proposed_code=None,
            )
        return self._propose_results.pop(0)

    def adjudicate_near_miss(self, **kwargs: Any) -> NearMissVerdict:
        self.near_miss_calls.append(dict(kwargs))
        if not self._near_miss_verdicts:
            return NearMissVerdict(legitimate=False, rationale="default-deny")
        return self._near_miss_verdicts.pop(0)


def _make_orchestrator(
    *,
    check_results: List[AlignmentCheckResult],
    propose_results: Optional[List[TradeAlignmentReport]] = None,
    near_miss_verdicts: Optional[List[NearMissVerdict]] = None,
) -> Tuple[StrategyLabOrchestrator, _StubAlignmentAgent, _StubChecker]:
    """Build an orchestrator with stubbed alignment checker + agent."""
    orch = StrategyLabOrchestrator()
    align_stub = _StubAlignmentAgent(
        propose_results=propose_results,
        near_miss_verdicts=near_miss_verdicts,
    )
    checker_stub = _StubChecker(check_results)
    orch.alignment_agent = align_stub  # type: ignore[assignment]
    orch.deterministic_alignment_checker = checker_stub  # type: ignore[assignment]
    return orch, align_stub, checker_stub


def _collect_emit() -> Tuple[List[Tuple[str, Dict[str, Any]]], Any]:
    events: List[Tuple[str, Dict[str, Any]]] = []

    def emit(phase: str, data: Dict[str, Any]) -> None:
        events.append((phase, data))

    return events, emit


_FIXED_CODE = (
    "from contract import Strategy\n\n"
    "class S(Strategy):\n"
    "    def on_bar(self, ctx, bar):\n"
    "        ctx.submit_order(symbol='X', qty=1, side='LONG')\n"
    "        ctx.submit_order(symbol='X', qty=1, side='FLAT')\n"
)


def _proposed_fix(changes_made: str = "apply fix") -> TradeAlignmentReport:
    return TradeAlignmentReport(
        aligned=False,
        rationale="off-spec",
        issues=[AlignmentIssue(rule_type="entry_rules", description="x", severity="critical")],
        proposed_code=_FIXED_CODE,
        predicted_aligned_after_fix=True,
        changes_made=changes_made,
    )


# ---------------------------------------------------------------------------
# `_run_alignment_audit` tests — deterministic gate drives the verdict
# ---------------------------------------------------------------------------


def test_audit_returns_aligned_when_gate_passes() -> None:
    """Gate reports aligned → audit synthesises an aligned report and the
    LLM ``propose_code_fix`` is never invoked."""
    orch, align_stub, _checker_stub = _make_orchestrator(
        check_results=[_aligned_check_result()],
        propose_results=[],
    )

    report, gate_results = orch._run_alignment_audit(
        spec=_spec(),
        code="code-v0",
        trades=_trade_records(),
        metrics=_metrics(),
        prior_attempts=[],
        market_data=_market_data(),
        config=_config(),
    )

    assert report.aligned is True
    assert report.proposed_code is None
    # Per-rule findings preserved on the synthesised report.
    assert report.alignment_findings
    assert all(f.passed for f in report.alignment_findings)
    # No LLM call.
    assert align_stub.calls == []
    # Gate did not register any QualityGateResult rows in this stub.
    assert gate_results == []


def test_audit_misaligned_calls_propose_code_fix() -> None:
    """Gate reports misaligned → LLM ``propose_code_fix`` is invoked with
    the structured findings."""
    orch, align_stub, _checker_stub = _make_orchestrator(
        check_results=[_misaligned_check_result()],
        propose_results=[_proposed_fix("add RSI guard")],
    )

    report, _gates = orch._run_alignment_audit(
        spec=_spec(),
        code="code-v0",
        trades=_trade_records(),
        metrics=_metrics(),
        prior_attempts=[],
        market_data=_market_data(),
        config=_config(),
    )

    assert report.aligned is False
    assert report.proposed_code == _FIXED_CODE
    assert report.changes_made == "add RSI guard"
    # LLM was called with the gate's findings.
    assert len(align_stub.calls) == 1
    assert align_stub.calls[0]["n_findings"] == 1
    # Findings preserved on the returned report.
    assert len(report.alignment_findings) == 1
    assert report.alignment_findings[0].check_name == "entry_signal"


def test_audit_fails_closed_on_unexpected_agent_exception(monkeypatch) -> None:
    """An unexpected (non-``AlignmentAuditError``) exception inside
    ``propose_code_fix`` must NOT silently default to aligned. The audit
    fails closed (``aligned=False``, ``proposed_code=None``) so the
    orchestrator's ``no_proposed_fix`` exit fires."""
    monkeypatch.delenv("STRATEGY_LAB_ALIGNMENT_RETRIES", raising=False)

    class _BoomAgent:
        def __init__(self) -> None:
            self.calls = 0

        def propose_code_fix(self, **_kwargs: Any) -> TradeAlignmentReport:
            self.calls += 1
            raise RuntimeError("LLM transport blew up")

        def adjudicate_near_miss(self, **_kwargs: Any) -> NearMissVerdict:  # pragma: no cover
            return NearMissVerdict(legitimate=False)

    orch = StrategyLabOrchestrator()
    orch.deterministic_alignment_checker = _StubChecker(  # type: ignore[assignment]
        [_misaligned_check_result()]
    )
    agent = _BoomAgent()
    orch.alignment_agent = agent  # type: ignore[assignment]

    report, _gates = orch._run_alignment_audit(
        spec=_spec(),
        code="code",
        trades=_trade_records(),
        metrics=_metrics(),
        prior_attempts=[],
        market_data=_market_data(),
        config=_config(),
    )
    assert report.aligned is False
    assert report.proposed_code is None
    assert "fail-closed" in report.rationale.lower()
    assert "RuntimeError" in report.rationale
    # Non-AlignmentAuditError → surfaced immediately, no retries
    assert agent.calls == 1


def test_audit_retries_then_succeeds(monkeypatch) -> None:
    """A transient ``AlignmentAuditError`` is retried; a later attempt
    that returns a valid report is what the orchestrator sees."""
    monkeypatch.setenv("STRATEGY_LAB_ALIGNMENT_RETRIES", "2")

    success_report = _proposed_fix("recovered on retry")

    class _FlakyAgent:
        def __init__(self) -> None:
            self.calls = 0

        def propose_code_fix(self, **_kwargs: Any) -> TradeAlignmentReport:
            self.calls += 1
            if self.calls == 1:
                raise AlignmentAuditError("ValueError: malformed JSON")
            return success_report

        def adjudicate_near_miss(self, **_kwargs: Any) -> NearMissVerdict:  # pragma: no cover
            return NearMissVerdict(legitimate=False)

    orch = StrategyLabOrchestrator()
    orch.deterministic_alignment_checker = _StubChecker(  # type: ignore[assignment]
        [_misaligned_check_result()]
    )
    agent = _FlakyAgent()
    orch.alignment_agent = agent  # type: ignore[assignment]

    report, _gates = orch._run_alignment_audit(
        spec=_spec(),
        code="code",
        trades=_trade_records(),
        metrics=_metrics(),
        prior_attempts=[],
        market_data=_market_data(),
        config=_config(),
    )
    assert report.proposed_code == success_report.proposed_code
    assert agent.calls == 2


def test_audit_retries_exhausted_fails_closed(monkeypatch) -> None:
    """When every retry raises ``AlignmentAuditError``, the audit returns
    ``aligned=False`` with the underlying error recorded in the rationale."""
    monkeypatch.setenv("STRATEGY_LAB_ALIGNMENT_RETRIES", "2")

    class _AlwaysBoomAgent:
        def __init__(self) -> None:
            self.calls = 0

        def propose_code_fix(self, **_kwargs: Any) -> TradeAlignmentReport:
            self.calls += 1
            raise AlignmentAuditError("ValueError: malformed JSON")

        def adjudicate_near_miss(self, **_kwargs: Any) -> NearMissVerdict:  # pragma: no cover
            return NearMissVerdict(legitimate=False)

    orch = StrategyLabOrchestrator()
    orch.deterministic_alignment_checker = _StubChecker(  # type: ignore[assignment]
        [_misaligned_check_result()]
    )
    agent = _AlwaysBoomAgent()
    orch.alignment_agent = agent  # type: ignore[assignment]

    report, _gates = orch._run_alignment_audit(
        spec=_spec(),
        code="code",
        trades=_trade_records(),
        metrics=_metrics(),
        prior_attempts=[],
        market_data=_market_data(),
        config=_config(),
    )
    assert report.aligned is False
    assert report.proposed_code is None
    assert "fail-closed" in report.rationale.lower()
    assert "AlignmentAuditError" in report.rationale
    assert "malformed JSON" in report.rationale
    # default(2) retries → 3 total attempts
    assert agent.calls == 3
    # Findings still preserved on the fail-closed report so the
    # downstream record reflects what the gate detected.
    assert report.alignment_findings


def test_audit_respects_zero_retry_env(monkeypatch) -> None:
    """``STRATEGY_LAB_ALIGNMENT_RETRIES=0`` means one attempt total then
    immediate fail-closed."""
    monkeypatch.setenv("STRATEGY_LAB_ALIGNMENT_RETRIES", "0")

    class _AlwaysBoomAgent:
        def __init__(self) -> None:
            self.calls = 0

        def propose_code_fix(self, **_kwargs: Any) -> TradeAlignmentReport:
            self.calls += 1
            raise AlignmentAuditError("ValueError: malformed JSON")

        def adjudicate_near_miss(self, **_kwargs: Any) -> NearMissVerdict:  # pragma: no cover
            return NearMissVerdict(legitimate=False)

    orch = StrategyLabOrchestrator()
    orch.deterministic_alignment_checker = _StubChecker(  # type: ignore[assignment]
        [_misaligned_check_result()]
    )
    agent = _AlwaysBoomAgent()
    orch.alignment_agent = agent  # type: ignore[assignment]

    report, _gates = orch._run_alignment_audit(
        spec=_spec(),
        code="code",
        trades=_trade_records(),
        metrics=_metrics(),
        prior_attempts=[],
        market_data=_market_data(),
        config=_config(),
    )
    assert report.aligned is False
    assert agent.calls == 1


def test_propose_code_fix_raises_on_unparseable_response(monkeypatch) -> None:
    """``TradeAlignmentAgent.propose_code_fix`` raises
    ``AlignmentAuditError`` when the LLM response cannot be parsed —
    the orchestrator's retry wrapper translates that into a fail-closed
    report."""
    from investment_team.strategy_lab.agents import alignment as alignment_module

    class _StubStrandsAgent:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __call__(self, _prompt: str) -> str:
            return "not json at all"

    monkeypatch.setattr(alignment_module, "Agent", _StubStrandsAgent)
    monkeypatch.setattr(
        alignment_module,
        "get_strands_model",
        lambda _role: None,
    )

    agent = alignment_module.TradeAlignmentAgent()

    with pytest.raises(AlignmentAuditError) as exc_info:
        agent.propose_code_fix(
            spec=_spec(),
            code="code-v0",
            findings=[
                AlignmentFinding(
                    trade_num=1,
                    check_name="entry_signal",
                    passed=False,
                    severity="critical",
                )
            ],
            prior_attempts=None,
        )
    assert exc_info.value.__cause__ is not None


# ---------------------------------------------------------------------------
# `_coerce_report` standalone helper tests
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
        "predicted_aligned_after_fix": True,
        "changes_made": "",
    }
    report = _coerce_report(raw, fallback_code="orig")
    assert report.aligned is False
    assert report.proposed_code is None
    assert report.predicted_aligned_after_fix is False
    assert len(report.issues) == 1
    assert report.issues[0].severity == "critical"
    assert report.issues[0].affected_trades == [1, 2]


def test_coerce_report_keeps_well_formed_fix() -> None:
    raw = {
        "aligned": False,
        "rationale": "entries early",
        "issues": [],
        "proposed_code": (
            "from contract import Strategy\n\n"
            "class S(Strategy):\n"
            "    def on_bar(self, ctx, bar):\n"
            "        pass\n"
        ),
        "predicted_aligned_after_fix": True,
        "changes_made": "guard added",
    }
    report = _coerce_report(raw, fallback_code="orig")
    assert report.aligned is False
    assert report.proposed_code is not None
    assert "class S(Strategy)" in report.proposed_code
    assert report.predicted_aligned_after_fix is True
    assert report.changes_made == "guard added"


def test_coerce_report_tolerates_invalid_severity() -> None:
    raw = {
        "aligned": False,
        "rationale": "x",
        "issues": [{"rule_type": "exit_rules", "description": "d", "severity": "bogus"}],
        "proposed_code": (
            "from contract import Strategy\n\n"
            "class S(Strategy):\n"
            "    def on_bar(self, ctx, bar):\n"
            "        pass\n"
        ),
        "changes_made": "fix",
    }
    report = _coerce_report(raw, fallback_code="orig")
    assert len(report.issues) == 1
    assert report.issues[0].severity == "warning"


# ---------------------------------------------------------------------------
# `_run_alignment_round` direct tests
#
# Cover the fix-then-re-validate branches (refining_code → safety →
# re-execute → anomaly → commit) on the orchestrator's production
# helper.
# ---------------------------------------------------------------------------


def test_run_alignment_round_exits_immediately_when_gate_aligned() -> None:
    """Gate reports aligned → round terminates with no LLM call and no
    re-execution."""
    orch, align_stub, checker_stub = _make_orchestrator(
        check_results=[_aligned_check_result()],
    )

    events, emit = _collect_emit()
    outcome = orch._run_alignment_round(
        spec=_spec(),
        code="code-v0",
        trades=_trade_records(),
        metrics=_metrics(),
        market_data=_market_data(),
        config=_config(),
        align_round=0,
        all_gate_results=[],
        alignment_attempts=[],
        alignment_reports=[],
        emit=emit,
    )

    assert outcome.terminate is True
    assert outcome.code == "code-v0"
    assert len(checker_stub.calls) == 1
    assert align_stub.calls == []
    aligning_subs = [d["sub_phase"] for p, d in events if p == "aligning"]
    assert aligning_subs == ["evaluating", "aligned"]


def test_run_alignment_round_commits_proposal_on_clean_path(monkeypatch) -> None:
    """Misaligned gate + LLM fix + safe code + clean re-execution + benign
    metrics → ``terminate=False`` with the proposed code committed as the
    new known-good state."""
    from investment_team.strategy_lab import orchestrator as orchestrator_module

    orch, align_stub, _checker_stub = _make_orchestrator(
        check_results=[_misaligned_check_result()],
        propose_results=[_proposed_fix("add RSI guard")],
    )

    sandbox_calls: List[str] = []

    def _sandbox(code: str, _market_data, _config_arg, *, strategy=None):
        sandbox_calls.append(code)
        return _code_exec(success=True, raw_trades=_benign_sandbox_trades())

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _sandbox)

    all_gate_results: List[QualityGateResult] = []
    alignment_attempts: List[str] = []
    alignment_reports: List[TradeAlignmentReport] = []
    events, emit = _collect_emit()

    outcome = orch._run_alignment_round(
        spec=_spec(),
        code="code-v0",
        trades=_trade_records(),
        metrics=_metrics(),
        market_data=_market_data(),
        config=_config(),
        align_round=0,
        all_gate_results=all_gate_results,
        alignment_attempts=alignment_attempts,
        alignment_reports=alignment_reports,
        emit=emit,
    )

    assert len(align_stub.calls) == 1
    assert sandbox_calls == [_FIXED_CODE]
    assert outcome.terminate is False
    assert outcome.code == _FIXED_CODE
    assert outcome.spec.strategy_code == _FIXED_CODE
    assert len(outcome.trades) == len(_benign_sandbox_trades())
    assert alignment_attempts == ["add RSI guard"]
    assert len(alignment_reports) == 1
    assert alignment_reports[0].aligned is False
    aligning_subs = [d["sub_phase"] for p, d in events if p == "aligning"]
    assert "evaluating" in aligning_subs
    assert "not_aligned" in aligning_subs
    assert "refining_code" in aligning_subs
    assert "refined" in aligning_subs
    # Code-safety gates recorded with the alignment_ prefix.
    assert any(g.gate_name.startswith("alignment_") for g in all_gate_results)


def test_run_alignment_round_terminates_at_max_rounds(monkeypatch) -> None:
    """When ``align_round == MAX_ALIGNMENT_ROUNDS - 1`` and the gate finds
    misalignment, the round emits ``max_rounds_reached``, terminates, and
    leaves alignment_attempts untouched — no sandbox re-execution."""
    from investment_team.strategy_lab import orchestrator as orchestrator_module

    orch, _align_stub, _checker_stub = _make_orchestrator(
        check_results=[_misaligned_check_result()],
        propose_results=[_proposed_fix()],
    )

    def _must_not_run(*_a, **_kw):
        raise AssertionError("sandbox must not run at the max-rounds boundary")

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _must_not_run)

    alignment_attempts: List[str] = []
    alignment_reports: List[TradeAlignmentReport] = []
    events, emit = _collect_emit()

    outcome = orch._run_alignment_round(
        spec=_spec(),
        code="code-v0",
        trades=_trade_records(),
        metrics=_metrics(),
        market_data=_market_data(),
        config=_config(),
        align_round=MAX_ALIGNMENT_ROUNDS - 1,
        all_gate_results=[],
        alignment_attempts=alignment_attempts,
        alignment_reports=alignment_reports,
        emit=emit,
    )

    assert outcome.terminate is True
    assert outcome.code == "code-v0"
    assert alignment_attempts == []
    aligning_subs = [d["sub_phase"] for p, d in events if p == "aligning"]
    assert "max_rounds_reached" in aligning_subs


def test_run_alignment_round_rejects_unsafe_proposed_code(monkeypatch) -> None:
    """A proposal that fails the code-safety gate must terminate without
    re-execution and without overwriting the baseline state."""
    from investment_team.strategy_lab import orchestrator as orchestrator_module

    unsafe_report = TradeAlignmentReport(
        aligned=False,
        rationale="off-spec",
        issues=[AlignmentIssue(rule_type="entry_rules", description="x", severity="critical")],
        proposed_code=(
            "import os\n\n"
            "from contract import Strategy\n\n"
            "class S(Strategy):\n"
            "    def on_bar(self, ctx, bar):\n"
            "        os.system('rm -rf /')\n"
        ),
        predicted_aligned_after_fix=True,
        changes_made="unsafe rewrite",
    )
    orch, _align_stub, _checker_stub = _make_orchestrator(
        check_results=[_misaligned_check_result()],
        propose_results=[unsafe_report],
    )

    def _must_not_run(*_a, **_kw):
        raise AssertionError("sandbox must not re-execute unsafe code")

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _must_not_run)

    all_gate_results: List[QualityGateResult] = []
    alignment_attempts: List[str] = []
    events, emit = _collect_emit()

    outcome = orch._run_alignment_round(
        spec=_spec(),
        code="code-v0",
        trades=_trade_records(),
        metrics=_metrics(),
        market_data=_market_data(),
        config=_config(),
        align_round=0,
        all_gate_results=all_gate_results,
        alignment_attempts=alignment_attempts,
        alignment_reports=[],
        emit=emit,
    )

    assert outcome.terminate is True
    assert outcome.code == "code-v0"
    assert alignment_attempts == []
    aligning_subs = [d["sub_phase"] for p, d in events if p == "aligning"]
    assert aligning_subs[-1] == "rejected_unsafe_code"
    assert any(
        g.gate_name.startswith("alignment_") and not g.passed and g.severity == "critical"
        for g in all_gate_results
    )


def test_run_alignment_round_handles_re_execution_failure(monkeypatch) -> None:
    """Sandbox returns ``success=False`` on the post-fix re-execution →
    round emits ``re_execution_failed``, terminates, and appends a
    failed ``alignment_code_execution`` gate to the running results."""
    from investment_team.strategy_lab import orchestrator as orchestrator_module

    orch, _align_stub, _checker_stub = _make_orchestrator(
        check_results=[_misaligned_check_result()],
        propose_results=[_proposed_fix()],
    )

    def _fail(*_a, **_kw):
        return _code_exec(success=False, stderr="boom", error_type="runtime_error")

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _fail)

    all_gate_results: List[QualityGateResult] = []
    events, emit = _collect_emit()
    outcome = orch._run_alignment_round(
        spec=_spec(),
        code="code-v0",
        trades=_trade_records(),
        metrics=_metrics(),
        market_data=_market_data(),
        config=_config(),
        align_round=0,
        all_gate_results=all_gate_results,
        alignment_attempts=[],
        alignment_reports=[],
        emit=emit,
    )

    assert outcome.terminate is True
    assert outcome.code == "code-v0"
    aligning_subs = [d["sub_phase"] for p, d in events if p == "aligning"]
    assert aligning_subs[-1] == "re_execution_failed"
    assert any(g.gate_name == "alignment_code_execution" for g in all_gate_results)


@pytest.mark.parametrize(
    "with_diagnostics, expect_diag_in_emit",
    [
        (False, False),
        (True, True),
    ],
)
def test_run_alignment_round_rejects_anomalous_rerun(
    monkeypatch, with_diagnostics: bool, expect_diag_in_emit: bool
) -> None:
    """Sandbox succeeds but the re-executed code emits zero trades → the
    anomaly detector flags critical, the round terminates with
    ``anomaly_detected``, and the proposal is NOT committed."""
    from investment_team.models import BacktestExecutionDiagnostics
    from investment_team.strategy_lab import orchestrator as orchestrator_module

    orch, _align_stub, _checker_stub = _make_orchestrator(
        check_results=[_misaligned_check_result()],
        propose_results=[_proposed_fix()],
    )

    def _zero_trades(*_a, **_kw):
        result = _code_exec(success=True, raw_trades=[])
        if with_diagnostics:
            result.execution_diagnostics = BacktestExecutionDiagnostics(
                zero_trade_category="NO_ORDERS_EMITTED",
                summary="strategy emitted no orders",
                bars_processed=20,
            )
        return result

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _zero_trades)

    all_gate_results: List[QualityGateResult] = []
    alignment_attempts: List[str] = []
    events, emit = _collect_emit()
    original_spec = _spec()
    outcome = orch._run_alignment_round(
        spec=original_spec,
        code="code-v0",
        trades=_trade_records(),
        metrics=_metrics(),
        market_data=_market_data(),
        config=_config(),
        align_round=0,
        all_gate_results=all_gate_results,
        alignment_attempts=alignment_attempts,
        alignment_reports=[],
        emit=emit,
    )

    assert outcome.terminate is True
    assert outcome.code == "code-v0"
    assert outcome.spec is original_spec
    assert alignment_attempts == []
    aligning_subs = [d["sub_phase"] for p, d in events if p == "aligning"]
    assert aligning_subs[-1] == "anomaly_detected"
    assert any(
        g.gate_name.startswith("alignment_") and not g.passed and g.severity == "critical"
        for g in all_gate_results
    )
    last_anomaly_payload = next(
        d
        for p, d in reversed(events)
        if p == "aligning" and d.get("sub_phase") == "anomaly_detected"
    )
    if expect_diag_in_emit:
        assert "execution_diagnostics" in last_anomaly_payload
        assert "NO_ORDERS_EMITTED" in last_anomaly_payload["execution_diagnostics"]
    else:
        assert "execution_diagnostics" not in last_anomaly_payload


# ---------------------------------------------------------------------------
# Full-loop end-to-end test via the orchestrator's `_run_trade_alignment_loop`
# ---------------------------------------------------------------------------


def test_loop_recovers_after_one_fix_and_re_execution(monkeypatch) -> None:
    """Loop semantics: round 1 misaligned + proposed fix → re-execute →
    round 2 aligned → exit cleanly. Demonstrates the re-execution loop
    is preserved end-to-end."""
    from investment_team.strategy_lab import orchestrator as orchestrator_module

    orch, align_stub, checker_stub = _make_orchestrator(
        check_results=[
            _misaligned_check_result(),  # round 1: misaligned
            _aligned_check_result(),  # round 2 (after re-exec): aligned
        ],
        propose_results=[_proposed_fix("add RSI guard")],
    )

    sandbox_calls: List[str] = []

    def _sandbox(code: str, _market_data, _config_arg, *, strategy=None):
        sandbox_calls.append(code)
        return _code_exec(success=True, raw_trades=_benign_sandbox_trades())

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _sandbox)

    all_gate_results: List[QualityGateResult] = []
    events, emit = _collect_emit()
    outcome = orch._run_trade_alignment_loop(
        spec=_spec(),
        code="code-v0",
        trades=_trade_records(),
        metrics=_metrics(),
        market_data=_market_data(),
        config=_config(),
        execution_succeeded=True,
        all_gate_results=all_gate_results,
        emit=emit,
    )

    # Two gate runs (one per loop iteration) + one LLM fix proposal.
    assert len(checker_stub.calls) == 2
    assert len(align_stub.calls) == 1
    assert sandbox_calls == [_FIXED_CODE]
    assert outcome.trades_aligned is True
    assert outcome.code == _FIXED_CODE
    assert outcome.alignment_rounds == 1
    aligning_subs = [d["sub_phase"] for p, d in events if p == "aligning"]
    # Round 1: evaluating → not_aligned → refining_code → refined
    # Round 2: evaluating → aligned
    assert aligning_subs.count("evaluating") == 2
    assert "refined" in aligning_subs
    assert aligning_subs[-1] == "aligned"


def test_loop_caps_at_max_rounds(monkeypatch) -> None:
    """When the gate never returns aligned, the loop exits at
    ``MAX_ALIGNMENT_ROUNDS``. Demonstrates the configurable cap is
    honored — no runaway."""
    from investment_team.strategy_lab import orchestrator as orchestrator_module

    # Always misaligned; the loop must run exactly MAX_ALIGNMENT_ROUNDS.
    # ``propose_code_fix`` runs once per audit, including the final
    # round whose proposal is then short-circuited by the
    # max_rounds_reached check before sandbox re-execution.
    orch, align_stub, checker_stub = _make_orchestrator(
        check_results=[_misaligned_check_result() for _ in range(MAX_ALIGNMENT_ROUNDS)],
        propose_results=[_proposed_fix() for _ in range(MAX_ALIGNMENT_ROUNDS)],
    )

    def _sandbox(code: str, _md, _cfg, *, strategy=None):
        return _code_exec(success=True, raw_trades=_benign_sandbox_trades())

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _sandbox)

    outcome = orch._run_trade_alignment_loop(
        spec=_spec(),
        code="code-v0",
        trades=_trade_records(),
        metrics=_metrics(),
        market_data=_market_data(),
        config=_config(),
        execution_succeeded=True,
        all_gate_results=[],
        emit=lambda *_a, **_kw: None,
    )

    assert outcome.trades_aligned is False
    # One gate evaluation + one LLM fix proposal per round. The final
    # round's proposal is short-circuited by the max-rounds check before
    # re-execution, but the LLM call still ran inside the audit.
    assert len(checker_stub.calls) == MAX_ALIGNMENT_ROUNDS
    assert len(align_stub.calls) == MAX_ALIGNMENT_ROUNDS


def test_loop_persists_alignment_findings_for_record() -> None:
    """The final-iteration findings are surfaced on
    ``alignment_reports[-1].alignment_findings`` so the assembler can
    persist them onto ``BacktestRecord.alignment_findings``."""
    orch, _align_stub, _checker_stub = _make_orchestrator(
        check_results=[_aligned_check_result()],
    )

    outcome = orch._run_trade_alignment_loop(
        spec=_spec(),
        code="code-v0",
        trades=_trade_records(),
        metrics=_metrics(),
        market_data=_market_data(),
        config=_config(),
        execution_succeeded=True,
        all_gate_results=[],
        emit=lambda *_a, **_kw: None,
    )

    assert outcome.trades_aligned is True
    # The persisted ledger comes from the final report's alignment_findings.
    last_report = outcome.alignment_reports[-1]
    assert last_report.alignment_findings
    assert all(f.passed for f in last_report.alignment_findings)
