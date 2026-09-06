"""Tests for migrating ``StopLossRule(basis="entry_price", style="market")`` to a
resting ``STOP`` order attached at entry-fill.

Exercises that resting-order mechanism directly and so opts into it
explicitly (via ``_EngineEntryDispatcher(resting_stop_loss_enabled=True, ...)``
in the shared ``_emit`` helper below) — the run feature check
(``trading_service.service._resting_stop_loss_enabled``) defaults it off, so a
run configured with no explicit override stays on the bar-close evaluator this
suite is not exercising. See ``test_stop_loss_mechanism_coexistence.py`` for
the mutual-exclusion contract between the two mechanisms and the bar-close
path's own unit tests, which stay on the default (bar-close) mechanism and
require no changes here.

Covers:

- ``_is_resting_stop_loss``: the eligibility predicate (basis/style/pct bound).
- ``_stop_loss_rule_to_leg_specs`` / ``resolve_resting_stop_loss_attachment``: the
  translation into the generalized exit-leg attachment plumbing, and that its
  price math matches ``rule_compiler.stop_loss_level`` (the bar-close evaluator's
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

from datetime import date, timedelta
from typing import Sequence

import pytest

from investment_team.execution.bar_safety import BarSafetyAssertion
from investment_team.execution.risk_filter import RiskFilter, RiskLimits
from investment_team.models import BacktestConfig, BacktestExecutionDiagnostics
from investment_team.strategy_lab.executor.predicate_evaluator import (
    BarRecord,
    StreamingHistoryView,
)
from investment_team.strategy_lab.executor.rule_compiler import PositionState, stop_loss_level
from investment_team.strategy_lab.quality_gates.exit_rule_conformance import (
    ExitRuleConformanceGate,
)
from investment_team.strategy_lab.spec_dsl import (
    BracketStopLeg,
    BracketTakeProfitLeg,
    EntryRule,
    ExitRule,
    FixedFractionSizing,
    OcoBracketRule,
    Predicate,
    StopLossRule,
    TakeProfitRule,
    protective_stop_price,
)
from investment_team.trading_service.engine.execution_model import RealisticExecutionModel
from investment_team.trading_service.engine.fill_simulator import (
    ENGINE_EXIT_REASON_PREFIX,
    FillSimulator,
    FillSimulatorConfig,
)
from investment_team.trading_service.engine.order_book import OrderBook
from investment_team.trading_service.engine.portfolio import Portfolio
from investment_team.trading_service.service import (
    TradingServiceResult,
    _apply_fill_outcome_events,
    _EngineEntryDispatcher,
    _is_resting_stop_loss,
    _stop_loss_rule_to_leg_specs,
    resolve_bracket_attachments,
    resolve_resting_stop_loss_attachment,
)
from investment_team.trading_service.strategy.contract import (
    Bar,
    ExitLegSpec,
    OrderRequest,
    OrderSide,
    OrderType,
    StopAttachment,
    TimeInForce,
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
    assert _is_resting_stop_loss(TakeProfitRule(pct=0.06)) is False


@pytest.mark.parametrize("pct", [0.0, -0.05])
def test_non_positive_pct_is_not_eligible(pct: float) -> None:
    """The predicate's own ``0 < pct`` check is defense-in-depth: ``StopLossRule.pct``
    already rejects non-positive values at construction (``Field(gt=0)``), so a
    non-positive-pct rule can only reach the predicate via ``model_construct``
    (bypassing validation) — exactly the case the isinstance/bound checks inside
    ``_is_resting_stop_loss`` exist to catch defensively."""
    rule = StopLossRule.model_construct(
        kind="stop_loss",
        pct=pct,
        basis="entry_price",
        style="market",
        limit_offset_pct=None,
        note="",
    )
    assert _is_resting_stop_loss(rule) is False


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
    producing a leg for a rule this migration doesn't cover — via an explicit
    raise (not assert) so the contract survives ``python -O``."""
    with pytest.raises(ValueError, match="resting-eligible StopLossRule"):
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
    ``rule_compiler.stop_loss_level`` uses for the bar-close evaluator, so the
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
    assert attachment.stop_price == pytest.approx(stop_loss_level(rule, position))


