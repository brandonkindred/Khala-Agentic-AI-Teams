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


def test_gates_raise_unsupported_order_feature_subclass():
    """The gates must raise the dedicated subclass, not bare
    ``NotImplementedError``, so streaming_harness only re-classifies real
    gate violations as ``unsupported_feature`` (and lets unrelated
    ``NotImplementedError`` from strategy code stay as ``runtime_error``)."""
    req = _base(tif=TimeInForce.IOC)
    with pytest.raises(UnsupportedOrderFeatureError):
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
