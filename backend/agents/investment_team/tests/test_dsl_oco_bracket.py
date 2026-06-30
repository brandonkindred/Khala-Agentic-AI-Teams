"""Tests for the ``oco_bracket`` exit-rule kind (OCO / bracket authoring).

Covers the new :class:`OcoBracketRule` ExitRule member end-to-end:

- DSL: discriminator dispatch, JSON round-trip, and leg validation.
- :class:`StrategySpec`: the bracket-exclusivity validator.
- :class:`_EngineEntryDispatcher`: ``maybe_emit`` attaches ``StopAttachment`` /
  ``LimitAttachment`` whose absolute prices are anchored at the signal-bar close
  (long & short, market & limit style).
- :func:`evaluate_exit_rules`: a bracket is skipped by the bar-by-bar exit
  dispatcher (no dual emission).
- Engine: a dispatcher-emitted bracketed entry materializes a resting OCO group;
  one leg filling cancels its sibling and the close is attributed
  ``engine_exit:bracket_*``.
"""

from __future__ import annotations

import pytest

from investment_team.execution.bar_safety import BarSafetyAssertion
from investment_team.execution.risk_filter import RiskFilter, RiskLimits
from investment_team.models import StrategySpec
from investment_team.strategy_lab.executor.predicate_evaluator import (
    BarRecord,
    StreamingHistoryView,
)
from investment_team.strategy_lab.executor.rule_compiler import (
    BarSnapshot,
    PositionState,
    evaluate_exit_rules,
)
from investment_team.strategy_lab.spec_dsl import (
    BracketStopLeg,
    BracketTakeProfitLeg,
    EntryRule,
    ExitRuleAdapter,
    FixedFractionSizing,
    OcoBracketRule,
    Predicate,
    is_bracket_exit,
    is_engine_handled_exit,
    is_full_position_exit,
)
from investment_team.trading_service.engine.execution_model import RealisticExecutionModel
from investment_team.trading_service.engine.fill_simulator import (
    FillSimulator,
    FillSimulatorConfig,
)
from investment_team.trading_service.engine.order_book import OrderBook
from investment_team.trading_service.engine.portfolio import Portfolio

# The pure bracket-attachment price math is unit-tested via the public
# ``resolve_bracket_attachments`` below. ``_EngineEntryDispatcher`` is private but
# is still imported to exercise the dispatcher *wiring* (that ``maybe_emit``
# attaches the resolved legs, and omits them when there is no bracket) and to
# drive the end-to-end engine tests at the bottom of this file.
from investment_team.trading_service.service import (
    TradingServiceResult,
    _EngineEntryDispatcher,
    resolve_bracket_attachments,
)
from investment_team.trading_service.strategy.contract import (
    Bar,
    OrderRequest,
    OrderSide,
    OrderType,
)

# ---------------------------------------------------------------------------
# DSL: discriminator dispatch, round-trip, classifiers
# ---------------------------------------------------------------------------


def _bracket(stop_pct: float = 0.03, tp_pct: float = 0.06, **stop_kwargs) -> OcoBracketRule:
    return OcoBracketRule(
        stop_loss=BracketStopLeg(pct=stop_pct, **stop_kwargs),
        take_profit=BracketTakeProfitLeg(pct=tp_pct),
    )


def test_oco_bracket_discriminator_dispatch() -> None:
    """A raw ``kind="oco_bracket"`` dict dispatches through ``ExitRuleAdapter`` to an
    ``OcoBracketRule`` with the legs parsed and ``style`` defaulting to ``market``."""
    raw = {"kind": "oco_bracket", "stop_loss": {"pct": 0.03}, "take_profit": {"pct": 0.06}}
    rule = ExitRuleAdapter.validate_python(raw)
    assert isinstance(rule, OcoBracketRule)
    assert rule.stop_loss.pct == pytest.approx(0.03)
    assert rule.take_profit.pct == pytest.approx(0.06)
    assert rule.stop_loss.style == "market"


def test_oco_bracket_json_round_trip() -> None:
    """A bracket survives a JSON dump/parse round-trip through ``ExitRuleAdapter``
    unchanged (including a limit-style stop leg)."""
    rule = _bracket(stop_pct=0.04, tp_pct=0.09, style="limit", limit_offset_pct=0.01)
    restored = ExitRuleAdapter.validate_json(rule.model_dump_json())
    assert isinstance(restored, OcoBracketRule)
    assert restored == rule


