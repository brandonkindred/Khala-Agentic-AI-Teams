"""Contract-level gates for trading-execution features that ship as schema in
issue #383 but whose engine support lands in later steps of #379.

Each gate raises ``UnsupportedOrderFeatureError`` (a subclass of
``NotImplementedError``) at submission time so a strategy that tries to use
a not-yet-supported feature fails loudly rather than producing a
silently-unfilled order or an IOC/FOK that behaves like GTC.

When a runtime step lands and removes the corresponding gate from
``validate_prices``, delete the matching test below.
"""

from __future__ import annotations

import pytest

from investment_team.trading_service.strategy.contract import (
    LimitAttachment,
    OrderRequest,
    OrderSide,
    OrderType,
    StopAttachment,
    TimeInForce,
    UnfilledPolicy,
    UnsupportedOrderFeatureError,
)


def _base(**overrides) -> OrderRequest:
    kwargs = {
        "client_order_id": "x",
        "symbol": "AAPL",
        "side": OrderSide.LONG,
        "qty": 1.0,
    }
    kwargs.update(overrides)
    return OrderRequest(**kwargs)


def test_trailing_stop_validates_post_step_8():
    """``TRAILING_STOP`` is runtime-supported as of #390. The blanket
    submission-time gate is gone; the remaining shape-consistency checks
    require ``stop_price`` (initial water mark) and ``trail_offset``."""
    _base(
        order_type=OrderType.TRAILING_STOP,
        stop_price=10.0,
        trail_offset=1.0,
    ).validate_prices()
    _base(
        order_type=OrderType.TRAILING_STOP,
        stop_price=10.0,
        trail_offset=20.0,
        trail_offset_kind="bps",
    ).validate_prices()
    # Missing required fields still rejected.
    with pytest.raises(ValueError, match="trail_offset"):
        _base(order_type=OrderType.TRAILING_STOP, stop_price=10.0).validate_prices()
    with pytest.raises(ValueError, match="stop_price"):
        _base(order_type=OrderType.TRAILING_STOP, trail_offset=1.0).validate_prices()
    with pytest.raises(ValueError, match="non-negative"):
        _base(
            order_type=OrderType.TRAILING_STOP,
            stop_price=10.0,
            trail_offset=-1.0,
        ).validate_prices()


def test_gates_raise_unsupported_order_feature_subclass():
    """The gates must raise the dedicated subclass, not bare
    ``NotImplementedError``, so streaming_harness only re-classifies real
    gate violations as ``unsupported_feature`` (and lets unrelated
    ``NotImplementedError`` from strategy code stay as ``runtime_error``)."""
    req = _base(parent_order_id="p-1")
    with pytest.raises(UnsupportedOrderFeatureError):
        req.validate_prices()


def test_attachments_validate_post_step_7():
    """Bracket attachments (``attached_stop_loss`` / ``attached_take_profit``)
    are runtime-supported as of #389; ``validate_prices`` must accept them
    without raising. (``parent_order_id`` / ``oco_group_id`` remain rejected
    on the strategy path — see ``test_parent_order_id_is_engine_internal``.)
    """
    _base(attached_stop_loss=StopAttachment(stop_price=95.0)).validate_prices()
    _base(attached_take_profit=LimitAttachment(limit_price=110.0)).validate_prices()
    _base(
        attached_stop_loss=StopAttachment(stop_price=95.0),
        attached_take_profit=LimitAttachment(limit_price=110.0),
    ).validate_prices()


def test_parent_order_id_is_engine_internal():
    """``parent_order_id`` is set by ``OrderBook.submit_attached`` when the
    engine materializes a bracket child; strategies must NOT supply it.
    ``submit_attached`` re-runs ``validate_prices`` on a clone with the
    field cleared, so this gate doesn't block the engine path while
    keeping strategy-side requests well-formed (otherwise a strategy
    that passed it would crash the run at ``OrderBook.submit``'s
    defense-in-depth ``ValueError`` rather than producing a structured
    ``unsupported_feature`` rejection)."""
    req = _base(parent_order_id="parent-123")
    with pytest.raises(UnsupportedOrderFeatureError, match="engine-internal"):
        req.validate_prices()


def test_oco_group_id_is_engine_internal():
    """Same as ``parent_order_id`` — set by ``OrderBook.submit_attached``."""
    req = _base(oco_group_id="oco-1")
    with pytest.raises(UnsupportedOrderFeatureError, match="engine-internal"):
        req.validate_prices()


def test_default_market_order_still_validates():
    """Sanity: the remaining gates only fire on still-deferred features."""
    _base().validate_prices()
    _base(order_type=OrderType.LIMIT, limit_price=100.0).validate_prices()
    _base(order_type=OrderType.STOP, stop_price=105.0).validate_prices()
    _base(tif=TimeInForce.GTC).validate_prices()
    # IOC/FOK are runtime-supported as of #388 (Step 6) — the blanket
    # submission-time gate is gone; only the shape-consistency check
    # ("IOC/FOK only valid with market or limit orders") remains live.
    _base(tif=TimeInForce.IOC).validate_prices()
    _base(tif=TimeInForce.FOK).validate_prices()
    _base(tif=TimeInForce.IOC, order_type=OrderType.LIMIT, limit_price=100.0).validate_prices()
    _base(tif=TimeInForce.FOK, order_type=OrderType.LIMIT, limit_price=100.0).validate_prices()


def test_unfilled_policies_validate_post_step_5():
    """All three ``unfilled_policy`` values are honored by the engine after
    #387 lands; ``validate_prices`` must accept them without raising."""
    _base(unfilled_policy=UnfilledPolicy.DROP).validate_prices()
    _base(unfilled_policy=UnfilledPolicy.REQUEUE_NEXT_BAR).validate_prices()
    _base(unfilled_policy=UnfilledPolicy.TWAP_N, twap_slices=2).validate_prices()
    _base(unfilled_policy=UnfilledPolicy.TWAP_N, twap_slices=10).validate_prices()


def test_twap_slices_shape_consistency_still_enforced():
    """The blanket gate is gone, but the shape-consistency checks at the
    bottom of ``validate_prices`` are now the active validators for
    TWAP_N's required-companion-fields invariant."""
    # TWAP_N requires twap_slices >= 2.
    with pytest.raises(ValueError, match="twap_n policy requires twap_slices >= 2"):
        _base(unfilled_policy=UnfilledPolicy.TWAP_N).validate_prices()
    with pytest.raises(ValueError, match="twap_n policy requires twap_slices >= 2"):
        _base(unfilled_policy=UnfilledPolicy.TWAP_N, twap_slices=1).validate_prices()
    # twap_slices is only valid when policy is TWAP_N.
    with pytest.raises(ValueError, match="twap_slices may only be set when"):
        _base(twap_slices=3).validate_prices()
    with pytest.raises(ValueError, match="twap_slices may only be set when"):
        _base(unfilled_policy=UnfilledPolicy.DROP, twap_slices=3).validate_prices()
