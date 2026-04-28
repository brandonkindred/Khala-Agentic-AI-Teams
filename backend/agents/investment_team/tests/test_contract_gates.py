"""Contract-level gates for trading-execution features that ship as schema in
issue #383 but whose engine support lands in later steps of #379.

Each gate raises ``NotImplementedError`` at submission time so a strategy that
tries to use a not-yet-supported feature fails loudly rather than producing a
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


def test_trailing_stop_is_gated_until_step_8():
    req = _base(order_type=OrderType.TRAILING_STOP, stop_price=10.0)
    with pytest.raises(NotImplementedError, match="#390"):
        req.validate_prices()


def test_ioc_is_gated_until_step_6():
    req = _base(tif=TimeInForce.IOC)
    with pytest.raises(NotImplementedError, match="#388"):
        req.validate_prices()


def test_fok_is_gated_until_step_6():
    req = _base(tif=TimeInForce.FOK)
    with pytest.raises(NotImplementedError, match="#388"):
        req.validate_prices()


def test_unfilled_policy_drop_is_gated_until_step_3():
    req = _base(unfilled_policy=UnfilledPolicy.DROP)
    with pytest.raises(NotImplementedError, match="#385"):
        req.validate_prices()


def test_unfilled_policy_requeue_is_gated_until_step_4():
    req = _base(unfilled_policy=UnfilledPolicy.REQUEUE_NEXT_BAR)
    with pytest.raises(NotImplementedError, match="#386"):
        req.validate_prices()


def test_unfilled_policy_twap_is_gated_until_step_5():
    req = _base(unfilled_policy=UnfilledPolicy.TWAP_N, twap_slices=3)
    with pytest.raises(NotImplementedError, match="#387"):
        req.validate_prices()


def test_attached_stop_loss_is_gated_until_step_7():
    req = _base(attached_stop_loss=StopAttachment(stop_price=10.0))
    with pytest.raises(NotImplementedError, match="#389"):
        req.validate_prices()


def test_attached_take_profit_is_gated_until_step_7():
    req = _base(attached_take_profit=LimitAttachment(limit_price=120.0))
    with pytest.raises(NotImplementedError, match="#389"):
        req.validate_prices()


def test_parent_order_id_is_gated_until_step_7():
    req = _base(parent_order_id="parent-123")
    with pytest.raises(NotImplementedError, match="#389"):
        req.validate_prices()


def test_oco_group_id_is_gated_until_step_7():
    req = _base(oco_group_id="oco-1")
    with pytest.raises(NotImplementedError, match="#389"):
        req.validate_prices()


def test_default_market_order_still_validates():
    """Sanity: the gates only fire on the new feature flags."""
    _base().validate_prices()
    _base(order_type=OrderType.LIMIT, limit_price=100.0).validate_prices()
    _base(order_type=OrderType.STOP, stop_price=105.0).validate_prices()
    _base(tif=TimeInForce.GTC).validate_prices()
