"""Unit tests for ``ExitRuleConformanceGate`` (issue #527).

Drives the gate with hand-built ``TradeRecord`` / diagnostics fixtures so
each rule type's check is exercised in isolation, then a final happy-path
test runs the whole gate end-to-end against a multi-rule spec.
"""

from __future__ import annotations

import re
from typing import List

import pytest

from investment_team.market_data_service import OHLCVBar
from investment_team.models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    TradeRecord,
)
from investment_team.strategy_lab.quality_gates.exit_rule_conformance import (
    ExitRuleConformanceGate,
)
from investment_team.strategy_lab.spec_dsl import (
    IndicatorRef,
    Predicate,
    ScaledTakeProfitRule,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
)


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-12-31",
        slippage_bps=2.0,
    )


def _trade(
    *,
    trade_num: int = 1,
    hold_days: int = 5,
    return_pct: float = 1.0,
    side: str = "long",
    entry_price: float = 100.0,
    exit_price: float = 101.0,
    symbol: str = "AAA",
    exit_reason: str | None = "engine_exit:stop_loss",
) -> TradeRecord:
    """Build a TradeRecord. ``exit_reason`` defaults to
    ``"engine_exit:stop_loss"`` so legacy fixtures keep counting
    below-floor trades against the engine — tests that need the
    strategy-close exclusion pass ``exit_reason=None`` or a
    non-engine string explicitly.
    """
    return TradeRecord(
        trade_num=trade_num,
        entry_date="2024-01-01",
        exit_date="2024-01-06",
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        exit_price=exit_price,
        shares=100.0,
        position_value=entry_price * 100.0,
        gross_pnl=(exit_price - entry_price) * 100.0,
        net_pnl=(exit_price - entry_price) * 100.0,
        return_pct=return_pct,
        hold_days=hold_days,
        outcome="win" if return_pct > 0 else "loss",
        cumulative_pnl=(exit_price - entry_price) * 100.0,
        exit_reason=exit_reason,
    )


def _diagnostics(*, symbol: str = "AAA", **firings: int) -> BacktestExecutionDiagnostics:
    """Helper: build a diagnostics envelope where every firing is attributed
    to a single symbol. Tests that need cross-symbol scenarios construct
    ``BacktestExecutionDiagnostics`` directly.
    """
    return BacktestExecutionDiagnostics(
        exit_rule_firings=dict(firings),
        exit_rule_firings_by_symbol={symbol: dict(firings)} if firings else {},
    )


def _replay_config(enabled: bool = True) -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-12-31",
        exit_rule_trailing_replay_enabled=enabled,
    )


def _bar(date: str, *, high: float, low: float, open_: float | None = None) -> OHLCVBar:
    """Minimal OHLCV bar. Only ``high``/``low`` drive the trailing trigger; the
    open/close default to the high so the bar is self-consistent.
    """
    return OHLCVBar(
        date=date,
        open=open_ if open_ is not None else high,
        high=high,
        low=low,
        close=high,
        volume=1_000_000.0,
    )


def _trailing_trade(
    *,
    entry_date: str,
    exit_date: str,
    side: str = "long",
    entry_price: float = 100.0,
    symbol: str = "AAA",
    trade_num: int = 1,
    return_pct: float = -10.0,
    exit_reason: str | None = None,
    entry_order_type: str = "market",
) -> TradeRecord:
    """A TradeRecord with caller-controlled entry/exit dates, for replay tests."""
    return TradeRecord(
        trade_num=trade_num,
        entry_date=entry_date,
        exit_date=exit_date,
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        exit_price=entry_price * (1.0 + return_pct / 100.0),
        shares=100.0,
        position_value=entry_price * 100.0,
        gross_pnl=0.0,
        net_pnl=0.0,
        return_pct=return_pct,
        hold_days=3,
        outcome="loss" if return_pct < 0 else "win",
        cumulative_pnl=0.0,
        exit_reason=exit_reason,
        entry_order_type=entry_order_type,
    )


# ---------------------------------------------------------------------------
# Empty exit_rules
# ---------------------------------------------------------------------------


def test_no_exit_rules_skips_with_info() -> None:
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[],
        trades=[_trade()],
        diagnostics=_diagnostics(),
        config=_config(),
    )
    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].severity == "info"
    assert "empty" in results[0].details.lower()


# ---------------------------------------------------------------------------
# StopLossRule
# ---------------------------------------------------------------------------


