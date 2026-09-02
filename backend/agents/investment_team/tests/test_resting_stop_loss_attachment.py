"""Tests for migrating ``StopLossRule(basis="entry_price", style="market")`` to a
resting ``STOP`` order attached at entry-fill (step 1 of 3 for that migration).

Covers:

- ``_is_resting_stop_loss``: the eligibility predicate (basis/style/pct bound).
- ``_stop_loss_rule_to_leg_specs`` / ``resolve_resting_stop_loss_attachment``: the
  translation into the generalized exit-leg attachment plumbing, and that its
  price math matches ``rule_compiler._stop_loss_level`` (the bar-close evaluator's
  own formula) exactly.
- ``_EngineEntryDispatcher``: ``maybe_emit`` attaches the resolved ``StopAttachment``
  via ``attached_exits`` (not the fixed ``attached_stop_loss`` bracket field), and
  omits it for an ineligible rule.
- The short-safety auto-stop landmine: a ``pct=1.0`` ``StopLossRule`` (the shape
  ``TradingService`` auto-injects when a spec allows shorts with no explicit
  stop) is never fed through this path, since ``ExitLegSpec.pct`` requires
  ``pct < 1.0`` and would otherwise raise at entry-emission time.
- End-to-end: a dispatcher-emitted entry with this attachment materializes a
  resting STOP order only after the entry fills, and that order fills the
  position when the stop level is crossed.
"""

from __future__ import annotations

import pytest

from investment_team.execution.bar_safety import BarSafetyAssertion
from investment_team.execution.risk_filter import RiskFilter, RiskLimits
from investment_team.strategy_lab.executor.predicate_evaluator import (
    BarRecord,
    StreamingHistoryView,
)
from investment_team.strategy_lab.executor.rule_compiler import PositionState, _stop_loss_level
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    FixedFractionSizing,
    Predicate,
    StopLossRule,
)
from investment_team.trading_service.engine.execution_model import RealisticExecutionModel
from investment_team.trading_service.engine.fill_simulator import FillSimulator, FillSimulatorConfig
from investment_team.trading_service.engine.order_book import OrderBook
from investment_team.trading_service.engine.portfolio import Portfolio
from investment_team.trading_service.service import (
    TradingServiceResult,
    _EngineEntryDispatcher,
    _is_resting_stop_loss,
    _stop_loss_rule_to_leg_specs,
    resolve_resting_stop_loss_attachment,
)
from investment_team.trading_service.strategy.contract import (
    Bar,
    ExitLegSpec,
    OrderRequest,
    OrderSide,
    OrderType,
    StopAttachment,
)

# ---------------------------------------------------------------------------
# _is_resting_stop_loss: eligibility predicate
# ---------------------------------------------------------------------------


def test_entry_price_market_rule_is_eligible() -> None:
    """The exact variant this migration targets is eligible."""
    assert (
        _is_resting_stop_loss(StopLossRule(pct=0.03, basis="entry_price", style="market")) is True
    )


@pytest.mark.parametrize("basis", ["trailing_high", "trailing_low"])
def test_trailing_basis_is_not_eligible(basis: str) -> None:
    """A trailing basis is out of scope for this migration (future issue)."""
    assert _is_resting_stop_loss(StopLossRule(pct=0.03, basis=basis)) is False


def test_limit_style_is_not_eligible() -> None:
    """``style="limit"`` is out of scope for this migration (future issue)."""
    rule = StopLossRule(pct=0.03, basis="entry_price", style="limit", limit_offset_pct=0.01)
    assert _is_resting_stop_loss(rule) is False


def test_pct_equal_to_one_is_not_eligible() -> None:
    """``pct=1.0`` — the exact shape of the short-safety auto-injected stop — is
    excluded: ``ExitLegSpec.pct`` requires strictly < 1.0, so feeding this through
    would raise at entry-emission time instead of leaving the rule bar-close-only
    as it behaves today."""
    assert _is_resting_stop_loss(StopLossRule(pct=1.0, basis="entry_price")) is False


def test_non_stop_loss_rule_is_not_eligible() -> None:
    """A non-``StopLossRule`` exit rule is never eligible."""
    from investment_team.strategy_lab.spec_dsl import TakeProfitRule

    assert _is_resting_stop_loss(TakeProfitRule(pct=0.06)) is False