def test_resolve_resting_stop_loss_attachment_rejects_ineligible_rule() -> None:
    """``resolve_resting_stop_loss_attachment`` enforces the same precondition as
    ``_stop_loss_rule_to_leg_specs``, which it delegates to, rather than silently
    resolving a rule this migration doesn't cover — unlike the leg-spec
    translation, this adapter's own ineligible-input behavior wasn't previously
    pinned by a dedicated test."""
    with pytest.raises(ValueError, match="resting-eligible StopLossRule"):
        resolve_resting_stop_loss_attachment(
            StopLossRule(pct=0.03, basis="trailing_high"), OrderSide.LONG, 100.0
        )


def test_resolved_attachment_has_no_limit_or_trail_offset() -> None:
    """A plain market STOP leg — not STOP_LIMIT or TRAILING_STOP."""
    attachment = resolve_resting_stop_loss_attachment(
        StopLossRule(pct=0.03, basis="entry_price"), OrderSide.LONG, 100.0
    )
    assert isinstance(attachment, StopAttachment)
    assert attachment.limit_offset is None
    assert attachment.trail_offset is None


def test_resolved_attachment_carries_entry_price_pct_for_reanchoring() -> None:
    """``entry_price_pct`` is set to the rule's ``pct`` so materialization can
    re-derive ``stop_price`` from the entry's actual fill price rather than
    trusting this ``ref_price``-anchored preview verbatim (see
    ``StopAttachment.entry_price_pct`` and the gap-reanchoring end-to-end test)."""
    attachment = resolve_resting_stop_loss_attachment(
        StopLossRule(pct=0.03, basis="entry_price"), OrderSide.LONG, 100.0
    )
    assert attachment.entry_price_pct == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# entry_price_pct bounds validation (OrderRequest.validate_prices)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_pct", [0.0, 1.0, -0.1, 1.5])
def test_validate_prices_rejects_out_of_range_entry_price_pct(bad_pct: float) -> None:
    """``StopAttachment.entry_price_pct`` shares ``ExitLegSpec.pct``'s strict
    ``(0, 1)`` bound (see ``_is_resting_stop_loss``); a leg carrying a value
    outside that bound must fail loudly at ``validate_prices`` rather than
    silently mis-anchoring at materialization time."""
    attachment = resolve_resting_stop_loss_attachment(
        StopLossRule(pct=0.03, basis="entry_price"), OrderSide.LONG, 100.0
    )
    attachment.entry_price_pct = bad_pct
    req = OrderRequest(
        client_order_id="co-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=10,
        order_type=OrderType.MARKET,
        attached_exits=[attachment],
    )
    with pytest.raises(ValueError, match="entry_price_pct"):
        req.validate_prices()


def test_validate_prices_accepts_in_range_entry_price_pct() -> None:
    """A pct strictly inside (0, 1) passes ``validate_prices`` — the
    complement of the rejection test."""
    attachment = resolve_resting_stop_loss_attachment(
        StopLossRule(pct=0.03, basis="entry_price"), OrderSide.LONG, 100.0
    )
    req = OrderRequest(
        client_order_id="co-2",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=10,
        order_type=OrderType.MARKET,
        attached_exits=[attachment],
    )
    req.validate_prices()  # does not raise


# ---------------------------------------------------------------------------
# protective_stop_price: shared geometry helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("is_long", "expected"),
    [(True, 97.0), (False, 103.0)],
)
def test_protective_stop_price_matches_direction(is_long: bool, expected: float) -> None:
    """Long stops sit below ref price, short stops above — the shared helper
    encodes both directions."""
    assert protective_stop_price(100.0, 0.03, is_long=is_long) == pytest.approx(expected)