def test_oco_bracket_classifiers() -> None:
    """A bracket classifies as an engine-handled, full-position bracket; the
    ``is_bracket_exit`` predicate returns False for non-bracket exit kinds (no
    false positives that would mis-route them)."""
    rule = _bracket()
    assert is_bracket_exit(rule) is True
    assert is_full_position_exit(rule) is True
    assert is_engine_handled_exit(rule) is True
    # Negative cases: other exit kinds must NOT be classified as brackets.
    for raw in (
        {"kind": "stop_loss", "pct": 0.03},
        {"kind": "take_profit", "pct": 0.06},
        {"kind": "signal_exit", "when": {"lhs": "bar.close", "op": "<", "rhs": 90.0}},
    ):
        assert is_bracket_exit(ExitRuleAdapter.validate_python(raw)) is False


def test_bracket_stop_leg_limit_style_requires_offset() -> None:
    """A ``style="limit"`` stop leg without ``limit_offset_pct`` is rejected."""
    with pytest.raises(ValueError, match="requires limit_offset_pct"):
        BracketStopLeg(pct=0.03, style="limit")


def test_bracket_stop_leg_offset_requires_limit_style() -> None:
    """``limit_offset_pct`` set without ``style="limit"`` is rejected."""
    with pytest.raises(ValueError, match="only valid when style='limit'"):
        BracketStopLeg(pct=0.03, limit_offset_pct=0.01)


@pytest.mark.parametrize("pct", [0.0, -0.1, 1.0, 1.5])
def test_bracket_stop_leg_pct_must_be_in_open_unit_interval(pct: float) -> None:
    """The stop-leg ``pct`` must be strictly in (0, 1); 0, negatives, and >= 1.0 are
    rejected."""
    # ``pct`` must be strictly in (0, 1): gt=0 rejects 0 / negatives, lt=1.0
    # rejects 1.0 and above (a long's resolved level stays positive only for
    # pct < 1.0).
    with pytest.raises(ValueError):
        BracketStopLeg(pct=pct)


@pytest.mark.parametrize("pct", [0.0, -0.1, 1.0, 1.5])
def test_bracket_take_profit_leg_pct_must_be_in_open_unit_interval(pct: float) -> None:
    """The take-profit-leg ``pct`` is bounded the same way (strictly in (0, 1))."""
    # The take-profit leg is bounded the same way: a short's resolved target
    # ``ref * (1 - pct)`` is positive only for pct < 1.0.
    with pytest.raises(ValueError):
        BracketTakeProfitLeg(pct=pct)


def test_short_bracket_high_take_profit_yields_positive_limit() -> None:
    """A high (but < 1.0) take-profit still resolves a strictly-positive short-side
    limit price (50 off a reference of 100)."""
    _sl, tp = resolve_bracket_attachments(
        _bracket(stop_pct=0.03, tp_pct=0.5), OrderSide.SHORT, 100.0
    )
    assert tp.limit_price == pytest.approx(50.0)
    assert tp.limit_price > 0


def test_format_rule_renders_bracket() -> None:
    """A bracket renders to prose for prompts (market and limit-style). Exercised via
    the public ``format_rules_for_prompt`` (which delegates to the per-rule
    formatter) rather than the private ``_format_rule``."""
    from investment_team.strategy_lab.spec_dsl import format_rules_for_prompt

    assert format_rules_for_prompt([_bracket(stop_pct=0.03, tp_pct=0.06)]) == (
        "OCO bracket: stop 3% / target 6%"
    )
    limit = _bracket(stop_pct=0.03, tp_pct=0.06, style="limit", limit_offset_pct=0.01)
    assert (
        format_rules_for_prompt([limit]) == "OCO bracket: stop 3% (limit, 1% offset) / target 6%"
    )


def test_first_side_stop_factor_recognizes_bracket_stop() -> None:
    """A bracket's (entry-anchored) stop leg is recognized as an effective stop for
    both sides, so it suppresses the short-safety auto-stop injection."""
    from investment_team.strategy_lab.spec_dsl import first_side_stop_factor

    rules = [_bracket(stop_pct=0.04)]
    # The bracket stop is entry-anchored → caps both sides (suppresses the
    # short-safety auto-stop injection).
    assert first_side_stop_factor(rules, "short") == pytest.approx(0.04)
    assert first_side_stop_factor(rules, "long") == pytest.approx(0.04)


# ---------------------------------------------------------------------------
# StrategySpec: bracket exclusivity
# ---------------------------------------------------------------------------


def _spec(exit_rules: list) -> dict:
    return dict(
        strategy_id="s",
        authored_by="a",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=100.0))],
        exit_rules=exit_rules,
        sizing=FixedFractionSizing(fraction=0.02),
    )