# ---------------------------------------------------------------------------
# _stop_loss_rule_to_leg_specs / resolve_resting_stop_loss_attachment
# ---------------------------------------------------------------------------


def test_leg_spec_translation_matches_bracket_shape() -> None:
    """Translates to the same single-STOP-leg shape ``_bracket_to_leg_specs``
    builds for a market-style bracket stop leg."""
    [leg] = _stop_loss_rule_to_leg_specs(StopLossRule(pct=0.03, basis="entry_price"))
    assert leg == ExitLegSpec(kind=OrderType.STOP, pct=0.03)


def test_leg_spec_translation_rejects_ineligible_rule() -> None:
    """The translation enforces its own precondition rather than silently
    producing a leg for a rule this migration doesn't cover."""
    with pytest.raises(AssertionError):
        _stop_loss_rule_to_leg_specs(StopLossRule(pct=0.03, basis="trailing_high"))


@pytest.mark.parametrize(
    "side, position_side, entry_price",
    [(OrderSide.LONG, "long", 100.0), (OrderSide.SHORT, "short", 100.0)],
)
def test_resolved_price_matches_bar_close_evaluator(
    side: OrderSide, position_side: str, entry_price: float
) -> None:
    """Acceptance criterion: the resting attachment's stop price is derived from
    the entry price and ``pct`` via the exact same formula
    ``rule_compiler._stop_loss_level`` uses for the bar-close evaluator, so the
    two paths can never disagree on where the stop sits."""
    rule = StopLossRule(pct=0.03, basis="entry_price")
    attachment = resolve_resting_stop_loss_attachment(rule, side, entry_price)
    position = PositionState(
        symbol="AAA",
        side=position_side,
        qty=100.0,
        entry_price=entry_price,
        high_since_entry=entry_price,
        low_since_entry=entry_price,
    )
    assert attachment.stop_price == pytest.approx(_stop_loss_level(rule, position))


def test_resolved_attachment_has_no_limit_or_trail_offset() -> None:
    """A plain market STOP leg — not STOP_LIMIT or TRAILING_STOP."""
    attachment = resolve_resting_stop_loss_attachment(
        StopLossRule(pct=0.03, basis="entry_price"), OrderSide.LONG, 100.0
    )
    assert isinstance(attachment, StopAttachment)
    assert attachment.limit_offset is None
    assert attachment.trail_offset is None


# ---------------------------------------------------------------------------
# _EngineEntryDispatcher: wiring
# ---------------------------------------------------------------------------


def _make_bar(symbol="AAA", close=100.0, timestamp="2024-01-10") -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=timestamp,
        timeframe="1d",
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=1000.0,
    )


def _make_portfolio(capital=10_000_000.0) -> Portfolio:
    return Portfolio(initial_capital=capital)


def _build_view(closes: list[float]) -> StreamingHistoryView:
    view = StreamingHistoryView()
    for i, c in enumerate(closes):
        view.append(
            BarRecord(
                timestamp=f"2024-01-{i + 1:02d}",
                open=c,
                high=c + 1,
                low=c - 1,
                close=c,
                volume=1000.0,
            )
        )
    return view


def _emit(exit_rules: list, side: str = "long", close: float = 100.0) -> OrderRequest:
    rhs = 90.0 if side == "long" else 110.0
    op = ">" if side == "long" else "<"
    rules = [EntryRule(side=side, when=Predicate(lhs="bar.close", op=op, rhs=rhs))]
    dispatcher = _EngineEntryDispatcher(
        entry_rules=rules,
        sizing=FixedFractionSizing(fraction=0.02),
        exit_rules=exit_rules,
        risk_limits=RiskLimits(max_position_pct=100),
        asset_class="stocks",
    )
    pending: list[OrderRequest] = []
    dispatcher.maybe_emit(
        cur_bar=_make_bar(close=close),
        portfolio=_make_portfolio(),
        pending_for_prev=pending,
        views={"AAA": _build_view([close, close])},
        result=TradingServiceResult(),
    )
    assert len(pending) == 1
    return pending[0]


