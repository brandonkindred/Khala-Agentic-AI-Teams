"""Tests for the generalized exit-leg attachment API (issue #7494, step 1 of 3).

Covers :func:`resolve_exit_leg_attachments` — the rule-agnostic replacement
for the OCO-bracket-only price math previously inlined in
``resolve_bracket_attachments`` — with 0, 1, and multiple legs, plus
:class:`ExitLegSpec`'s own kind/offset-coupling validation. A final
equivalence test ties the generalized API back to the ``OcoBracketRule``
adapter (:func:`resolve_bracket_attachments` / :func:`_bracket_to_leg_specs`)
to prove the refactor produces byte-identical bracket behavior.
"""

from __future__ import annotations

import pytest

from investment_team.strategy_lab.spec_dsl import BracketStopLeg, BracketTakeProfitLeg, OcoBracketRule
from investment_team.trading_service.service import (
    _bracket_to_leg_specs,
    resolve_bracket_attachments,
    resolve_exit_leg_attachments,
)
from investment_team.trading_service.strategy.contract import (
    ExitLegSpec,
    LimitAttachment,
    OrderSide,
    OrderType,
    StopAttachment,
)


def _leg(kind: OrderType = OrderType.STOP, pct: float = 0.03, **kwargs) -> ExitLegSpec:
    return ExitLegSpec(kind=kind, pct=pct, **kwargs)


# ---------------------------------------------------------------------------
# resolve_exit_leg_attachments: 0 legs
# ---------------------------------------------------------------------------


def test_zero_legs_resolves_to_empty_list() -> None:
    """An empty leg list resolves to an empty attachment list (no-op case)."""
    assert resolve_exit_leg_attachments([], OrderSide.LONG, 100.0) == []


# ---------------------------------------------------------------------------
# resolve_exit_leg_attachments: 1 leg, each kind, both sides
# ---------------------------------------------------------------------------


def test_single_stop_leg_long() -> None:
    """A lone STOP leg resolves one StopAttachment below the reference for a long,
    with no limit or trail offset."""
    [att] = resolve_exit_leg_attachments([_leg(OrderType.STOP, pct=0.03)], OrderSide.LONG, 100.0)
    assert isinstance(att, StopAttachment)
    assert att.stop_price == pytest.approx(97.0)
    assert att.limit_offset is None
    assert att.trail_offset is None


def test_single_stop_leg_short() -> None:
    """A lone STOP leg resolves above the reference for a short."""
    [att] = resolve_exit_leg_attachments([_leg(OrderType.STOP, pct=0.03)], OrderSide.SHORT, 100.0)
    assert isinstance(att, StopAttachment)
    assert att.stop_price == pytest.approx(103.0)


def test_single_stop_limit_leg_sets_limit_offset() -> None:
    """A lone STOP_LIMIT leg resolves a limit_offset (absolute distance off the
    resolved stop level: limit_offset_pct * stop_price)."""
    [att] = resolve_exit_leg_attachments(
        [_leg(OrderType.STOP_LIMIT, pct=0.03, limit_offset_pct=0.01)], OrderSide.LONG, 100.0
    )
    assert isinstance(att, StopAttachment)
    assert att.stop_price == pytest.approx(97.0)
    assert att.limit_offset == pytest.approx(0.97)
    assert att.limit_offset_kind == "abs"
    assert att.trail_offset is None


def test_single_trailing_stop_leg_sets_trail_offset() -> None:
    """A lone TRAILING_STOP leg resolves a trail_offset (absolute distance off the
    resolved stop level: trail_offset_pct * stop_price), with no limit offset."""
    [att] = resolve_exit_leg_attachments(
        [_leg(OrderType.TRAILING_STOP, pct=0.03, trail_offset_pct=0.02)], OrderSide.LONG, 100.0
    )
    assert isinstance(att, StopAttachment)
    assert att.stop_price == pytest.approx(97.0)
    assert att.trail_offset == pytest.approx(1.94)
    assert att.trail_offset_kind == "abs"
    assert att.limit_offset is None


def test_single_limit_target_leg_long() -> None:
    """A lone LIMIT (target) leg resolves a LimitAttachment above the reference for
    a long."""
    [att] = resolve_exit_leg_attachments([_leg(OrderType.LIMIT, pct=0.06)], OrderSide.LONG, 100.0)
    assert isinstance(att, LimitAttachment)
    assert att.limit_price == pytest.approx(106.0)


def test_single_limit_target_leg_short() -> None:
    """A lone LIMIT (target) leg resolves below the reference for a short."""
    [att] = resolve_exit_leg_attachments([_leg(OrderType.LIMIT, pct=0.06)], OrderSide.SHORT, 100.0)
    assert isinstance(att, LimitAttachment)
    assert att.limit_price == pytest.approx(94.0)


# ---------------------------------------------------------------------------
# resolve_exit_leg_attachments: multiple legs
# ---------------------------------------------------------------------------