def test_stop_loss_level_delegates_to_protective_stop_price() -> None:
    """``rule_compiler.stop_loss_level`` and the shared helper must never
    drift apart — they are the same formula, not two copies of it."""
    rule = StopLossRule(pct=0.05, basis="entry_price")
    long_pos = PositionState(
        symbol="AAA",
        side="long",
        qty=10.0,
        entry_price=200.0,
        high_since_entry=200.0,
        low_since_entry=200.0,
    )
    short_pos = PositionState(
        symbol="AAA",
        side="short",
        qty=10.0,
        entry_price=200.0,
        high_since_entry=200.0,
        low_since_entry=200.0,
    )
    assert stop_loss_level(rule, long_pos) == protective_stop_price(200.0, 0.05, is_long=True)
    assert stop_loss_level(rule, short_pos) == protective_stop_price(200.0, 0.05, is_long=False)


def test_resolve_resting_stop_loss_attachment_delegates_to_protective_stop_price() -> None:
    """``resolve_resting_stop_loss_attachment``'s preview price is the same
    shared-helper formula as the bar-close evaluator's, for both sides."""
    long_attachment = resolve_resting_stop_loss_attachment(
        StopLossRule(pct=0.04, basis="entry_price"), OrderSide.LONG, 150.0
    )
    short_attachment = resolve_resting_stop_loss_attachment(
        StopLossRule(pct=0.04, basis="entry_price"), OrderSide.SHORT, 150.0
    )
    assert long_attachment.stop_price == pytest.approx(
        protective_stop_price(150.0, 0.04, is_long=True)
    )
    assert short_attachment.stop_price == pytest.approx(
        protective_stop_price(150.0, 0.04, is_long=False)
    )


# ---------------------------------------------------------------------------
# _EngineEntryDispatcher: wiring
# ---------------------------------------------------------------------------


# Dispatcher-wiring bars: only ``close`` (and an implied symbol) vary across
# call sites here — distinct from ``_bar`` below, which the end-to-end section
# uses for explicit per-bar OHLC control (gaps, wicks) across a bar sequence.
def _make_bar(symbol: str = "AAA", close: float = 100.0, timestamp: str = "2024-01-10") -> Bar:
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


def _make_portfolio(capital: float = 10_000_000.0) -> Portfolio:
    return Portfolio(initial_capital=capital)


def _build_view(closes: list[float]) -> StreamingHistoryView:
    # Real date arithmetic (not a zero-padded day-of-month string) so this
    # stays valid for a ``closes`` list longer than 31 entries, unlike a
    # naive ``f"2024-01-{i + 1:02d}"`` which would emit an impossible date
    # such as "2024-01-32".
    start = date(2024, 1, 1)
    view = StreamingHistoryView()
    for i, c in enumerate(closes):
        view.append(
            BarRecord(
                timestamp=(start + timedelta(days=i)).isoformat(),
                open=c,
                high=c + 1,
                low=c - 1,
                close=c,
                volume=1000.0,
            )
        )
    return view