def test_spec_bracket_only_is_valid() -> None:
    """A spec whose sole exit is a bracket constructs cleanly."""
    spec = StrategySpec(**_spec([_bracket()]))
    assert [r.kind for r in spec.exit_rules] == ["oco_bracket"]


def test_spec_bracket_with_signal_exit_is_valid() -> None:
    """A bracket may coexist with a ``signal_exit`` (the one allowed companion)."""
    from investment_team.strategy_lab.spec_dsl import SignalExitRule

    sig = SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=90.0))
    spec = StrategySpec(**_spec([_bracket(), sig]))
    assert {r.kind for r in spec.exit_rules} == {"oco_bracket", "signal_exit"}


@pytest.mark.parametrize(
    "conflicting",
    [
        {"kind": "stop_loss", "pct": 0.03},
        {"kind": "take_profit", "pct": 0.06},
        {"kind": "scaled_take_profit", "levels": [{"pct": 0.05, "qty_fraction": 1.0}]},
    ],
)
def test_spec_bracket_rejects_coexisting_price_exit(conflicting: dict) -> None:
    """A bracket alongside any other engine-handled price exit
    (stop_loss / take_profit / scaled_take_profit) is rejected."""
    with pytest.raises(ValueError, match="sole .*price exit"):
        StrategySpec(**_spec([_bracket(), conflicting]))


def test_spec_rejects_two_brackets() -> None:
    """At most one bracket is allowed per spec."""
    with pytest.raises(ValueError, match="at most one oco_bracket"):
        StrategySpec(**_spec([_bracket(), _bracket()]))


def test_spec_rejects_bracket_with_requires_custom_code() -> None:
    """A bracket combined with ``requires_custom_code=True`` is rejected at
    construction (the bracket would be inert on the custom-code path)."""
    # A bracket attaches only to engine-managed entries, so it is inert on the
    # custom-code path — reject the combination at construction.
    with pytest.raises(ValueError, match="not usable with requires_custom_code"):
        StrategySpec(requires_custom_code=True, **_spec([_bracket()]))


# ---------------------------------------------------------------------------
# Entry dispatcher: bracket → entry-order attachments
# ---------------------------------------------------------------------------


def _make_bar(symbol="AAA", close=100.0, timestamp="2024-01-10") -> Bar:
    # A real ``Bar`` (not a permissive mock) so an interface change to the fields
    # the dispatcher reads surfaces here instead of being silently absorbed.
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
    # A real ``Portfolio``: fresh, no positions, so ``mark_to_market()`` returns
    # ``capital`` and ``positions`` is an empty dict — exactly what the dispatcher
    # and ``_compute_qty`` consume, with no mocked interface to drift.
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


def _emit_with_bracket(side: str, bracket: OcoBracketRule, close: float = 100.0):
    rhs = 90.0 if side == "long" else 110.0
    op = ">" if side == "long" else "<"
    rules = [EntryRule(side=side, when=Predicate(lhs="bar.close", op=op, rhs=rhs))]
    dispatcher = _EngineEntryDispatcher(
        entry_rules=rules,
        sizing=FixedFractionSizing(fraction=0.02),
        exit_rules=[bracket],
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


def test_resolve_long_bracket_prices_off_ref() -> None:
    """For a long, the resolved stop sits below and the target above the reference
    (97 / 106 off 100); a market-style stop leg carries no limit offset."""
    sl, tp = resolve_bracket_attachments(
        _bracket(stop_pct=0.03, tp_pct=0.06), OrderSide.LONG, 100.0
    )
    assert sl.stop_price == pytest.approx(97.0)
    assert tp.limit_price == pytest.approx(106.0)
    # Market-style stop leg → no limit offset on the attachment.
    assert sl.limit_offset is None


def test_resolve_short_bracket_prices_off_ref() -> None:
    """For a short the signs flip: stop above and target below the reference
    (103 / 94 off 100)."""
    sl, tp = resolve_bracket_attachments(
        _bracket(stop_pct=0.03, tp_pct=0.06), OrderSide.SHORT, 100.0
    )
    assert sl.stop_price == pytest.approx(103.0)
    assert tp.limit_price == pytest.approx(94.0)


def test_resolve_limit_style_stop_offset() -> None:
    """A limit-style stop leg resolves a ``limit_offset`` (absolute distance off the
    stop level) so the engine materializes a STOP_LIMIT child."""
    sl, _tp = resolve_bracket_attachments(
        _bracket(stop_pct=0.03, style="limit", limit_offset_pct=0.01), OrderSide.LONG, 100.0
    )
    # limit_offset is an absolute distance: limit_offset_pct * stop_price.
    assert sl.limit_offset == pytest.approx(0.97)
    assert sl.limit_offset_kind == "abs"


def test_resolve_bracket_rejects_non_positive_ref_price() -> None:
    """The pure resolver enforces its ``ref_price > 0`` precondition with an
    explicit ``ValueError`` (active even under ``python -O``)."""
    with pytest.raises(ValueError, match="reference price must be positive"):
        resolve_bracket_attachments(_bracket(), OrderSide.LONG, 0.0)


def test_entry_without_bracket_has_no_attachments() -> None:
    """A spec with no bracket emits a plain entry order (no attachments)."""
    rules = [EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=90.0))]
    dispatcher = _EngineEntryDispatcher(
        entry_rules=rules,
        sizing=FixedFractionSizing(fraction=0.02),
        exit_rules=[],
    )
    pending: list[OrderRequest] = []
    dispatcher.maybe_emit(
        cur_bar=_make_bar(close=100.0),
        portfolio=_make_portfolio(),
        pending_for_prev=pending,
        views={"AAA": _build_view([100.0, 100.0])},
        result=TradingServiceResult(),
    )
    assert pending[0].attached_stop_loss is None
    assert pending[0].attached_take_profit is None