def test_stop_loss_passes_when_engine_fired_even_on_gap_fill() -> None:
    """The engine detects the trigger on bar N's low but fills on bar N+1's
    open. Overnight gaps can push the realised return well past the raw
    floor without indicating an enforcement bug, so the gate must pass
    as long as the engine emitted at least one stop_loss close.
    """
    gate = ExitRuleConformanceGate()
    # pct=0.05 → raw floor=-5%. Trade closed at -20% (huge gap) — but the
    # engine recorded a stop_loss firing, so this is a real-world gap
    # fill, not a leak.
    trades = [_trade(return_pct=-20.0)]
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(stop_loss=1),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == [], [r.details for r in results]


def test_stop_loss_fails_when_below_floor_and_engine_never_fired() -> None:
    """Critical only when trades cleared the raw floor AND the engine
    recorded no stop_loss firings — i.e. the rule should have triggered
    but the enforcement path didn't run.
    """
    gate = ExitRuleConformanceGate()
    # pct=0.05 → raw floor=-5%. -10% trade with zero engine firings:
    # the engine missed the trigger, which is a real leak.
    trades = [_trade(return_pct=-10.0)]
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(),  # no firings
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "critical"
    assert "StopLossRule" in fails[0].details
    assert "leak" in fails[0].details.lower()


def test_stop_loss_passes_when_below_floor_but_engine_fired() -> None:
    """Same below-floor trade, but engine recorded a firing → gap fill,
    not a leak. Conformance should pass.
    """
    gate = ExitRuleConformanceGate()
    trades = [_trade(return_pct=-10.0)]
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(stop_loss=1),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []


def test_stop_loss_trailing_variant_skipped() -> None:
    gate = ExitRuleConformanceGate()
    # Trailing variants can't be checked from the ledger alone.
    trades = [_trade(return_pct=-50.0)]
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.03, basis="trailing_high")],
        trades=trades,
        diagnostics=_diagnostics(),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []


# ---------------------------------------------------------------------------
# StopLossRule — opt-in trailing bar-by-bar replay
# ---------------------------------------------------------------------------


def _trailing_high_bars() -> dict[str, list[OHLCVBar]]:
    """Long position, entry 100. The running high reaches 110 on d1, so the
    trailing floor (pct=0.05) ratchets to 110*0.95 = 104.5. d2's low of 104
    breaches it. The engine should emit on d2 and fill on d3.
    """
    return {
        "AAA": [
            _bar("2024-01-02", high=100.0, low=100.0),  # entry bar
            _bar("2024-01-03", high=110.0, low=108.0),  # high ratchets to 110
            _bar("2024-01-04", high=110.0, low=104.0),  # low 104 <= floor 104.5 -> trigger
            _bar("2024-01-05", high=106.0, low=103.0),  # fill bar
            _bar("2024-01-08", high=105.0, low=100.0),
            _bar("2024-01-09", high=101.0, low=95.0),
        ]
    }


def test_trailing_replay_disabled_by_default_even_with_market_data() -> None:
    """Flag off (default): the gate keeps the post-hoc info-skip line and never
    replays, even when bars are supplied.
    """
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
        trades=[_trailing_trade(entry_date="2024-01-02", exit_date="2024-01-09")],
        diagnostics=_diagnostics(),
        config=_config(),  # exit_rule_trailing_replay_enabled defaults False
        market_data=_trailing_high_bars(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []
    sl = [r for r in results if "StopLossRule(basis=" in r.details]
    assert sl and "check not run" in sl[0].details
    assert "trailing replay" not in sl[0].details


def test_trailing_replay_enabled_but_no_bars_skips() -> None:
    """Flag on but no market_data → still the post-hoc info-skip line."""
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
        trades=[_trailing_trade(entry_date="2024-01-02", exit_date="2024-01-09")],
        diagnostics=_diagnostics(),
        config=_replay_config(enabled=True),
        market_data=None,
    )
    sl = [r for r in results if "StopLossRule(basis=" in r.details]
    assert sl and "check not run" in sl[0].details