def test_multiple_legs_resolve_in_order() -> None:
    """A mixed list of all four leg kinds resolves to attachments of the matching
    type, in the same order as the input legs."""
    legs = [
        _leg(OrderType.STOP, pct=0.03),
        _leg(OrderType.STOP_LIMIT, pct=0.04, limit_offset_pct=0.01),
        _leg(OrderType.TRAILING_STOP, pct=0.05, trail_offset_pct=0.02),
        _leg(OrderType.LIMIT, pct=0.06),
    ]
    attachments = resolve_exit_leg_attachments(legs, OrderSide.LONG, 100.0)
    assert len(attachments) == 4
    stop, stop_limit, trailing, target = attachments

    assert isinstance(stop, StopAttachment)
    assert stop.stop_price == pytest.approx(97.0)
    assert stop.limit_offset is None
    assert stop.trail_offset is None

    assert isinstance(stop_limit, StopAttachment)
    assert stop_limit.stop_price == pytest.approx(96.0)
    assert stop_limit.limit_offset == pytest.approx(0.96)

    assert isinstance(trailing, StopAttachment)
    assert trailing.stop_price == pytest.approx(95.0)
    assert trailing.trail_offset == pytest.approx(1.9)

    assert isinstance(target, LimitAttachment)
    assert target.limit_price == pytest.approx(106.0)


# ---------------------------------------------------------------------------
# resolve_exit_leg_attachments: non-positive ref_price
# ---------------------------------------------------------------------------


def test_rejects_non_positive_ref_price() -> None:
    """The pure resolver enforces its ``ref_price > 0`` precondition with an
    explicit ``ValueError`` (active even under ``python -O``)."""
    with pytest.raises(ValueError, match="reference price must be positive"):
        resolve_exit_leg_attachments([_leg()], OrderSide.LONG, 0.0)


def test_empty_legs_with_non_positive_ref_price_still_raises() -> None:
    """The ``ref_price`` precondition is checked before iterating legs, so it is
    still enforced even when there is nothing to resolve."""
    with pytest.raises(ValueError, match="reference price must be positive"):
        resolve_exit_leg_attachments([], OrderSide.LONG, -5.0)


# ---------------------------------------------------------------------------
# ExitLegSpec: kind / secondary-offset coupling validation
# ---------------------------------------------------------------------------


def test_stop_limit_leg_requires_limit_offset_pct() -> None:
    """A STOP_LIMIT leg without limit_offset_pct is rejected."""
    with pytest.raises(ValueError, match="limit_offset_pct is required"):
        ExitLegSpec(kind=OrderType.STOP_LIMIT, pct=0.03)


def test_non_stop_limit_leg_rejects_limit_offset_pct() -> None:
    """A non-STOP_LIMIT leg carrying limit_offset_pct is rejected."""
    with pytest.raises(ValueError, match="limit_offset_pct is required"):
        ExitLegSpec(kind=OrderType.STOP, pct=0.03, limit_offset_pct=0.01)


def test_trailing_stop_leg_requires_trail_offset_pct() -> None:
    """A TRAILING_STOP leg without trail_offset_pct is rejected."""
    with pytest.raises(ValueError, match="trail_offset_pct is required"):
        ExitLegSpec(kind=OrderType.TRAILING_STOP, pct=0.03)


def test_non_trailing_stop_leg_rejects_trail_offset_pct() -> None:
    """A non-TRAILING_STOP leg carrying trail_offset_pct is rejected."""
    with pytest.raises(ValueError, match="trail_offset_pct is required"):
        ExitLegSpec(kind=OrderType.LIMIT, pct=0.06, trail_offset_pct=0.02)


def test_unsupported_kind_is_rejected() -> None:
    """A leg kind outside {STOP, STOP_LIMIT, TRAILING_STOP, LIMIT} (e.g. MARKET) is
    rejected."""
    with pytest.raises(ValueError, match="must be one of STOP"):
        ExitLegSpec(kind=OrderType.MARKET, pct=0.03)


@pytest.mark.parametrize("pct", [0.0, -0.1, 1.0, 1.5])
def test_leg_pct_must_be_in_open_unit_interval(pct: float) -> None:
    """``pct`` is bounded strictly in (0, 1), same as the bracket legs it
    generalizes."""
    with pytest.raises(ValueError):
        ExitLegSpec(kind=OrderType.STOP, pct=pct)


# ---------------------------------------------------------------------------
# Adapter equivalence: OcoBracketRule -> ExitLegSpec list ties back to
# resolve_bracket_attachments exactly.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("side", [OrderSide.LONG, OrderSide.SHORT])
def test_bracket_adapter_matches_generalized_resolution(side: OrderSide) -> None:
    """Resolving an OcoBracketRule through the old adapter and through the
    generalized API directly (via ``_bracket_to_leg_specs``) yields identical
    attachments — the refactor changes no bracket-path arithmetic."""
    bracket = OcoBracketRule(
        stop_loss=BracketStopLeg(pct=0.03, style="limit", limit_offset_pct=0.01),
        take_profit=BracketTakeProfitLeg(pct=0.06),
    )
    via_adapter = resolve_bracket_attachments(bracket, side, 100.0)
    via_generalized = resolve_exit_leg_attachments(_bracket_to_leg_specs(bracket), side, 100.0)

    assert via_adapter[0] == via_generalized[0]
    assert via_adapter[1] == via_generalized[1]