# ---------------------------------------------------------------------------
# Exit dispatcher: a bracket is NOT evaluated bar-by-bar (no dual emission)
# ---------------------------------------------------------------------------


def test_bracket_is_skipped_by_exit_evaluator() -> None:
    """The bar-by-bar exit evaluator emits no intent for a bracket (it is engine-
    attached), even on a bar that would trigger an equivalent stop_loss."""
    pos = PositionState(
        symbol="AAA",
        side="long",
        qty=100.0,
        entry_price=100.0,
        high_since_entry=100.0,
        low_since_entry=100.0,
    )
    # A bar that plunges well past the bracket's 3% stop level — a bar-by-bar
    # stop_loss WOULD fire here, but a bracket must not (its legs are engine-
    # attached resting orders, not dispatcher exits).
    bar = BarSnapshot(high=101.0, low=90.0, close=95.0)
    intents = evaluate_exit_rules([_bracket(stop_pct=0.03)], {"AAA": pos}, {"AAA": bar})
    assert intents == []


# ---------------------------------------------------------------------------
# End-to-end: dispatcher-emitted bracket materializes a resting OCO group
#
# These tests intentionally drive the FULL DSL path — dispatcher emits the entry
# with the attachments it computes, then the engine materializes them — to verify
# the exact attachments the dispatcher produces flow through to OCO materialization
# and OCO cancellation. The engine's materialization from an explicitly-constructed
# ``OrderRequest`` + ``StopAttachment`` / ``LimitAttachment`` is independently
# covered by ``tests/test_bracket_orders.py`` / ``test_bracket_stop_limit.py``.
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


def _dispatcher_bracket_entry(order_book):
    """Build a bracketed entry via the dispatcher and submit it to ``order_book``."""
    req = _emit_with_bracket("long", _bracket(stop_pct=0.05, tp_pct=0.10), close=100.0)
    assert req.order_type == OrderType.MARKET
    return order_book.submit(
        req,
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )


def test_end_to_end_take_profit_leg_fills_and_cancels_stop() -> None:
    """End-to-end: a dispatcher-emitted bracket materializes two OCO children; the
    take-profit LIMIT fills and cancels the stop sibling (engine_exit:bracket_tp)."""
    sim, order_book, portfolio = _make_simulator()
    parent = _dispatcher_bracket_entry(order_book)

    # Bar 2: entry fills, OCO children materialize (stop 95, target 110).
    sim.process_bar(_bar("2024-01-02", open_price=100.0))
    children = order_book.children_of(parent.order_id)
    assert len(children) == 2

    # Bar 3: high crosses the 110 target → resting LIMIT take-profit fills.
    outcome = sim.process_bar(
        _bar("2024-01-03", open_price=108.0, high=112.0, low=107.0, close=111.0)
    )
    assert len(outcome.closed_trades) == 1
    assert outcome.closed_trades[0].exit_reason == "engine_exit:bracket_tp"
    # OCO sibling (the stop) was cancelled.
    assert "AAA" not in portfolio.positions
    assert order_book.children_of(parent.order_id) == []