def test_dispatcher_attaches_resting_stop_loss_via_attached_exits() -> None:
    """A spec whose sole exit is an eligible ``StopLossRule`` gets it attached on
    ``attached_exits`` — not the fixed ``attached_stop_loss`` bracket field, which
    stays reserved for an ``OcoBracketRule``."""
    req = _emit([StopLossRule(pct=0.03, basis="entry_price")], side="long", close=100.0)
    assert req.attached_stop_loss is None
    assert req.attached_take_profit is None
    assert len(req.attached_exits) == 1
    [attachment] = req.attached_exits
    assert isinstance(attachment, StopAttachment)
    assert attachment.stop_price == pytest.approx(97.0)


def test_dispatcher_attaches_resting_stop_loss_short() -> None:
    """Short mirror: the resolved stop sits above the reference."""
    req = _emit([StopLossRule(pct=0.03, basis="entry_price")], side="short", close=100.0)
    [attachment] = req.attached_exits
    assert attachment.stop_price == pytest.approx(103.0)


def test_dispatcher_omits_attachment_for_ineligible_rule() -> None:
    """A ``style="limit"`` rule (out of scope for this migration) is left alone —
    no resting attachment, so it remains purely bar-close evaluated."""
    rule = StopLossRule(pct=0.03, basis="entry_price", style="limit", limit_offset_pct=0.01)
    req = _emit([rule], side="long", close=100.0)
    assert req.attached_exits == []


def test_dispatcher_omits_attachment_for_no_exit_rules() -> None:
    """No exit rules at all → no attachment (existing behavior unaffected)."""
    req = _emit([], side="long", close=100.0)
    assert req.attached_exits == []
    assert req.attached_stop_loss is None


def test_dispatcher_does_not_attach_short_safety_auto_stop_shape() -> None:
    """Regression test for the auto-injection landmine: a spec carrying the exact
    rule shape ``TradingService`` auto-injects for short-safety
    (``StopLossRule(pct=1.0, basis="entry_price")``) must NOT be turned into a
    resting attachment for a long entry — that would attempt
    ``ExitLegSpec(pct=1.0)``, which raises, breaking every long entry on any spec
    where shorts are possible. It must simply pass through unattached, exactly as
    it behaves today."""
    req = _emit([StopLossRule(pct=1.0, basis="entry_price")], side="long", close=100.0)
    assert req.attached_exits == []
    assert req.validate_prices() is None  # does not raise


# ---------------------------------------------------------------------------
# End-to-end: resting order materializes only after entry fill
# ---------------------------------------------------------------------------


def _bar(ts, *, open_price=100.0, high=None, low=None, close=None, volume=1_000_000.0) -> Bar:
    return Bar(
        symbol="AAA",
        timestamp=ts,
        timeframe="1d",
        open=open_price,
        high=high if high is not None else open_price + 1.0,
        low=low if low is not None else open_price - 1.0,
        close=close if close is not None else open_price,
        volume=volume,
    )


def _make_simulator():
    portfolio = Portfolio(initial_capital=10_000_000.0)
    order_book = OrderBook()
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=RiskFilter(RiskLimits(max_position_pct=100, max_gross_leverage=10.0)),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(),
        execution_model=RealisticExecutionModel(participation_cap=0.10),
    )
    return sim, order_book, portfolio


def test_end_to_end_resting_stop_only_materializes_after_entry_fill() -> None:
    """Acceptance criterion: the resting STOP order is attached only once the
    entry has filled — never resting against an unfilled position — and, once
    materialized, fills the position when the bar crosses the stop level."""
    sim, order_book, portfolio = _make_simulator()
    req = _emit([StopLossRule(pct=0.05, basis="entry_price")], side="long", close=100.0)
    parent = order_book.submit(
        req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )

    # Before any bar is processed the entry hasn't filled yet: no resting child.
    assert order_book.children_of(parent.order_id) == []

    # Bar 2: entry fills at the open; the resting STOP child materializes at 95.
    sim.process_bar(_bar("2024-01-02", open_price=100.0))
    children = order_book.children_of(parent.order_id)
    assert len(children) == 1
    assert children[0].request.order_type == OrderType.STOP
    assert children[0].request.stop_price == pytest.approx(95.0)

    # Bar 3: low crosses 95 → the resting STOP fills and closes the position.
    outcome = sim.process_bar(_bar("2024-01-03", open_price=97.0, high=98.0, low=93.0, close=94.0))
    assert len(outcome.closed_trades) == 1
    assert "AAA" not in portfolio.positions
    assert order_book.children_of(parent.order_id) == []