def _emit(exit_rules: Sequence[ExitRule], side: str = "long", close: float = 100.0) -> OrderRequest:
    rhs = 90.0 if side == "long" else 110.0
    op = ">" if side == "long" else "<"
    rules = [EntryRule(side=side, when=Predicate(lhs="bar.close", op=op, rhs=rhs))]
    dispatcher = _EngineEntryDispatcher(
        entry_rules=rules,
        sizing=FixedFractionSizing(fraction=0.02),
        exit_rules=exit_rules,
        risk_limits=RiskLimits(max_position_pct=100),
        asset_class="stocks",
        # This suite exercises the resting-order mechanism directly; the run
        # feature check defaults it off (see ``_resting_stop_loss_enabled``),
        # so every test in this file opts in explicitly here.
        resting_stop_loss_enabled=True,
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


def test_dispatcher_attaches_stop_among_mixed_exit_rules() -> None:
    """The dispatcher scans the full ``exit_rules`` list — an eligible stop is
    found and attached even when a non-eligible exit rule precedes it, not just
    when it is the spec's sole exit rule."""
    req = _emit(
        [TakeProfitRule(pct=0.06), StopLossRule(pct=0.03, basis="entry_price")],
        side="long",
        close=100.0,
    )
    [attachment] = req.attached_exits
    assert isinstance(attachment, StopAttachment)
    assert attachment.stop_price == pytest.approx(97.0)


def test_dispatcher_picks_first_eligible_stop_among_several() -> None:
    """When more than one eligible ``StopLossRule`` is present, the first in spec
    order wins — mirroring ``first_side_stop_factor``'s spec-order precedent."""
    req = _emit(
        [
            StopLossRule(pct=0.03, basis="entry_price"),
            StopLossRule(pct=0.10, basis="entry_price"),
        ],
        side="long",
        close=100.0,
    )
    [attachment] = req.attached_exits
    assert attachment.stop_price == pytest.approx(97.0)


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


def _bar(
    ts: str,
    *,
    open_price: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    volume: float = 1_000_000.0,
) -> Bar:
    """Build an OHLC-valid ``Bar`` for AAA; ``high``/``low`` default to
    brackets around ``open``/``close`` so ``BarSafetyAssertion`` never rejects
    a bar where only ``close`` was overridden."""
    resolved_close = close if close is not None else open_price
    return Bar(
        symbol="AAA",
        timestamp=ts,
        timeframe="1d",
        open=open_price,
        # Derived from both open and close (not open alone) so an override of
        # only close still yields an OHLC-valid bar — BarSafetyAssertion
        # rejects high < close / low > close.
        high=high if high is not None else max(open_price, resolved_close) + 1.0,
        low=low if low is not None else min(open_price, resolved_close) - 1.0,
        close=resolved_close,
        volume=volume,
    )


def _make_simulator() -> tuple[FillSimulator, OrderBook, Portfolio]:
    """Build a deterministic ``FillSimulator``/``OrderBook``/``Portfolio`` triple.
    Zero slippage and costs so fills land exactly at open/stop prices;
    ``participation_cap`` is sized well above the 2%-fraction position so
    entries always fully fill."""
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
    assert "AAA" in portfolio.positions  # entry filled — the child now rests against it
    children = order_book.children_of(parent.order_id)
    assert len(children) == 1
    assert children[0].request.order_type == OrderType.STOP
    assert children[0].request.stop_price == pytest.approx(95.0)

    # Bar 3: low crosses 95 → the resting STOP fills and closes the position.
    outcome = sim.process_bar(_bar("2024-01-03", open_price=97.0, high=98.0, low=93.0, close=94.0))
    assert len(outcome.closed_trades) == 1
    assert "AAA" not in portfolio.positions
    assert order_book.children_of(parent.order_id) == []


def test_end_to_end_resting_stop_reanchors_to_actual_fill_price_on_gap() -> None:
    """The resting child's ``stop_price`` is derived from the entry's ACTUAL fill
    price, not the stale signal-bar-close preview the dispatcher resolved it
    from — otherwise, on a gap (``fill_price != signal_close``), this resting
    order and the still-independently-active bar-close evaluator (which anchors
    to the real fill price via ``PositionState.entry_price``) would disagree
    about where the stop sits. Signal close is 100 (preview stop 97 at pct=0.03),
    but the entry actually gaps down and fills at 90 on bar 2 — the materialized
    child must sit at 90 * (1 - 0.03) = 87.3, not the stale 97 (which would sit
    ABOVE the fill price and could liquidate the position almost immediately)."""
    sim, order_book, portfolio = _make_simulator()
    req = _emit([StopLossRule(pct=0.03, basis="entry_price")], side="long", close=100.0)
    [preview] = req.attached_exits
    assert preview.stop_price == pytest.approx(97.0)  # the stale, signal-close-anchored preview
    parent = order_book.submit(
        req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )

    # Bar 2: entry gaps down and fills at the (lower) open, not the signal close.
    sim.process_bar(_bar("2024-01-02", open_price=90.0))
    assert "AAA" in portfolio.positions
    assert portfolio.positions["AAA"].entry_price == pytest.approx(90.0)
    [child] = order_book.children_of(parent.order_id)
    assert child.request.stop_price == pytest.approx(87.3)


def test_end_to_end_resting_stop_short_side_materializes_and_closes_position() -> None:
    """Short mirror of ``test_end_to_end_resting_stop_only_materializes_after_entry_fill``:
    for a short, the resting STOP sits above the fill price and is a buy-side
    trigger — this drives a short entry through FillSimulator/OrderBook end to
    end to prove that direction isn't inverted anywhere in the materialization
    or trigger path (the long side alone wouldn't catch that class of bug)."""
    sim, order_book, portfolio = _make_simulator()
    req = _emit([StopLossRule(pct=0.05, basis="entry_price")], side="short", close=100.0)
    parent = order_book.submit(
        req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )

    # Before any bar is processed the entry hasn't filled yet: no resting child.
    assert order_book.children_of(parent.order_id) == []

    # Bar 2: entry fills at the open; the resting STOP child materializes at 105.
    sim.process_bar(_bar("2024-01-02", open_price=100.0))
    assert "AAA" in portfolio.positions
    children = order_book.children_of(parent.order_id)
    assert len(children) == 1
    assert children[0].request.order_type == OrderType.STOP
    assert children[0].request.stop_price == pytest.approx(105.0)

    # Bar 3: high crosses 105 → the resting STOP fills and closes the position.
    outcome = sim.process_bar(
        _bar("2024-01-03", open_price=102.0, high=107.0, low=101.0, close=106.0)
    )
    assert len(outcome.closed_trades) == 1
    assert "AAA" not in portfolio.positions
    assert order_book.children_of(parent.order_id) == []


# ---------------------------------------------------------------------------
# Fill semantics: through-bar, gap-through, no-trigger, reason attribution,
# and direct comparison against the bracket stop leg's behavior.
# ---------------------------------------------------------------------------


def test_through_bar_fills_resting_stop_at_exact_price_long() -> None:
    """A bar whose open stays on the safe side of the stop but whose low
    crosses it fills at the stop's EXACT price, not a worse one — the
    defining behavior a through-bar (as opposed to a gap-through bar) must
    have."""
    sim, order_book, _portfolio = _make_simulator()
    req = _emit([StopLossRule(pct=0.05, basis="entry_price")], side="long", close=100.0)
    order_book.submit(
        req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )
    sim.process_bar(_bar("2024-01-02", open_price=100.0))  # entry fills; stop rests at 95

    # Open (97) is above the stop (95); low (93) crosses it — a through-bar.
    outcome = sim.process_bar(_bar("2024-01-03", open_price=97.0, high=98.0, low=93.0, close=94.0))
    [trade] = outcome.closed_trades
    assert trade.exit_price == pytest.approx(95.0)


def test_through_bar_fills_resting_stop_at_exact_price_short() -> None:
    """Short mirror: a bar whose open stays on the safe side of the stop
    (below it, for a short) but whose high crosses it fills at the exact
    stop price."""
    sim, order_book, _portfolio = _make_simulator()
    req = _emit([StopLossRule(pct=0.05, basis="entry_price")], side="short", close=100.0)
    order_book.submit(
        req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )
    sim.process_bar(_bar("2024-01-02", open_price=100.0))  # entry fills; stop rests at 105

    # Open (103) is below the stop (105); high (107) crosses it — a through-bar.
    outcome = sim.process_bar(
        _bar("2024-01-03", open_price=103.0, high=107.0, low=102.0, close=106.0)
    )
    [trade] = outcome.closed_trades
    assert trade.exit_price == pytest.approx(105.0)


def test_gap_through_bar_fills_resting_stop_at_worse_of_open_long() -> None:
    """Acceptance criterion: a bar that gaps through the stop level fills at
    worse-of-open, not the stop price — the level was never actually traded,
    so filling at the nominal stop would be a more flattering, less faithful
    result."""
    sim, order_book, _portfolio = _make_simulator()
    req = _emit([StopLossRule(pct=0.05, basis="entry_price")], side="long", close=100.0)
    order_book.submit(
        req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )
    sim.process_bar(_bar("2024-01-02", open_price=100.0))  # entry fills; stop rests at 95

    # Open (90) has already gapped below the stop (95).
    outcome = sim.process_bar(_bar("2024-01-03", open_price=90.0, high=91.0, low=88.0, close=89.0))
    [trade] = outcome.closed_trades
    assert trade.exit_price == pytest.approx(90.0)
    assert trade.exit_price != pytest.approx(95.0)


def test_gap_through_bar_fills_resting_stop_at_worse_of_open_short() -> None:
    """Short mirror of the gap-through case."""
    sim, order_book, _portfolio = _make_simulator()
    req = _emit([StopLossRule(pct=0.05, basis="entry_price")], side="short", close=100.0)
    order_book.submit(
        req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )
    sim.process_bar(_bar("2024-01-02", open_price=100.0))  # entry fills; stop rests at 105

    # Open (110) has already gapped above the stop (105).
    outcome = sim.process_bar(
        _bar("2024-01-03", open_price=110.0, high=112.0, low=109.0, close=111.0)
    )
    [trade] = outcome.closed_trades
    assert trade.exit_price == pytest.approx(110.0)
    assert trade.exit_price != pytest.approx(105.0)


def test_bar_not_reaching_stop_leaves_order_resting() -> None:
    """A bar whose range never touches the stop level does not fill — the
    order stays resting and the position stays open."""
    sim, order_book, portfolio = _make_simulator()
    req = _emit([StopLossRule(pct=0.05, basis="entry_price")], side="long", close=100.0)
    parent = order_book.submit(
        req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )
    sim.process_bar(_bar("2024-01-02", open_price=100.0))  # entry fills; stop rests at 95

    # Low (96) never reaches the stop (95).
    outcome = sim.process_bar(_bar("2024-01-03", open_price=99.0, high=100.0, low=96.0, close=98.0))
    assert outcome.closed_trades == []
    assert "AAA" in portfolio.positions
    [child] = order_book.children_of(parent.order_id)
    assert child.request.stop_price == pytest.approx(95.0)


def test_resting_stop_fill_carries_engine_exit_stop_loss_reason() -> None:
    """Acceptance criterion: ``engine_exit:stop_loss`` reason attribution is
    preserved for a resting-stop fill — the same literal the bar-close
    evaluator stamps for this rule kind, which the alignment and conformance
    quality gates match exactly."""
    sim, order_book, _portfolio = _make_simulator()
    req = _emit([StopLossRule(pct=0.05, basis="entry_price")], side="long", close=100.0)
    order_book.submit(
        req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )
    sim.process_bar(_bar("2024-01-02", open_price=100.0))
    outcome = sim.process_bar(_bar("2024-01-03", open_price=97.0, high=98.0, low=93.0, close=94.0))
    [trade] = outcome.closed_trades
    assert trade.exit_reason == f"{ENGINE_EXIT_REASON_PREFIX}stop_loss"


def test_resting_stop_trade_record_exit_price_shape_matches_bracket_field_names() -> None:
    """Acceptance criterion: the realized exit price is recorded on the trade
    record in the same shape a bracket stop's is, so downstream consumers
    need no special case — the same fields a bracket stop's trade record
    carries are populated here with the expected values. The actual
    cross-path field-by-field comparison against a real bracket-stop trade
    lives in ``test_resting_stop_matches_bracket_stop_leg_fill_behavior``."""
    sim, order_book, _portfolio = _make_simulator()
    req = _emit([StopLossRule(pct=0.05, basis="entry_price")], side="long", close=100.0)
    order_book.submit(
        req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )
    sim.process_bar(_bar("2024-01-02", open_price=100.0))
    outcome = sim.process_bar(_bar("2024-01-03", open_price=97.0, high=98.0, low=93.0, close=94.0))
    [trade] = outcome.closed_trades
    assert trade.exit_price == pytest.approx(95.0)
    assert trade.exit_fill_price == pytest.approx(trade.exit_price)
    assert trade.exit_bid_price is not None
    assert trade.exit_order_type == OrderType.STOP.value


@pytest.mark.parametrize(
    ("open_price", "high", "low", "close", "expected_fill"),
    [
        pytest.param(97.0, 98.0, 93.0, 94.0, 95.0, id="through_bar"),
        pytest.param(90.0, 91.0, 88.0, 89.0, 90.0, id="gap_through"),
    ],
)
def test_resting_stop_matches_bracket_stop_leg_fill_behavior(
    open_price: float, high: float, low: float, close: float, expected_fill: float
) -> None:
    """Acceptance criterion: the resting stop's behavior matches the bracket
    stop leg's, verified by direct comparison on identical bars rather than
    by assumption — both a through-bar and a gap-through bar. The two paths'
    reasons are deliberately different (each keeps its own canonical
    attribution); everything else about the fill must agree."""
    pct = 0.05
    entry_price = 100.0

    resting_sim, resting_book, _resting_portfolio = _make_simulator()
    resting_stop = resolve_resting_stop_loss_attachment(
        StopLossRule(pct=pct, basis="entry_price"), OrderSide.LONG, entry_price
    )
    resting_req = OrderRequest(
        client_order_id="resting-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=100.0,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
        attached_exits=[resting_stop],
    )
    resting_book.submit(
        resting_req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )
    resting_sim.process_bar(_bar("2024-01-02", open_price=entry_price))

    bracket_sim, bracket_book, _bracket_portfolio = _make_simulator()
    bracket = OcoBracketRule(
        stop_loss=BracketStopLeg(pct=pct), take_profit=BracketTakeProfitLeg(pct=0.50)
    )
    bracket_stop, _bracket_take_profit = resolve_bracket_attachments(
        bracket, OrderSide.LONG, entry_price
    )
    bracket_req = OrderRequest(
        client_order_id="bracket-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=100.0,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
        attached_stop_loss=bracket_stop,
    )
    bracket_book.submit(
        bracket_req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )
    bracket_sim.process_bar(_bar("2024-01-02", open_price=entry_price))

    # Both resolvers must agree on where the stop sits before either bar fires.
    assert resting_stop.stop_price == pytest.approx(bracket_stop.stop_price)

    bar = _bar("2024-01-03", open_price=open_price, high=high, low=low, close=close)
    resting_outcome = resting_sim.process_bar(bar)
    bracket_outcome = bracket_sim.process_bar(bar)

    [resting_trade] = resting_outcome.closed_trades
    [bracket_trade] = bracket_outcome.closed_trades
    assert resting_trade.exit_price == pytest.approx(expected_fill)
    assert bracket_trade.exit_price == pytest.approx(expected_fill)
    assert resting_trade.exit_price == pytest.approx(bracket_trade.exit_price)
    assert resting_trade.net_pnl == pytest.approx(bracket_trade.net_pnl)
    assert resting_trade.return_pct == pytest.approx(bracket_trade.return_pct)
    assert resting_trade.exit_fill_price == pytest.approx(bracket_trade.exit_fill_price)
    assert resting_trade.exit_bid_price == pytest.approx(bracket_trade.exit_bid_price)
    assert resting_trade.exit_order_type == bracket_trade.exit_order_type

    assert resting_trade.exit_reason == f"{ENGINE_EXIT_REASON_PREFIX}stop_loss"
    assert bracket_trade.exit_reason == f"{ENGINE_EXIT_REASON_PREFIX}bracket_sl"


# ---------------------------------------------------------------------------
# Coexistence with the bar-close evaluator: firing-count reconciliation.
# The resting leg's materialization must credit its own firing count
# (independent of whether the bar-close evaluator also fires that bar) so
# ``exit_rule_conformance.py::_check_stop_loss`` never mistakes a below-floor
# resting-order-only close for an unreconciled leak. See the migration
# transitional-state comment on ``_EngineEntryDispatcher.__post_init__`` in
# ``trading_service.service``.
# ---------------------------------------------------------------------------


def test_resting_stop_materialization_emits_engine_exit_attached_event() -> None:
    """Acceptance criterion: materializing the resting entry_price/market
    stop-loss leg emits an ``engine_exit_attached`` diagnostic event at
    entry-fill time — independent of, and structurally earlier than, any
    later trigger/fill — so the firing-count telemetry can credit this leg
    even on a bar where the leg never fires."""
    sim, order_book, _portfolio = _make_simulator()
    req = _emit([StopLossRule(pct=0.05, basis="entry_price")], side="long", close=100.0)
    order_book.submit(
        req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )

    # Bar 2: entry fills and the resting STOP child materializes — the
    # attachment event fires here, not on a later triggering bar.
    outcome = sim.process_bar(_bar("2024-01-02", open_price=100.0))

    attached = [e for e in outcome.diagnostic_events if e.kind == "engine_exit_attached"]
    assert len(attached) == 1
    assert attached[0].symbol == "AAA"
    assert attached[0].reason == f"{ENGINE_EXIT_REASON_PREFIX}stop_loss"
    assert attached[0].order_type == OrderType.STOP.value


def test_bracket_stop_leg_materialization_does_not_emit_engine_exit_attached() -> None:
    """A fixed bracket stop leg (``attached_stop_loss``, no ``entry_price_pct``)
    must NOT emit ``engine_exit_attached`` — that event is exclusively for the
    resting entry_price/market stop-loss migration's leg (identified by
    ``entry_price_pct``, per ``StopAttachment``'s own docstring). Otherwise a
    bracket's stop would double-credit a firing counter it isn't governed by
    (bracket rules are excluded from the bar-close evaluator's ``exit_rules``
    entirely, so they have no reconciliation counterpart to begin with)."""
    sim, order_book, _portfolio = _make_simulator()
    bracket = OcoBracketRule(
        stop_loss=BracketStopLeg(pct=0.05), take_profit=BracketTakeProfitLeg(pct=0.50)
    )
    bracket_stop, _take_profit = resolve_bracket_attachments(bracket, OrderSide.LONG, 100.0)
    req = OrderRequest(
        client_order_id="bracket-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=100.0,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
        attached_stop_loss=bracket_stop,
    )
    order_book.submit(
        req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )

    outcome = sim.process_bar(_bar("2024-01-02", open_price=100.0))

    assert [e for e in outcome.diagnostic_events if e.kind == "engine_exit_attached"] == []


def test_gap_through_resting_only_close_does_not_trip_conformance_gate_false_critical() -> None:
    """The task's literal regression: a below-floor gap-through fill closed
    entirely by the resting order (no bar-close evaluator involved — this
    suite never instantiates ``_EngineExitDispatcher``) must not trip
    ``ExitRuleConformanceGate``'s stop-loss leak critical. Before the
    ``engine_exit_attached`` fix, this trade would have zero firing credit
    (the resting path never touched ``exit_rule_firings_by_symbol``) despite
    correctly carrying ``engine_exit:stop_loss`` (post-#7976) — a false
    positive. Follows the same gap-through setup as
    ``test_gap_through_bar_fills_resting_stop_at_worse_of_open_long``.
    """
    sim, order_book, _portfolio = _make_simulator()
    rule = StopLossRule(pct=0.05, basis="entry_price")
    req = _emit([rule], side="long", close=100.0)
    order_book.submit(
        req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )

    # Bar 2: entry fills; resting STOP child materializes at 95 and emits
    # its own "engine_exit_attached" credit.
    attach_outcome = sim.process_bar(_bar("2024-01-02", open_price=100.0))

    # Bar 3: open (90) has already gapped below the stop (95) — a
    # resting-order-only, below-floor close with no bar-close evaluator
    # in this test's setup to independently re-fire.
    fill_outcome = sim.process_bar(
        _bar("2024-01-03", open_price=90.0, high=91.0, low=88.0, close=89.0)
    )
    [trade] = fill_outcome.closed_trades
    assert trade.exit_reason == f"{ENGINE_EXIT_REASON_PREFIX}stop_loss"
    assert trade.return_pct < -5.0  # below the pct=0.05 nominal floor

    diagnostics = BacktestExecutionDiagnostics()
    _apply_fill_outcome_events(diagnostics, attach_outcome)
    _apply_fill_outcome_events(diagnostics, fill_outcome)
    assert diagnostics.exit_rule_firings_by_symbol.get("AAA", {}).get("stop_loss") == 1

    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[rule],
        trades=[trade],
        diagnostics=diagnostics,
        config=BacktestConfig(start_date="2024-01-01", end_date="2024-01-03", slippage_bps=0.0),
    )
    fails = [r for r in results if not r.passed]
    assert fails == [], [r.details for r in fails]