def test_end_to_end_stop_leg_fills_and_cancels_take_profit() -> None:
    """Mirror of the take-profit E2E: the STOP stop-loss fills and cancels the
    take-profit sibling (engine_exit:bracket_sl)."""
    sim, order_book, portfolio = _make_simulator()
    parent = _dispatcher_bracket_entry(order_book)

    sim.process_bar(_bar("2024-01-02", open_price=100.0))

    # Bar 3: low crosses the 95 stop → resting STOP stop-loss fills.
    outcome = sim.process_bar(_bar("2024-01-03", open_price=97.0, high=98.0, low=93.0, close=94.0))
    assert len(outcome.closed_trades) == 1
    assert outcome.closed_trades[0].exit_reason == "engine_exit:bracket_sl"
    assert "AAA" not in portfolio.positions
    assert order_book.children_of(parent.order_id) == []


def test_end_to_end_limit_style_stop_materializes_and_fills_stop_limit_child() -> None:
    """End-to-end (long): a limit-style bracket stop materializes a resting
    STOP_LIMIT child that fills at its limit and cancels the take-profit sibling."""
    # A limit-style bracket stop materializes as a resting STOP_LIMIT child (not a
    # plain STOP) and, when triggered and able to fill at its limit, closes the
    # position and cancels the take-profit sibling.
    sim, order_book, portfolio = _make_simulator()
    req = _emit_with_bracket(
        "long",
        _bracket(stop_pct=0.05, style="limit", limit_offset_pct=0.02, tp_pct=0.10),
        close=100.0,
    )
    parent = order_book.submit(
        req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )

    # Bar 2: entry fills; the limit-style stop leg materializes as a STOP_LIMIT
    # child at stop=95 with its limit on the protective side (95 - 0.02*95 = 93.1).
    sim.process_bar(_bar("2024-01-02", open_price=100.0))
    children = order_book.children_of(parent.order_id)
    sl = next(c for c in children if c.request.order_type == OrderType.STOP_LIMIT)
    assert sl.request.stop_price == pytest.approx(95.0)
    assert sl.request.limit_price == pytest.approx(93.1)

    # Bar 3: dips through the 95 stop while trading above the 93.1 limit → the
    # STOP_LIMIT fills at its limit and the take-profit sibling is cancelled.
    outcome = sim.process_bar(_bar("2024-01-03", open_price=96.0, high=96.0, low=93.0, close=94.0))
    assert len(outcome.closed_trades) == 1
    assert outcome.closed_trades[0].exit_reason == "engine_exit:bracket_sl"
    assert len(outcome.exit_fills) == 1
    assert outcome.exit_fills[0].price == pytest.approx(93.1)
    assert "AAA" not in portfolio.positions
    assert order_book.children_of(parent.order_id) == []


def test_end_to_end_short_limit_style_stop_fills_and_cancels_take_profit() -> None:
    """Short mirror of the limit-style bracket-stop E2E: verifies the short-side
    ``protective_limit_price`` sign convention. For a short, the stop sits ABOVE
    entry and the buy STOP_LIMIT child's protective limit sits ABOVE the stop; when
    triggered it fills at its limit, closes the short, and cancels the take-profit
    sibling."""
    sim, order_book, portfolio = _make_simulator()
    req = _emit_with_bracket(
        "short",
        _bracket(stop_pct=0.05, style="limit", limit_offset_pct=0.02, tp_pct=0.10),
        close=100.0,
    )
    parent = order_book.submit(
        req, submitted_at="2024-01-01", submitted_equity=10_000_000.0, expect_brackets=True
    )

    # Bar 2: short entry fills; the limit-style stop materializes as a BUY
    # STOP_LIMIT child at stop=105 (above entry) with its limit on the protective
    # side ABOVE the stop (105 + 0.02*105 = 107.1).
    sim.process_bar(_bar("2024-01-02", open_price=100.0))
    sl = next(
        c
        for c in order_book.children_of(parent.order_id)
        if c.request.order_type == OrderType.STOP_LIMIT
    )
    assert sl.request.side == OrderSide.LONG  # buy-to-cover closes the short
    assert sl.request.stop_price == pytest.approx(105.0)
    assert sl.request.limit_price == pytest.approx(107.1)

    # Bar 3: rises through the 105 stop while trading below the 107.1 limit → the
    # STOP_LIMIT fills at its limit and the take-profit sibling is cancelled.
    outcome = sim.process_bar(
        _bar("2024-01-03", open_price=104.0, high=108.0, low=104.0, close=106.0)
    )
    assert len(outcome.closed_trades) == 1
    assert outcome.closed_trades[0].exit_reason == "engine_exit:bracket_sl"
    assert len(outcome.exit_fills) == 1
    assert outcome.exit_fills[0].price == pytest.approx(107.1)
    assert "AAA" not in portfolio.positions
    assert order_book.children_of(parent.order_id) == []