def test_trailing_replay_known_good_run_passes() -> None:
    """Engine fired on time: floor breached on d2, position closed on d3 (the
    next-bar fill). No earlier breach → info, no warning.
    """
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
        trades=[_trailing_trade(entry_date="2024-01-02", exit_date="2024-01-05")],
        diagnostics=_diagnostics(stop_loss=1),
        config=_replay_config(),
        market_data=_trailing_high_bars(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == [], [r.details for r in results]
    replay = [r for r in results if "trailing replay" in r.details]
    assert replay and replay[0].severity == "info"
    assert "no leaks" in replay[0].details


def test_trailing_replay_detects_injected_leak() -> None:
    """Floor breached on d2 but the position stayed open until d9 — the engine
    had a trigger bar AND a fill bar to spare. Warning (never critical).
    """
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
        trades=[_trailing_trade(entry_date="2024-01-02", exit_date="2024-01-09")],
        diagnostics=_diagnostics(),  # engine never fired
        config=_replay_config(),
        market_data=_trailing_high_bars(),
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "warning"
    assert "trailing replay" in fails[0].details
    # First breach is the d2 bar (2024-01-04).
    assert "2024-01-04" in fails[0].details
    # Never escalates to a critical veto.
    assert all(r.severity != "critical" for r in results)


def test_trailing_replay_gap_fill_is_not_a_false_positive() -> None:
    """A deep next-bar gap fill (trigger on d2, fill on d3 well below the floor)
    is legitimate: the trigger bar immediately precedes the fill bar, so there
    is no EARLIER breach to flag.
    """
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
        trades=[_trailing_trade(entry_date="2024-01-02", exit_date="2024-01-05", return_pct=-30.0)],
        diagnostics=_diagnostics(stop_loss=1),
        config=_replay_config(),
        market_data=_trailing_high_bars(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []


def test_trailing_replay_skips_trade_without_bar_window() -> None:
    """A trade whose symbol/dates are absent from the bar series is skipped and
    reported, not crashed.
    """
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
        trades=[_trailing_trade(entry_date="2024-01-02", exit_date="2024-01-05", symbol="ZZZ")],
        diagnostics=_diagnostics(),
        config=_replay_config(),
        market_data=_trailing_high_bars(),  # only has AAA
    )
    fails = [r for r in results if not r.passed]
    assert fails == []
    replay = [r for r in results if "trailing replay" in r.details]
    assert replay and "skipped 1 trade" in replay[0].details


def test_trailing_replay_skips_trade_with_date_outside_series() -> None:
    """Symbol is present but the trade's exit_date is not in the bar series →
    skipped via the date-index lookup, reported, not crashed.
    """
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
        trades=[_trailing_trade(entry_date="2024-01-02", exit_date="2024-12-31")],
        diagnostics=_diagnostics(),
        config=_replay_config(),
        market_data=_trailing_high_bars(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []
    replay = [r for r in results if "trailing replay" in r.details]
    assert replay and "skipped 1 trade" in replay[0].details


def test_trailing_replay_detects_short_leak_via_trailing_low() -> None:
    """Short position, entry 100. Running low reaches 90 on d1, so the trailing
    cap (pct=0.05) ratchets to 90*1.05 = 94.5. d2's high of 95 breaches it but
    the position stayed open until d5 → warning.
    """
    bars = {
        "AAA": [
            _bar("2024-01-02", high=100.0, low=100.0),  # entry
            _bar("2024-01-03", high=92.0, low=90.0),  # low ratchets to 90
            _bar("2024-01-04", high=95.0, low=93.0),  # high 95 >= cap 94.5 -> trigger
            _bar("2024-01-05", high=96.0, low=94.0),
            _bar("2024-01-08", high=98.0, low=95.0),
        ]
    }
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_low")],
        trades=[
            _trailing_trade(
                entry_date="2024-01-02",
                exit_date="2024-01-08",
                side="short",
                return_pct=2.0,
            )
        ],
        diagnostics=_diagnostics(),
        config=_replay_config(),
        market_data=bars,
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "warning"
    assert "2024-01-04" in fails[0].details


def _pre_entry_spike_bars() -> dict[str, list[OHLCVBar]]:
    """Long entry at 100 whose ENTRY bar prints a pre-entry high of 120. Folding
    that spike into the watermark ratchets the trailing floor (pct=0.05) to 114,
    which the next bar's low of 99 breaches — a leak that only exists if the
    entry bar is (wrongly) consumed. After the entry bar, price is flat near 100.
    """
    return {
        "AAA": [
            _bar("2024-01-02", high=120.0, low=100.0),  # entry bar w/ pre-entry spike
            _bar("2024-01-03", high=101.0, low=99.0),
            _bar("2024-01-04", high=101.0, low=99.0),
            _bar("2024-01-05", high=100.0, low=98.0),  # fill bar
        ]
    }


def test_trailing_replay_skips_non_market_entry_bar() -> None:
    """A non-market (limit/stop) entry shares its fill bar with pre-entry price
    action, so — like the engine's ``just_opened`` guard — the replay must NOT
    evaluate or fold in the entry bar. Without the guard the d0 high of 120 would
    ratchet the floor and report a false leak; with it, no warning.
    """
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
        trades=[
            _trailing_trade(
                entry_date="2024-01-02",
                exit_date="2024-01-05",
                entry_order_type="limit",
            )
        ],
        diagnostics=_diagnostics(),
        config=_replay_config(),
        market_data=_pre_entry_spike_bars(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == [], [r.details for r in results]
    replay = [r for r in results if "trailing replay" in r.details]
    assert replay and replay[0].severity == "info"


def test_trailing_replay_market_entry_consumes_entry_bar() -> None:
    """Contrast to the non-market case: a MARKET entry IS evaluated/extended from
    the entry bar (matching the engine), so the d0 high of 120 ratchets the floor
    and d1's low of 99 is a genuine breach. The position closing on d5 instead of
    d4 is a real leak → warning at the d1 bar.
    """
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
        trades=[
            _trailing_trade(
                entry_date="2024-01-02",
                exit_date="2024-01-05",
                entry_order_type="market",
            )
        ],
        diagnostics=_diagnostics(),
        config=_replay_config(),
        market_data=_pre_entry_spike_bars(),
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "warning"
    assert "2024-01-03" in fails[0].details


def test_trailing_replay_none_entry_order_type_defaults_to_market() -> None:
    """A missing ``entry_order_type`` (``None`` on a legacy/partial record) is
    treated as a market entry — the leak-detection-safe default — so the entry
    bar is evaluated and a real leak is still surfaced rather than masked.
    """
    trade = _trailing_trade(entry_date="2024-01-02", exit_date="2024-01-05")
    # Force the field absent (pydantic v2 models are mutable by default).
    trade.entry_order_type = None  # type: ignore[assignment]
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
        trades=[trade],
        diagnostics=_diagnostics(),
        config=_replay_config(),
        market_data=_pre_entry_spike_bars(),
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "warning"
    assert "2024-01-03" in fails[0].details


def test_trailing_replay_rejects_non_trailing_basis() -> None:
    """The replay's precondition is enforced with an explicit raise (not a bare
    assert that ``python -O`` would strip): a non-trailing basis is a caller bug
    and must fail loudly rather than silently mapping to a side.
    """
    gate = ExitRuleConformanceGate()
    with pytest.raises(ValueError, match="non-trailing basis"):
        gate._check_stop_loss_trailing_replay(
            StopLossRule(pct=0.05),  # default basis="entry_price"
            [],
            {},
        )


# ---------------------------------------------------------------------------
# TakeProfitRule
# ---------------------------------------------------------------------------


def test_take_profit_alone_fires_when_threshold_reached() -> None:
    gate = ExitRuleConformanceGate()
    trades = [_trade(return_pct=6.0)]  # >= 5% target
    results = gate.check(
        exit_rules=[TakeProfitRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(take_profit=1),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []


def test_take_profit_alone_warns_when_never_reached() -> None:
    gate = ExitRuleConformanceGate()
    # take_profit is the only rule, no trade reached 5% — warning.
    trades = [_trade(return_pct=1.0), _trade(trade_num=2, return_pct=2.0)]
    results = gate.check(
        exit_rules=[TakeProfitRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "warning"


def test_take_profit_with_other_rules_is_informational() -> None:
    # With another rule present, never reaching the take-profit threshold
    # is acceptable (the other rule may close trades first).
    gate = ExitRuleConformanceGate()
    trades = [_trade(return_pct=1.0)]
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05), TakeProfitRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(stop_loss=1),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []


# ---------------------------------------------------------------------------
# ScaledTakeProfitRule
# ---------------------------------------------------------------------------


def _ladder() -> ScaledTakeProfitRule:
    """A two-rung ladder: 50% at +5%, 30% at +10%."""
    return ScaledTakeProfitRule(
        levels=[{"pct": 0.05, "qty_fraction": 0.5}, {"pct": 0.10, "qty_fraction": 0.3}]
    )


def test_scaled_take_profit_alone_is_informational_with_firings() -> None:
    """Rungs fired → informational per-rung telemetry naming each fired rung key,
    never a failure."""
    gate = ExitRuleConformanceGate()
    diag = BacktestExecutionDiagnostics(
        exit_rule_firings={"scaled_take_profit": 2},
        exit_rule_firings_by_symbol={"AAA": {"scaled_take_profit": 2}},
        scaled_take_profit_level_firings={"0:0": 1, "0:1": 1},
    )
    results = gate.check(
        exit_rules=[_ladder()], trades=[_trade()], diagnostics=diag, config=_config()
    )
    assert [r for r in results if not r.passed] == []
    # The per-rung telemetry names BOTH rungs with their level params and fired
    # counts. Assert the meaningful DATA (rung index → pct → qty_fraction → fired
    # count, in that order) rather than the exact punctuation/spacing of the
    # human-readable line — the message is presentation, not a machine-parsed
    # contract, so a future format tweak (e.g. "L0: @0.05 / 0.5 = 1") should not
    # break this. ``\D*`` spans whatever separators the format uses between fields.
    info = next(
        r for r in results if "ScaledTakeProfitRule" in r.details and "per-rung" in r.details
    )
    assert re.search(r"L0\D*0\.05\D*0\.5\D*1", info.details)  # rung 0: +5%, 50%, fired once
    assert re.search(r"L1\D*0\.1\D*0\.3\D*1", info.details)  # rung 1: +10%, 30%, fired once


def test_scaled_take_profit_alone_warns_when_no_rung_fired() -> None:
    """The ladder is the only exit and trades exist, yet no rung ever fired → warning
    (the rung targets may be unreachable on the strategy's universe)."""
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[_ladder()],
        trades=[_trade(return_pct=1.0)],
        diagnostics=_diagnostics(),  # no firings at all
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "warning"


def test_scaled_take_profit_no_warn_when_a_sibling_ladder_fired() -> None:
    """Multiple ladders with NO other exit kind: as long as SOME rung fired
    strategy-wide, a sibling ladder whose own rungs never fired does NOT warn. The
    warning keys off whole-strategy rung firings, not this ladder's count — with
    several ladders one can carry the exits while another's higher rungs legitimately
    never reach their target, so a per-ladder zero is a false alarm."""
    gate = ExitRuleConformanceGate()
    # Two ladders (rule_index 0 and 1); only rule 0's first rung fired, rule 1 none.
    diag = BacktestExecutionDiagnostics(
        exit_rule_firings={"scaled_take_profit": 1},
        exit_rule_firings_by_symbol={"AAA": {"scaled_take_profit": 1}},
        scaled_take_profit_level_firings={"0:0": 1},
    )
    results = gate.check(
        exit_rules=[_ladder(), _ladder()],
        trades=[_trade(return_pct=1.0)],
        diagnostics=diag,
        config=_config(),
    )
    # No warning: a rung fired strategy-wide, so neither ladder is flagged.
    assert [r for r in results if not r.passed] == []
    # Both ladders still report their per-rung telemetry informationally.
    assert any("ScaledTakeProfitRule[0]" in r.details for r in results)
    assert any("ScaledTakeProfitRule[1]" in r.details for r in results)


def test_scaled_take_profit_warns_when_no_ladder_rung_fires_strategy_wide() -> None:
    """Multiple ladders with NO other exit kind and NOT A SINGLE rung firing across
    the whole strategy: every ladder warns — the position relies entirely on rungs
    reaching their targets and none did, so the targets may be unreachable."""
    gate = ExitRuleConformanceGate()
    # Two ladders, zero rung firings anywhere.
    diag = BacktestExecutionDiagnostics(scaled_take_profit_level_firings={})
    results = gate.check(
        exit_rules=[_ladder(), _ladder()],
        trades=[_trade(return_pct=1.0)],
        diagnostics=diag,
        config=_config(),
    )
    warnings = [r for r in results if not r.passed and r.severity == "warning"]
    # Both ladders warn — the whole-strategy signal that no rung target was reached.
    assert len(warnings) == 2
    assert {"ScaledTakeProfitRule[0]", "ScaledTakeProfitRule[1]"} == {
        w.details.split(" ")[0] for w in warnings
    }


def test_scaled_take_profit_with_other_rules_is_informational() -> None:
    """A co-existing stop can close trades before any rung reaches its target, so
    zero rung firings alongside another (non-ladder) exit is acceptable
    (informational only, flagged as co-existing)."""
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05), _ladder()],
        trades=[_trade(return_pct=-1.0)],
        diagnostics=_diagnostics(stop_loss=1),
        config=_config(),
    )
    assert [r for r in results if not r.passed] == []
    assert any("co-exists with other exit rules" in r.details for r in results)


# ---------------------------------------------------------------------------
# SignalExitRule
# ---------------------------------------------------------------------------


def test_signal_exit_rule_is_informational_only() -> None:
    gate = ExitRuleConformanceGate()
    rule = SignalExitRule(
        when=Predicate(
            lhs=IndicatorRef(name="rsi", params={"period": 14}),
            op=">",
            rhs=70.0,
        )
    )
    results = gate.check(
        exit_rules=[rule],
        trades=[_trade()],
        diagnostics=_diagnostics(),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []
    # The note appears in one of the info rows.
    assert any("SignalExitRule" in r.details for r in results)


# ---------------------------------------------------------------------------
# Diagnostics summary row
# ---------------------------------------------------------------------------


def test_diagnostics_summary_includes_firings_breakdown() -> None:
    gate = ExitRuleConformanceGate()
    diag = _diagnostics(stop_loss=3, take_profit=1)
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=[_trade(return_pct=-1.0)],
        diagnostics=diag,
        config=_config(),
    )
    summary = [r for r in results if "engine_exits" in r.details]
    assert summary, [r.details for r in results]
    assert "stop_loss=3" in summary[0].details
    assert "take_profit=1" in summary[0].details


def test_diagnostics_summary_handles_no_firings() -> None:
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=[_trade(return_pct=1.0)],
        diagnostics=_diagnostics(),
        config=_config(),
    )
    summary = [r for r in results if "engine_exits" in r.details]
    assert summary
    assert "none" in summary[0].details


# ---------------------------------------------------------------------------
# Multi-rule integration
# ---------------------------------------------------------------------------


def test_multi_rule_all_pass_returns_no_failures() -> None:
    gate = ExitRuleConformanceGate()
    trades: List[TradeRecord] = [
        _trade(trade_num=1, return_pct=-2.0),
        _trade(trade_num=2, return_pct=1.0),
        _trade(trade_num=3, return_pct=6.0),
    ]
    results = gate.check(
        exit_rules=[
            StopLossRule(pct=0.05),
            TakeProfitRule(pct=0.05),
        ],
        trades=trades,
        diagnostics=_diagnostics(stop_loss=1, take_profit=1),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == [], [r.details for r in fails]


def test_stop_loss_fails_when_below_floor_trades_exceed_firings() -> None:
    """Per-trade leak counting: one correctly stopped trade must NOT mask
    a separate trade that closed below the floor without an engine firing.
    Regression for the P2 review comment on issue #527 PR: the previous
    "any firings > 0 → pass" check was too loose.
    """
    gate = ExitRuleConformanceGate()
    # Two below-floor trades, only one engine firing → 1 unaccounted leak.
    trades = [
        _trade(trade_num=1, return_pct=-7.0),
        _trade(trade_num=2, return_pct=-8.0),
    ]
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(stop_loss=1),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "critical"
    assert "leak" in fails[0].details.lower()
    assert "1 unaccounted" in fails[0].details


def test_stop_loss_passes_when_firings_cover_below_floor_count() -> None:
    """Exactly as many firings as below-floor trades → no leak. Each
    below-floor trade is plausibly accounted for by a matching engine
    emission; gap-fill semantics absorb the rest.
    """
    gate = ExitRuleConformanceGate()
    trades = [
        _trade(trade_num=1, return_pct=-7.0),
        _trade(trade_num=2, return_pct=-8.0),
    ]
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(stop_loss=2),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []


def test_take_profit_alone_passes_on_firings_even_with_short_realized_return() -> None:
    """Realized return below the rule's raw target is expected when the
    engine fires take-profit on bar N's high but the synthetic close
    fills on bar N+1's open (gap / slippage / costs eat into the
    realised return). The gate must use the engine firings diagnostic,
    not the trade ledger's ``return_pct``, to verify enforcement.
    Regression for the P3 review comment on issue #527 PR.
    """
    gate = ExitRuleConformanceGate()
    # take_profit target = 5%; trade realised 3% after gap/costs.
    trades = [_trade(return_pct=3.0)]
    results = gate.check(
        exit_rules=[TakeProfitRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(take_profit=1),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == [], [r.details for r in results]


def test_take_profit_alone_warns_when_engine_never_fired() -> None:
    """Take-profit is the only rule, trades exist, but engine recorded
    zero firings — the threshold may be unreachable on the strategy's
    universe. Surface as a warning so the operator can revisit the
    spec.
    """
    gate = ExitRuleConformanceGate()
    trades = [_trade(return_pct=1.0), _trade(trade_num=2, return_pct=2.0)]
    results = gate.check(
        exit_rules=[TakeProfitRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "warning"
    assert "zero take_profit firings" in fails[0].details


def test_stop_loss_cross_symbol_firings_do_not_mask_leak() -> None:
    """Per-symbol attribution: an engine stop_loss firing on symbol AAA
    must NOT cover a missed firing on symbol BBB. A global rule-kind
    count would pass this scenario (2 below-floor trades, 2 firings)
    even though BBB never had its rule enforced. Regression for the P2
    review comment on issue #527 PR.
    """
    gate = ExitRuleConformanceGate()
    # AAA: one below-floor trade, one firing (protected).
    # BBB: one below-floor trade, ZERO firings (leak).
    trades = [
        TradeRecord(
            trade_num=1,
            entry_date="2024-01-01",
            exit_date="2024-01-02",
            symbol="AAA",
            side="long",
            entry_price=100.0,
            exit_price=92.0,
            shares=100,
            position_value=10_000.0,
            gross_pnl=-800.0,
            net_pnl=-800.0,
            return_pct=-8.0,
            hold_days=1,
            outcome="loss",
            cumulative_pnl=-800.0,
            exit_reason="engine_exit:stop_loss",
        ),
        TradeRecord(
            trade_num=2,
            entry_date="2024-01-03",
            exit_date="2024-01-04",
            symbol="BBB",
            side="long",
            entry_price=200.0,
            exit_price=180.0,
            shares=50,
            position_value=10_000.0,
            gross_pnl=-1_000.0,
            net_pnl=-1_000.0,
            return_pct=-10.0,
            hold_days=1,
            outcome="loss",
            cumulative_pnl=-1_800.0,
            exit_reason="engine_exit:stop_loss",
        ),
    ]
    # Aggregate firings would mask the leak (2 trades, 2 firings); per
    # symbol, BBB has 0 firings.
    diag = BacktestExecutionDiagnostics(
        exit_rule_firings={"stop_loss": 2},
        exit_rule_firings_by_symbol={
            "AAA": {"stop_loss": 1},
            "CCC": {"stop_loss": 1},  # unrelated firing on another symbol
        },
    )
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=trades,
        diagnostics=diag,
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "critical"
    assert "BBB" in fails[0].details


def test_stop_loss_excludes_strategy_closed_below_floor_trades() -> None:
    """A strategy-emitted market exit that fills on a next-bar gap-down
    open beneath the structured stop-loss floor must NOT count as an
    engine leak: the engine never had a chance to fire on that bar
    (position closed before rule eval). Regression for the P2 review
    comment on PR #581 (issue #527).

    Setup: two below-floor trades on AAA, zero engine firings. Trade
    1 closed by strategy (``exit_reason="strategy_market_exit"``);
    trade 2 closed by engine (``exit_reason=None`` would be ambiguous,
    so explicit ``engine_exit:stop_loss``). Old behaviour: both count,
    "2 unaccounted" critical. New behaviour: only trade 2 counts,
    still flags 1 unaccounted critical (the real engine leak).
    """
    gate = ExitRuleConformanceGate()
    trades = [
        _trade(
            trade_num=1,
            symbol="AAA",
            entry_price=100.0,
            exit_price=92.0,
            return_pct=-8.0,
            exit_reason="strategy_market_exit",  # strategy closed
        ),
        _trade(
            trade_num=2,
            symbol="AAA",
            entry_price=100.0,
            exit_price=93.0,
            return_pct=-7.0,
            exit_reason="engine_exit:stop_loss",  # engine claim
        ),
    ]
    # Zero engine firings — the engine claim on trade 2 is the leak
    # (engine attribution but no recorded firing).
    diag = BacktestExecutionDiagnostics(
        exit_rule_firings={},
        exit_rule_firings_by_symbol={"AAA": {}},
    )
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=trades,
        diagnostics=diag,
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "critical"
    # Trade 2 (the engine-attributed one) is flagged; trade 1 is
    # explicitly excluded with a count in the details.
    assert "trade_num" in fails[0].details
    assert "1 strategy-closed below-floor trade(s)" in fails[0].details
    assert "excluded" in fails[0].details


def test_stop_loss_passes_when_all_below_floor_are_strategy_closed() -> None:
    """Counterpart: if every below-floor trade was strategy-closed
    (None exit_reason for a vanilla strategy market exit, or an
    explicit strategy string), the gate must pass — the engine had
    nothing to do here.
    """
    gate = ExitRuleConformanceGate()
    trades = [
        _trade(
            trade_num=1,
            symbol="AAA",
            entry_price=100.0,
            exit_price=92.0,
            return_pct=-8.0,
            exit_reason=None,  # vanilla strategy market exit
        ),
        _trade(
            trade_num=2,
            symbol="AAA",
            entry_price=100.0,
            exit_price=93.0,
            return_pct=-7.0,
            exit_reason="strategy_close",  # explicit strategy reason
        ),
    ]
    diag = BacktestExecutionDiagnostics(
        exit_rule_firings={},
        exit_rule_firings_by_symbol={"AAA": {}},
    )
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=trades,
        diagnostics=diag,
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []
    info = next(r for r in results if r.gate_name == "exit_rule_conformance" and r.passed)
    assert "2 strategy-closed below-floor trade(s)" in info.details


# ---------------------------------------------------------------------------
# StopLossRule(style="limit") — fill-based reconciliation
# ---------------------------------------------------------------------------


def _limit_rule() -> StopLossRule:
    return StopLossRule(pct=0.05, style="limit", limit_offset_pct=0.01)


def test_limit_stop_tolerates_fired_but_not_filled() -> None:
    """A limit-style stop can fire (emit) on several bars yet gap through its
    limit unfilled — those firings produce no closed trade. Firing-based
    reconciliation tolerates this: the extra (non-filling) firings only inflate
    the denominator, so one below-floor fill against three firings is no leak.
    The exit_rule_fills telemetry surfaces the fire-vs-fill divergence.
    """
    gate = ExitRuleConformanceGate()
    # One below-floor engine-stopped trade (it filled); three firings total
    # (two gapped through unfilled, leaving the position open / no trade).
    trades = [_trade(return_pct=-6.0)]
    diag = BacktestExecutionDiagnostics(
        exit_rule_firings={"stop_loss": 3},
        exit_rule_firings_by_symbol={"AAA": {"stop_loss": 3}},
        exit_rule_fills={"stop_loss": 1},
        exit_rule_fills_by_symbol={"AAA": {"stop_loss": 1}},
    )
    results = gate.check(
        exit_rules=[_limit_rule()],
        trades=trades,
        diagnostics=diag,
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []
    info = next(
        r for r in results if "StopLossRule" in r.details and "per-symbol firings" in r.details
    )
    assert "fired-but-unfilled gap-throughs tolerated" in info.details
    # The fill counter is surfaced as telemetry (not used for the leak check).
    assert any("engine_exit_fills" in r.details for r in results)


def test_limit_stop_leak_caught_when_firings_insufficient() -> None:
    """A genuine leak is still caught for a limit-style stop: two engine-attributed
    below-floor trades against only one firing means the dispatcher's emission
    path and the close ledger disagree — flagged via the independent firing count.
    """
    gate = ExitRuleConformanceGate()
    trades = [
        _trade(trade_num=1, return_pct=-6.0),
        _trade(trade_num=2, return_pct=-7.0),
    ]
    diag = BacktestExecutionDiagnostics(
        exit_rule_firings={"stop_loss": 1},
        exit_rule_firings_by_symbol={"AAA": {"stop_loss": 1}},
    )
    results = gate.check(
        exit_rules=[_limit_rule()],
        trades=trades,
        diagnostics=diag,
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "critical"
    assert "1 unaccounted" in fails[0].details
    assert "per-symbol firings" in fails[0].details


def test_limit_stop_zero_firings_with_engine_trade_is_a_leak() -> None:
    """A below-floor trade attributed to engine_exit:stop_loss with zero recorded
    firings is a real leak — the engine claimed the close but never recorded
    emitting it. Caught identically for limit-style and market-style.
    """
    gate = ExitRuleConformanceGate()
    trades = [_trade(return_pct=-6.0)]
    diag = BacktestExecutionDiagnostics(
        exit_rule_firings={},
        exit_rule_firings_by_symbol={"AAA": {}},
    )
    results = gate.check(
        exit_rules=[_limit_rule()],
        trades=trades,
        diagnostics=diag,
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "critical"


def test_market_stop_ignores_fill_counter() -> None:
    """A market-style stop reconciles against firings (emission == guaranteed
    fill), so an empty fill counter must NOT turn a correctly-fired market stop
    into a false leak.
    """
    gate = ExitRuleConformanceGate()
    trades = [_trade(return_pct=-6.0)]
    diag = BacktestExecutionDiagnostics(
        exit_rule_firings={"stop_loss": 1},
        exit_rule_firings_by_symbol={"AAA": {"stop_loss": 1}},
        # No fill counter populated — the market path must not consult it.
    )
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=trades,
        diagnostics=diag,
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []
    info = next(
        r for r in results if "StopLossRule" in r.details and "per-symbol firings" in r.details
    )
    assert "fired-but-unfilled" not in info.details


def test_stop_loss_excludes_engine_take_profit_below_floor() -> None:
    """An engine take_profit firing can fill below the stop-loss floor
    on a gap (engine fired TP on bar N's high, fills on bar N+1's
    open which gapped down past the SL floor). That's NOT a stop_loss
    leak — the engine made a deliberate TP choice and the fill
    happened where it happened.
    """
    gate = ExitRuleConformanceGate()
    trades = [
        _trade(
            trade_num=1,
            symbol="AAA",
            entry_price=100.0,
            exit_price=93.0,
            return_pct=-7.0,
            exit_reason="engine_exit:take_profit",  # engine TP fired
        ),
    ]
    diag = BacktestExecutionDiagnostics(
        exit_rule_firings={"take_profit": 1},
        exit_rule_firings_by_symbol={"AAA": {"take_profit": 1}},
    )
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=trades,
        diagnostics=diag,
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []
