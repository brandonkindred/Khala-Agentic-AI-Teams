"""Shared fixtures + stub-double factory for the walk-forward/acceptance-gate
test suite (test_walk_forward_evaluation.py, test_acceptance_gate_integration.py,
test_run_cycle_caveat_resolution.py).

Not a test module — the leading underscore keeps pytest from collecting it.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from investment_team.market_data_service import OHLCVBar
from investment_team.models import BacktestConfig, BacktestResult, StrategySpec, TradeRecord
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    Predicate,
    SignalExitRule,
    StopLossRule,
)

from .conftest import stub_design_loop


def spec() -> StrategySpec:
    """Canned StrategySpec (single long-entry/short-exit rule) used across the suite."""
    return StrategySpec(
        strategy_id="strat-wf-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))],
        exit_rules=[SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=0))],
        risk_limits={},
        speculative=False,
        strategy_code=(
            "from contract import Strategy\n\nclass S(Strategy):\n"
            "    def on_bar(self, ctx, bar):\n"
            "        ctx.submit_order(symbol='X', qty=1, side='LONG')\n"
            "        ctx.submit_order(symbol='X', qty=1, side='FLAT')\n"
        ),
    )


def config(**overrides: Any) -> BacktestConfig:
    """Build a BacktestConfig with sensible test defaults; overrides are merged in."""
    base: Dict[str, Any] = dict(
        start_date="2022-01-03",
        end_date="2022-12-30",
        initial_capital=100_000.0,
    )
    base.update(overrides)
    return BacktestConfig(**base)


def mk_trade(
    *,
    entry: str,
    exit_: str,
    net: float,
    symbol: str = "AAPL",
    hold: int = 5,
) -> TradeRecord:
    """Build a single TradeRecord with a fixed price/shares baseline scaled by ``net`` P&L."""
    return TradeRecord(
        trade_num=1,
        entry_date=entry,
        exit_date=exit_,
        symbol=symbol,
        side="long" if net >= 0 else "short",
        entry_price=100.0,
        exit_price=100.0 + net / 10.0,
        shares=10.0,
        position_value=1000.0,
        gross_pnl=net,
        net_pnl=net,
        return_pct=net / 1000.0 * 100,
        hold_days=hold,
        outcome="win" if net > 0 else "loss",
        cumulative_pnl=net,
    )


def trades_across_year(n_per_month: int = 4, base_pnl: float = 50.0) -> List[TradeRecord]:
    """Spread roughly ``n_per_month`` winning trades across the calendar so that
    every walk-forward fold gets a handful of OOS observations."""
    out: List[TradeRecord] = []
    cum = 0.0
    for month in range(1, 13):
        for j in range(n_per_month):
            day = (j * 7) + 3  # 3, 10, 17, 24
            entry = date(2022, month, day)
            exit_ = entry + timedelta(days=5)
            net = base_pnl if (month + j) % 2 == 0 else -base_pnl * 0.6
            cum += net
            out.append(
                TradeRecord(
                    trade_num=len(out) + 1,
                    entry_date=entry.isoformat(),
                    exit_date=exit_.isoformat(),
                    symbol="AAPL",
                    side="long",
                    entry_price=100.0,
                    exit_price=100.0 + net / 10.0,
                    shares=10.0,
                    position_value=1000.0,
                    gross_pnl=net,
                    net_pnl=net,
                    return_pct=net / 1000.0 * 100,
                    hold_days=5,
                    outcome="win" if net > 0 else "loss",
                    cumulative_pnl=cum,
                )
            )
    return out


def stub_bars(symbol: str, *, drift: float = 0.0003, n: int = 250) -> List[OHLCVBar]:
    """Synthetic OHLCV bars starting 2022-01-03; price drifts at ``drift`` per day."""
    bars: List[OHLCVBar] = []
    d = date(2022, 1, 3)
    price = 100.0
    while len(bars) < n:
        if d.weekday() < 5:
            bars.append(
                OHLCVBar(
                    date=d.isoformat(),
                    open=price,
                    high=price * 1.005,
                    low=price * 0.995,
                    close=price,
                    volume=1_000_000,
                )
            )
            price *= 1.0 + drift
        d += timedelta(days=1)
    return bars


class StubMarketDataService:
    """Returns canned SPY/AGG bars for the 60/40 blend; no network access."""

    def __init__(self, *, has_spy: bool = True, has_agg: bool = True) -> None:
        self.has_spy = has_spy
        self.has_agg = has_agg
        self.calls: List[Dict[str, Any]] = []

    def fetch_multi_symbol_range(
        self,
        *,
        symbols: List[str],
        asset_class: str,
        start_date: str,
        end_date: str,
        as_of: Optional[str] = None,
        frequency: str = "1d",
    ) -> Dict[str, List[OHLCVBar]]:
        self.calls.append(
            {
                "symbols": list(symbols),
                "asset_class": asset_class,
                "start_date": start_date,
                "end_date": end_date,
                "as_of": as_of,
                "frequency": frequency,
            }
        )
        out: Dict[str, List[OHLCVBar]] = {}
        for s in symbols:
            if s == "SPY" and self.has_spy:
                out[s] = stub_bars("SPY", drift=0.0004)
            elif s == "AGG" and self.has_agg:
                out[s] = stub_bars("AGG", drift=0.0001)
            elif s not in {"SPY", "AGG"}:
                out[s] = stub_bars(s, drift=0.0002)
        return out


def orchestrator(
    market_data_service: Optional[StubMarketDataService] = None,
) -> StrategyLabOrchestrator:
    """Build a StrategyLabOrchestrator wired for the walk-forward test suite.

    Pre: ``market_data_service``, if given, implements ``fetch_multi_symbol_range``.
    Post: the returned orchestrator's readiness gate always sees a usable
    sample price, even when ``market_data_service`` doesn't implement
    ``fetch_ohlcv`` (the stub service here only implements
    ``fetch_multi_symbol_range``, so the readiness gate's own provider would
    otherwise fail closed to ``NaN`` and trip Rule 5 before the walk-forward
    path under test can run).
    """
    orch = StrategyLabOrchestrator()
    if market_data_service is not None:
        orch.market_data_service = market_data_service  # type: ignore[assignment]
    orch.spec_readiness_gate._market_sample_provider = lambda symbol, asset_class: 100.0
    return orch


def raise_walk_forward(*_args, **_kwargs):
    """Force the orchestrator's walk-forward fallback branch by raising."""
    raise RuntimeError("walk-forward fold construction failed (synthetic)")


