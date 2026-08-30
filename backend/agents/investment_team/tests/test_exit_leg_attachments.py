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

from investment_team.strategy_lab.spec_dsl import (
    BracketStopLeg,
    BracketTakeProfitLeg,
    OcoBracketRule,
)
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
    """A lone TRAILING_STOP leg resolves a trail_offset derived from ref_price
    (ref_price * pct = 3.0), not from stop_price, so it matches the level
    fill_simulator actually seeds (entry_fill_price - trail_offset) when
    entry_fill_price == ref_price; no limit offset."""
    [att] = resolve_exit_leg_attachments(
        [_leg(OrderType.TRAILING_STOP, pct=0.03)], OrderSide.LONG, 100.0
    )
    assert isinstance(att, StopAttachment)
    assert att.stop_price == pytest.approx(97.0)
    assert att.trail_offset == pytest.approx(3.0)
    assert att.trail_offset_kind == "abs"
    assert att.limit_offset is None
    # The advertised stop_price matches entry_fill_price - trail_offset
    # (the level fill_simulator actually arms) when entry_fill_price == ref_price.
    assert att.stop_price == pytest.approx(100.0 - att.trail_offset)


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
        _leg(OrderType.TRAILING_STOP, pct=0.05),
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
    assert trailing.trail_offset == pytest.approx(5.0)

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


@pytest.mark.parametrize("ref_price", [float("nan"), float("inf"), float("-inf")])
def test_rejects_non_finite_ref_price(ref_price: float) -> None:
    """A non-finite ref_price (NaN/+inf/-inf) is rejected explicitly rather than
    silently slipping past a bare ``<= 0`` check (NaN's comparisons are always
    False) and propagating a NaN/inf price into the returned attachment."""
    with pytest.raises(ValueError, match="reference price must be positive"):
        resolve_exit_leg_attachments([_leg()], OrderSide.LONG, ref_price)


def test_rejects_overflowed_limit_price() -> None:
    """A finite but extreme ref_price can overflow a LIMIT leg's resolved price
    to inf (float64 overflow is silent, no exception) — the resolver must catch
    this itself since ``inf <= 0`` is False and would otherwise slip past a bare
    positivity check."""
    with pytest.raises(ValueError, match="non-finite/non-positive limit_price"):
        resolve_exit_leg_attachments([_leg(OrderType.LIMIT, pct=0.9)], OrderSide.LONG, 1e308)


def test_rejects_overflowed_stop_price() -> None:
    """The same overflow risk applies to the STOP-family branch (a short's
    ref_price * (1 + pct))."""
    with pytest.raises(ValueError, match="non-finite/non-positive stop_price"):
        resolve_exit_leg_attachments([_leg(OrderType.STOP, pct=0.9)], OrderSide.SHORT, 1e308)


# ---------------------------------------------------------------------------
# ExitLegSpec: kind / secondary-offset coupling validation
# ---------------------------------------------------------------------------


def test_stop_limit_leg_requires_limit_offset_pct() -> None:
    """A STOP_LIMIT leg without limit_offset_pct is rejected with a message
    distinct from the extraneous-field case below, so a caller can tell a
    missing required field from an unexpected one apart."""
    with pytest.raises(ValueError, match="STOP_LIMIT requires limit_offset_pct"):
        ExitLegSpec(kind=OrderType.STOP_LIMIT, pct=0.03)


def test_non_stop_limit_leg_rejects_limit_offset_pct() -> None:
    """A non-STOP_LIMIT leg carrying limit_offset_pct is rejected with a message
    distinct from the missing-field case above."""
    with pytest.raises(ValueError, match="limit_offset_pct is only valid"):
        ExitLegSpec(kind=OrderType.STOP, pct=0.03, limit_offset_pct=0.01)


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


def test_leg_is_frozen() -> None:
    """A constructed ExitLegSpec is immutable, matching its documented
    postcondition — mutating a field after construction (which would bypass
    ``_validate_kind_fields`` and could silently break the kind/offset
    coupling) is rejected."""
    leg = _leg(OrderType.STOP, pct=0.03)
    with pytest.raises(ValueError):
        leg.pct = 0.5


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

    # A single list-equality assertion (rather than indexing each side)
    # verifies both cardinality and element-wise equality together, so a
    # regression that drops/adds an attachment fails with a clear diff
    # instead of an IndexError. via_adapter is a tuple (its public
    # signature); list(...) normalizes it for comparison against
    # via_generalized's list — tuple != list in Python regardless of contents.
    assert list(via_adapter) == via_generalized
