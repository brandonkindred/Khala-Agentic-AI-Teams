"""Concurrent near-miss adjudication equivalence.

Near-miss LLM adjudications are now collected during the trade loop and
dispatched through a bounded ``ThreadPoolExecutor`` instead of blocking the
loop one trade at a time. These tests assert the concurrent path is
observationally identical to the serial path: every candidate adjudicated
exactly once, verdicts mapped to the correct trade, and the findings list in
the same order regardless of completion timing or the configured concurrency.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List

import pytest

from investment_team.market_data_service import OHLCVBar
from investment_team.strategy_lab.alignment_findings import NearMissVerdict
from investment_team.strategy_lab.quality_gates.alignment_checks import (
    DeterministicAlignmentChecker,
    _bars_to_frame,
)
from investment_team.strategy_lab.spec_dsl import EntryRule, Predicate
from investment_team.tests.test_alignment_checks import _spec, _trade

# Entry rule: long when ``bar.close < 100``. The signal bar's close is 100.5
# (a 0.5% relative miss, inside the default 1% tolerance), so every trade
# routes to the adjudicator.
_RULE = EntryRule(side="long", when=Predicate(lhs="bar.close", op="<", rhs=100.0))


def _near_miss_bars() -> List[OHLCVBar]:
    return [
        OHLCVBar(date="2023-01-01", open=99.0, high=101.0, low=98.0, close=100.5, volume=1_000_000),
        OHLCVBar(
            date="2023-01-02", open=100.5, high=101.0, low=99.5, close=100.0, volume=1_000_000
        ),
    ]


def _scenario(symbols: List[str]):
    """Spec + trades + per-symbol market data, one near-miss trade per symbol."""
    spec = _spec(entry_rules=[_RULE])
    market_data = {s: _near_miss_bars() for s in symbols}
    trades = [
        _trade(trade_num=i + 1, symbol=s, entry_date="2023-01-02") for i, s in enumerate(symbols)
    ]
    return spec, trades, market_data


def _threadsafe_adjudicator(legitimate_for):
    """Records calls under a lock; verdict legitimacy keyed on symbol."""
    lock = threading.Lock()
    calls: List[Dict[str, Any]] = []

    def adjudicator(**kwargs) -> NearMissVerdict:
        with lock:
            calls.append(dict(kwargs))
        legit = legitimate_for(kwargs["symbol"])
        return NearMissVerdict(legitimate=legit, rationale=f"verdict::{kwargs['symbol']}")

    return calls, adjudicator


def _entry_findings(result):
    return [f for f in result.findings if f.check_name == "entry_signal"]


def test_each_near_miss_adjudicated_exactly_once(monkeypatch) -> None:
    monkeypatch.setenv("STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT", "0.01")
    monkeypatch.setenv("STRATEGY_LAB_ALIGNMENT_ADJUDICATION_CONCURRENCY", "8")
    symbols = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    spec, trades, md = _scenario(symbols)
    calls, adjudicator = _threadsafe_adjudicator(lambda s: s in {"AAA", "CCC", "EEE"})

    result = DeterministicAlignmentChecker().check(
        spec=spec,
        trades=trades,
        market_data=md,
        initial_capital=100_000.0,
        near_miss_adjudicator=adjudicator,
    )

    # One adjudication per trade, all symbols covered exactly once.
    assert len(calls) == len(symbols)
    assert sorted(c["symbol"] for c in calls) == sorted(symbols)

    entries = _entry_findings(result)
    # Findings stay in trade order despite concurrent completion.
    assert [f.trade_num for f in entries] == [t.trade_num for t in trades]
    # Verdict mapped to the right trade: legitimate -> info/passed.
    by_trade = {f.trade_num: f for f in entries}
    for i, s in enumerate(symbols, start=1):
        legit = s in {"AAA", "CCC", "EEE"}
        assert by_trade[i].passed is legit
        assert by_trade[i].severity == ("info" if legit else "critical")
        assert s in by_trade[i].details  # the symbol's verdict rationale is cited


def test_concurrent_matches_serial_output(monkeypatch) -> None:
    monkeypatch.setenv("STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT", "0.01")
    symbols = ["S1", "S2", "S3", "S4"]

    def run(concurrency: str):
        monkeypatch.setenv("STRATEGY_LAB_ALIGNMENT_ADJUDICATION_CONCURRENCY", concurrency)
        spec, trades, md = _scenario(symbols)
        _, adjudicator = _threadsafe_adjudicator(lambda s: s in {"S2", "S4"})
        result = DeterministicAlignmentChecker().check(
            spec=spec,
            trades=trades,
            market_data=md,
            initial_capital=100_000.0,
            near_miss_adjudicator=adjudicator,
        )
        return [
            (f.trade_num, f.check_name, f.passed, f.severity, f.details) for f in result.findings
        ]

    serial = run("1")
    parallel = run("8")
    assert parallel == serial


def test_no_near_miss_means_no_threadpool(monkeypatch) -> None:
    """When no trade produces a near-miss, the adjudicator is never called
    and the result is unaffected (the pending list stays empty)."""
    monkeypatch.setenv("STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT", "0.01")
    spec, trades, md = _scenario(["AAA"])
    # Override the trade's symbol bars so the predicate is satisfied (close
    # well below 100 on the signal bar) — no miss, no adjudication.
    md["AAA"] = [
        OHLCVBar(date="2023-01-01", open=50.0, high=51.0, low=49.0, close=50.0, volume=1_000),
        OHLCVBar(date="2023-01-02", open=50.0, high=51.0, low=49.0, close=50.0, volume=1_000),
    ]
    calls, adjudicator = _threadsafe_adjudicator(lambda s: True)
    result = DeterministicAlignmentChecker().check(
        spec=spec,
        trades=trades,
        market_data=md,
        initial_capital=100_000.0,
        near_miss_adjudicator=adjudicator,
    )
    assert calls == []
    entry = next(f for f in result.findings if f.check_name == "entry_signal")
    assert entry.passed is True


def test_serial_fallback_when_no_collector(monkeypatch) -> None:
    """``_check_entry_signal`` still adjudicates inline when called without a
    pending collector (the back-compatible serial path)."""
    monkeypatch.setenv("STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT", "0.01")
    spec, trades, md = _scenario(["AAA"])
    calls, adjudicator = _threadsafe_adjudicator(lambda s: True)
    gate = DeterministicAlignmentChecker()
    findings: List[Any] = []
    gate_results: List[Any] = []
    frames = {"AAA": _bars_to_frame(md["AAA"])}

    with gate._using_phase("verification"):
        gate._check_entry_signal(
            spec,
            trades[0],
            frames,
            {"AAA": {}},
            0.01,
            adjudicator,
            findings,
            gate_results,
            None,  # pending=None -> serial path
        )

    assert len(calls) == 1
    entry = next(f for f in findings if f.check_name == "entry_signal")
    assert entry.passed is True
    assert len(gate_results) == len(findings)


@pytest.mark.parametrize("raw, expected", [("0", 1), ("3", 3), ("garbage", 4), ("", 4)])
def test_adjudication_concurrency_env_parsing(monkeypatch, raw, expected) -> None:
    from investment_team.strategy_lab.quality_gates.alignment_checks import (
        _adjudication_concurrency,
    )

    if raw == "":
        monkeypatch.delenv("STRATEGY_LAB_ALIGNMENT_ADJUDICATION_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("STRATEGY_LAB_ALIGNMENT_ADJUDICATION_CONCURRENCY", raw)
    assert _adjudication_concurrency() == expected