def minimal_custom_spec_dict(**overrides: Any) -> Dict[str, Any]:
    """Build the minimal custom-code spec dict shared by the run_cycle stubs.

    Post: a dict with a single long entry rule / stop-loss exit rule,
    suitable for ``DesignAgent.run`` stubs; ``overrides`` are merged in
    (e.g. ``requires_custom_code=True``, which routes the stub trades
    through ``RuleFiringRateGate``'s custom-code path — see
    ``wire_run_cycle_stubs``'s ``deterministic_alignment_checker`` stub
    for how a stubbed ``entry[0]`` alignment finding keeps that path from
    reporting a spurious dead-rule critical on these fixtures).
    """
    base: Dict[str, Any] = {
        # No "asset_class": the design loop pins each attempt to one
        # randomly-drawn allowed category and an omitted class inherits that
        # pin, so this payload stays valid whichever category is drawn.
        "hypothesis": "h",
        "signal_definition": "s",
        "entry_rules": [
            EntryRule(
                side="long",
                when=Predicate(lhs="bar.close", op=">", rhs=0),
            ).model_dump()
        ],
        "exit_rules": [StopLossRule(pct=0.20).model_dump()],
        "risk_limits": {},
        "speculative": False,
    }
    base.update(overrides)
    return base


def minimal_strategy_code() -> str:
    """Return the minimal strategy source shared by the run_cycle stubs."""
    return (
        "from contract import Strategy\n\nclass S(Strategy):\n"
        "    def on_bar(self, ctx, bar):\n"
        "        ctx.submit_order(symbol='X', qty=1, side='LONG')\n"
        "        ctx.submit_order(symbol='X', qty=1, side='FLAT')\n"
    )


def recording_analysis_run() -> Tuple[Callable[..., str], Dict[str, Any]]:
    """Build an AnalysisAgent.run stub that records its call kwargs.

    Post: returns ``(stub, captured)``; ``captured`` is populated in place
    once the stub — set via ``monkeypatch.setattr(orch.analysis_agent,
    "run", stub)`` — has been invoked.
    """
    captured: Dict[str, Any] = {}

    def _run(*_args, **kwargs) -> str:
        captured.update(kwargs)
        return "narrative"

    return _run, captured


class StubExecResult:
    """Stand-in for the sandbox's execution result used by run_cycle stubs.

    Post: reports a successful execution carrying the given trades; the
    other fields mirror the subset of ``StrategyRunResult`` that
    ``run_cycle`` reads.
    """

    def __init__(self, trades: List[TradeRecord]) -> None:
        self.success = True
        self.trades = trades
        self.execution_time_seconds = 0.01
        self.error_type = None
        self.stderr = ""
        self.execution_diagnostics = None


def wire_run_cycle_stubs(
    orch: StrategyLabOrchestrator,
    monkeypatch: pytest.MonkeyPatch,
    *,
    alignment_aligned: bool,
    alignment_rationale: str = "ok",
    metrics: Optional[BacktestResult] = None,
    trades_override: Optional[List[TradeRecord]] = None,
) -> None:
    """Common stub wiring for end-to-end ``run_cycle`` tests.

    Pre: ``orch`` is a constructed ``StrategyLabOrchestrator``.
    Post: bypasses every LLM- or sandbox-touching call site so the
    orchestrator falls through to the ``is_winning`` resolution block with a
    deterministic set of inputs, and the deterministic alignment checker
    reports ``alignment_aligned``/``alignment_rationale`` as scripted.
    """
    from investment_team.strategy_lab.agents.alignment import TradeAlignmentReport
    from investment_team.strategy_lab.orchestrator import _MarketDataFetch

    # These stubs bypass the compiler and pin ``requires_custom_code=True`` on
    # purpose (see the spec_dict comment below), so the design pre-flight must not
    # demote the spec back to the compiled path — that would route
    # ``RuleFiringRateGate`` to its reason-string counting instead of the
    # custom-code / alignment-findings path these fixtures exercise. Keep
    # the intended custom path.
    monkeypatch.setenv("STRATEGY_LAB_DEMOTE_COMPILABLE_CUSTOM_CODE", "false")

    # ``_readiness_price_provider`` fails closed (NaN) when the live
    # ``MarketDataService.fetch_ohlcv`` returns no bars, so without a stub
    # SpecReadinessGate's Rule 5 critical short-circuits the design phase
    # before any test reaches the walk-forward path it exercises. Replace
    # the gate's bound provider with a fixed sentinel so readiness sees a
    # usable price and the test can drive the rest of the cycle. We poke
    # the gate's slot directly because the bound reference was captured
    # at ``__init__`` time — patching ``orch._readiness_price_provider``
    # alone does not redirect it.
    orch.spec_readiness_gate._market_sample_provider = lambda symbol, asset_class: 100.0

    # The stub bypasses the compiler entirely, so the compiled
    # ``reason="compiled_entry:entry[N]"`` annotation is absent from
    # trades. Mark the spec as custom-code so ``RuleFiringRateGate``
    # takes its alignment-findings path instead of reason-string
    # counting — the ``deterministic_alignment_checker`` stub below
    # supplies a passed ``entry[0]`` finding for the ``alignment_aligned``
    # case precisely so that path doesn't fire a spurious dead-rule
    # critical on these fixtures.
    spec_dict = minimal_custom_spec_dict(requires_custom_code=True)
    code = minimal_strategy_code()

    stub_design_loop(monkeypatch, orch, spec_dict, code, rationale="rationale")
    monkeypatch.setattr(orch.refinement_agent, "run", lambda **kw: ({"changes_made": "x"}, code))
    # The minimal stub strategy code used in these tests (qty=1, no entry
    # conditional) would otherwise be rejected by code/predicate conformance
    # checks. Neutralise them — the walk-forward path is the subject under
    # test, not synthesis-phase code conformance.
    monkeypatch.setattr(orch.code_conformance_gate, "check", lambda *a, **kw: [])
    monkeypatch.setattr(orch.predicate_conformance_gate, "check", lambda *a, **kw: [])
    # Drive the deterministic gate's verdict directly; the LLM
    # ``propose_code_fix`` is only consulted for the misaligned arm
    # and returns a report with no proposed_code so the loop's
    # ``no_proposed_fix`` exit fires cleanly.
    from investment_team.strategy_lab.alignment_findings import AlignmentFinding
    from investment_team.strategy_lab.quality_gates.alignment_checks import (
        AlignmentCheckResult,
    )

    monkeypatch.setattr(
        orch.deterministic_alignment_checker,
        "check",
        lambda **kw: AlignmentCheckResult(
            aligned=alignment_aligned,
            rationale=alignment_rationale,
            # ``aligned=True`` stands in for a real deterministic-gate run
            # where every trade's entry_signal check passed against
            # ``entry[0]`` — the stub spec's sole entry rule (see
            # ``minimal_custom_spec_dict``). Without this, the rule-firing
            # gate's alignment-findings-derived signal (fed by this same
            # report on the custom-code path — see rule_firing.py) would
            # read an "aligned" report as "entry[0] never fired" and emit a
            # spurious critical the real deterministic gate would never
            # produce for a genuinely aligned ledger.
            findings=[
                AlignmentFinding(
                    trade_num=1,
                    rule_id="entry[0]",
                    check_name="entry_signal",
                    passed=True,
                    severity="info",
                    details="stub: entry aligned",
                )
            ]
            if alignment_aligned
            else [
                AlignmentFinding(
                    trade_num=1,
                    check_name="entry_signal",
                    passed=False,
                    severity="critical",
                    details=alignment_rationale,
                )
            ],
        ),
    )
    monkeypatch.setattr(
        orch.alignment_agent,
        "propose_code_fix",
        lambda **kw: TradeAlignmentReport(
            aligned=False, rationale=alignment_rationale, proposed_code=None
        ),
    )
    monkeypatch.setattr(orch.analysis_agent, "run", lambda *a, **k: "narrative")
    monkeypatch.setattr(
        orch,
        "_fetch_market_data",
        lambda spec, config: _MarketDataFetch(
            data={"AAPL": stub_bars("AAPL")},
            requested_symbols=["AAPL"],
            fetched_symbols=["AAPL"],
        ),
    )

    sample_trades = (
        trades_override
        if trades_override is not None
        else trades_across_year(n_per_month=4, base_pnl=80.0)
    )
    sample_metrics = (
        metrics
        if metrics is not None
        else BacktestResult(
            total_return_pct=18.0,
            annualized_return_pct=15.0,
            volatility_pct=8.0,
            sharpe_ratio=1.4,
            max_drawdown_pct=4.0,
            win_rate_pct=60.0,
            profit_factor=2.0,
            calmar_ratio=0.0,
            deflated_sharpe=0.0,
            sortino_ratio=0.0,
        )
    )

    monkeypatch.setattr(
        "investment_team.strategy_lab.orchestrator.run_strategy_code",
        lambda *a, **k: StubExecResult(sample_trades),
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.orchestrator.compute_metrics",
        lambda *a, **k: sample_metrics,
    )
