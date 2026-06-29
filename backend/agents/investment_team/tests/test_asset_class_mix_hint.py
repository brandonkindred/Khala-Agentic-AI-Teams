"""Regression tests for ``asset_class_mix_hint`` (#535).

Verifies that ``options`` — which the validator now rejects as a
critical gate failure — is no longer surfaced as an underrepresented
asset class the LLM should favor.
"""

from __future__ import annotations

import uuid

import pytest

from investment_team.api import main as lab_main
from investment_team.models import (
    BacktestConfig,
    BacktestRecord,
    BacktestResult,
    StrategyLabRecord,
    StrategySpec,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    Predicate,
    StopLossRule,
)
from investment_team.strategy_lab_context import asset_class_mix_hint


def _stub_backtest_result(
    *, annualized_return_pct: float = 5.0, win_rate_pct: float = 55.0
) -> BacktestResult:
    return BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=annualized_return_pct,
        volatility_pct=12.0,
        sharpe_ratio=0.5,
        max_drawdown_pct=-3.0,
        win_rate_pct=win_rate_pct,
        profit_factor=1.2,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )


def _record(
    asset_class: str,
    *,
    backtest_status: str = "completed",
    annual: float = 5.0,
    win: float = 55.0,
) -> StrategyLabRecord:
    suffix = uuid.uuid4().hex[:6]
    strategy = StrategySpec(
        strategy_id=f"s-{suffix}",
        authored_by="test",
        asset_class=asset_class,
        hypothesis="h",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))],
        exit_rules=[StopLossRule(pct=0.03)],
        risk_limits={},
        speculative=False,
    )
    now = lab_main._now()
    backtest = BacktestRecord(
        backtest_id=f"bt-{suffix}",
        strategy_id=strategy.strategy_id,
        strategy=strategy,
        config=BacktestConfig(start_date="2024-01-01", end_date="2024-12-31"),
        submitted_by="test",
        submitted_at=now,
        completed_at=now,
        status=backtest_status,
        result=_stub_backtest_result(annualized_return_pct=annual, win_rate_pct=win),
        notes=[],
        trades=[],
    )
    return StrategyLabRecord(
        lab_record_id=f"lab-{suffix}",
        strategy=strategy,
        backtest=backtest,
        is_winning=False,
        strategy_rationale="r",
        analysis_narrative="ok",
        created_at=now,
        quality_gate_results=[],
    )


def test_hint_no_records_omits_options() -> None:
    """With no prior strategies the menu of choices must not include options."""
    out = asset_class_mix_hint([])
    assert "options" not in out.lower(), out


def test_hint_with_history_never_lists_options_as_underrepresented() -> None:
    """A history of stocks-heavy runs would have left options=0 in the count
    dict before the fix, sending the LLM to options as 'underrepresented'."""
    records = [_record("stocks") for _ in range(5)] + [_record("crypto")]
    out = asset_class_mix_hint(records)

    # The recent-counts breakdown must not enumerate options at all.
    assert "options=" not in out, out
    # The underrepresented-line steering must not name options.
    assert "options" not in out.lower(), out


def test_hint_count_includes_supported_classes() -> None:
    """The supported classes should still appear in the count breakdown so
    the LLM gets useful diversification steering."""
    records = [_record("stocks") for _ in range(3)]
    out = asset_class_mix_hint(records)
    for ac in ("stocks", "crypto", "forex", "futures", "commodities"):
        assert f"{ac}=" in out, f"expected '{ac}=' in counts, got: {out}"


def test_hint_excludes_failed_short_circuit_records_from_counts() -> None:
    """A short-circuited cycle (status ``failed: …``) never ran a backtest and
    its recorded asset_class may be a coerced placeholder (an unsupported class
    like ``bonds`` is canonicalized to ``stocks`` for schema validity before
    being routed to redesign). Such records must not be counted, or a rejected,
    never-backtested design would pollute the stock history and skew steering."""
    completed = [_record("crypto") for _ in range(2)]
    failed = [_record("stocks", backtest_status="failed: spec_unimplementable") for _ in range(5)]
    out = asset_class_mix_hint(completed + failed)
    # Only the two completed crypto runs count; the coerced-stocks failures
    # must not register as a stocks-heavy window.
    assert "stocks=0" in out, out
    assert "crypto=2" in out, out
    assert "Equities are relatively heavy" not in out, out


def test_hint_all_failed_records_falls_back_to_neutral_menu() -> None:
    """When every record is a short-circuit (never executed), there is no
    history to steer from — emit the neutral no-history menu rather than a
    misleading all-zero count derived from coerced placeholders."""
    records = [_record("stocks", backtest_status="failed: spec_unimplementable") for _ in range(3)]
    out = asset_class_mix_hint(records)
    assert "No executed lab backtests yet" in out, out


def test_hint_excludes_all_pre_backtest_short_circuit_statuses() -> None:
    """Every status the orchestrator passes to ``_build_short_circuit_record``
    (none of which ran a backtest) must be excluded — including
    ``design_not_ready`` and ``budget_exhausted``, which are persisted before
    code synthesis just like the spec/synthesis failures. Otherwise a restart
    that rebuilds the hint from persisted ``prior_records`` would count these
    never-executed designs as real asset-class history."""
    for status in (
        "failed: spec_unimplementable",
        "failed: spec_validation",
        "failed: code_synthesis",
        "failed: design_not_ready",
        "failed: budget_exhausted",
    ):
        records = [_record("crypto")] + [
            _record("stocks", backtest_status=status) for _ in range(5)
        ]
        out = asset_class_mix_hint(records)
        assert "stocks=0" in out, f"{status}: {out}"
        assert "crypto=1" in out, f"{status}: {out}"


def test_hint_counts_executed_but_losing_failed_backtests() -> None:
    """Executed-but-losing cycles use ``failed`` / ``failed: max_refinement_rounds``
    but DID run a backtest with a genuine canonical class — they must keep
    counting, or repeated failed futures/forex runs would vanish from the
    diversity steering. Only never-executed short-circuits are excluded."""
    records = [_record("futures", backtest_status="failed") for _ in range(3)] + [
        _record("forex", backtest_status="failed: max_refinement_rounds") for _ in range(2)
    ]
    out = asset_class_mix_hint(records)
    # All five executed-but-losing records count toward the diversity history.
    assert "futures=3" in out, out
    assert "forex=2" in out, out
    assert "stocks=0" in out, out


# ---------------------------------------------------------------------------
# Objective-aware mode gating (exploit vs explore)
# ---------------------------------------------------------------------------


def test_explore_mode_emits_equities_heavy_nudge() -> None:
    """``explore`` keeps the portfolio-rotation nudge: a stocks-heavy window
    must surface the anti-equities 'strongly prefer' steering."""
    records = [_record("stocks") for _ in range(5)] + [_record("crypto")]
    out = asset_class_mix_hint(records, mode="explore")
    assert "Equities are relatively heavy" in out, out
    assert "strongly prefer" in out, out
    # Edge-exploitation language belongs to the other mode only.
    assert "lean into your demonstrated edge" not in out, out


def test_exploit_mode_drops_equities_nudge() -> None:
    """``exploit`` (the default) never emits the rotation nudge, even on a
    stocks-heavy window — concentrating on edge is the point."""
    records = [_record("stocks") for _ in range(5)] + [_record("crypto")]
    out = asset_class_mix_hint(records, mode="exploit")
    assert "Equities are relatively heavy" not in out, out
    assert "Underrepresented line" not in out, out


def test_exploit_mode_names_top_scoring_class() -> None:
    """``exploit`` steers toward the highest-scoring asset class. Crypto here
    has the best annualized return, so it must be named as the edge."""
    records = [_record("crypto", annual=20.0, win=60.0) for _ in range(3)] + [
        _record("stocks", annual=3.0, win=50.0) for _ in range(3)
    ]
    out = asset_class_mix_hint(records, mode="exploit")
    assert "lean into your demonstrated edge" in out, out
    assert "crypto scores best" in out, out
    assert "Equities are relatively heavy" not in out, out


def test_explore_is_the_helper_default() -> None:
    """The shared helper defaults to ``explore`` so existing callers keep the
    historical diversity hint; the design agent opts into ``exploit`` itself."""
    records = [_record("crypto", annual=20.0) for _ in range(2)] + [_record("stocks")]
    assert asset_class_mix_hint(records) == asset_class_mix_hint(records, mode="explore")


def test_exploit_mode_respects_exclude_when_ranking_edge() -> None:
    """The excluded class must never be named as the edge even when it scores
    highest — steering must stay inside the run's allowed classes."""
    records = [_record("crypto", annual=30.0, win=65.0) for _ in range(2)] + [
        _record("forex", annual=10.0, win=52.0) for _ in range(2)
    ]
    out = asset_class_mix_hint(records, exclude=["crypto"], mode="exploit")
    assert "forex scores best" in out, out
    assert "crypto" not in out.lower(), out


def test_exploit_mode_no_attributable_edge_falls_back_to_neutral() -> None:
    """When the only executed history is in an excluded class there is no
    in-bounds edge to name, so steer with the neutral menu instead of
    fabricating a preference."""
    records = [_record("stocks", annual=12.0) for _ in range(3)]
    out = asset_class_mix_hint(records, exclude=["stocks"], mode="exploit")
    assert "No per-class edge attributable yet" in out, out
    # Still offers the allowed menu to choose from.
    assert "crypto" in out.lower(), out


def test_invalid_mode_is_a_precondition_violation() -> None:
    """``mode`` outside the known set is a caller bug — fail loudly per DbC."""
    with pytest.raises(AssertionError):
        asset_class_mix_hint([], mode="bogus")
