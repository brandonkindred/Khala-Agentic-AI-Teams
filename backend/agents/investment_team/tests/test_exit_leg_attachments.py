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
    _as_bracket_attachment_pair,
    _bracket_to_leg_specs,
    _validated_trail_offset,
    resolve_bracket_attachments,
    resolve_exit_leg_attachments,
)
from investment_team.trading_service.strategy.contract import (
    BPS_DIVISOR,
    ExitLegSpec,
    LimitAttachment,
    OrderSide,
    OrderType,
    StopAttachment,
)


def _leg(kind: OrderType = OrderType.STOP, pct: float = 0.03, **kwargs) -> ExitLegSpec:
    """Build an ExitLegSpec; defaults to a 3% STOP leg. Kind-coupled fields
    (e.g. limit_offset_pct for STOP_LIMIT) are passed through via kwargs."""
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


def test_single_stop_limit_leg_sets_limit_offset_short() -> None:
    """The short-side mirror of the LONG STOP_LIMIT case above: stop and
    limit_offset resolve identically (both are magnitudes derived from
    stop_price), only stop_price's side relative to ref_price flips."""
    [att] = resolve_exit_leg_attachments(
        [_leg(OrderType.STOP_LIMIT, pct=0.03, limit_offset_pct=0.01)], OrderSide.SHORT, 100.0
    )
    assert isinstance(att, StopAttachment)
    assert att.stop_price == pytest.approx(103.0)
    assert att.limit_offset == pytest.approx(1.03)
    assert att.limit_offset_kind == "abs"
    assert att.trail_offset is None


def test_single_trailing_stop_leg_sets_trail_offset() -> None:
    """A lone TRAILING_STOP leg resolves trail_offset as a "bps" (basis-point)
    value — pct * 10_000 — not an absolute distance, so it self-scales to
    whatever price fill_simulator actually applies it to (the entry fill,
    which may gap away from ref_price) instead of going stale; no limit
    offset. stop_price is still resolved as the nominal ref_price-anchored
    preview."""
    [att] = resolve_exit_leg_attachments(
        [_leg(OrderType.TRAILING_STOP, pct=0.03)], OrderSide.LONG, 100.0
    )
    assert isinstance(att, StopAttachment)
    assert att.stop_price == pytest.approx(97.0)
    assert att.trail_offset == pytest.approx(300.0)
    assert att.trail_offset_kind == "bps"
    assert att.limit_offset is None


def test_single_trailing_stop_leg_sets_trail_offset_short() -> None:
    """The short-side mirror of the long case above: stop sits above the
    reference; trail_offset is still a side-independent "bps" value."""
    [att] = resolve_exit_leg_attachments(
        [_leg(OrderType.TRAILING_STOP, pct=0.03)], OrderSide.SHORT, 100.0
    )
    assert isinstance(att, StopAttachment)
    assert att.stop_price == pytest.approx(103.0)
    assert att.trail_offset == pytest.approx(300.0)
    assert att.trail_offset_kind == "bps"
    assert att.limit_offset is None


def test_trailing_stop_leg_offset_scales_to_actual_entry_fill_price() -> None:
    """The whole point of "bps" mode: applying the resolved trail_offset to
    a fill price that gapped away from ref_price still yields the requested
    percentage distance, unlike a stale ref_price-anchored absolute offset
    (which could even go non-positive on a large gap). Mirrors the bps
    application formula documented on BPS_DIVISOR: entry_fill_price *
    (trail_offset / BPS_DIVISOR). This is a design-intent check on the
    resolver's own output, not a substitute for an integration-level test
    against the actual materializer, which belongs with that component's
    own test suite."""
    [att] = resolve_exit_leg_attachments(
        [_leg(OrderType.TRAILING_STOP, pct=0.5)], OrderSide.LONG, 100.0
    )
    gapped_entry_fill_price = 40.0
    offset = gapped_entry_fill_price * (att.trail_offset / BPS_DIVISOR)
    effective_stop = gapped_entry_fill_price - offset
    assert offset == pytest.approx(20.0)
    assert effective_stop == pytest.approx(20.0)
    assert effective_stop > 0


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
    assert trailing.trail_offset == pytest.approx(500.0)
    assert trailing.trail_offset_kind == "bps"

    assert isinstance(target, LimitAttachment)
    assert target.limit_price == pytest.approx(106.0)


def test_multiple_legs_resolve_in_order_short() -> None:
    """The short-side mirror of the long case above: same four-leg list and
    ordering, with every resolved price on the short side of the reference."""
    legs = [
        _leg(OrderType.STOP, pct=0.03),
        _leg(OrderType.STOP_LIMIT, pct=0.04, limit_offset_pct=0.01),
        _leg(OrderType.TRAILING_STOP, pct=0.05),
        _leg(OrderType.LIMIT, pct=0.06),
    ]
    attachments = resolve_exit_leg_attachments(legs, OrderSide.SHORT, 100.0)
    assert len(attachments) == 4
    stop, stop_limit, trailing, target = attachments

    assert isinstance(stop, StopAttachment)
    assert stop.stop_price == pytest.approx(103.0)
    assert stop.limit_offset is None
    assert stop.trail_offset is None

    assert isinstance(stop_limit, StopAttachment)
    assert stop_limit.stop_price == pytest.approx(104.0)
    assert stop_limit.limit_offset == pytest.approx(1.04)

    assert isinstance(trailing, StopAttachment)
    assert trailing.stop_price == pytest.approx(105.0)
    assert trailing.trail_offset == pytest.approx(500.0)
    assert trailing.trail_offset_kind == "bps"

    assert isinstance(target, LimitAttachment)
    assert target.limit_price == pytest.approx(94.0)


def test_multiple_legs_preserve_input_order_not_grouped_by_kind() -> None:
    """The two tests above only ever place every StopAttachment-producing leg
    before the single LimitAttachment-producing leg, so they can't tell
    order-preserving resolution apart from a hypothetical implementation
    that grouped output by attachment type. This interleaves two LIMIT legs
    around a STOP leg (and gives the LIMIT legs distinct percentages) to pin
    the "same order as the input legs" postcondition directly: any grouping
    or reordering implementation fails this test."""
    legs = [
        _leg(OrderType.LIMIT, pct=0.06),
        _leg(OrderType.STOP, pct=0.03),
        _leg(OrderType.LIMIT, pct=0.02),
    ]
    attachments = resolve_exit_leg_attachments(legs, OrderSide.LONG, 100.0)
    first, second, third = attachments
    assert isinstance(first, LimitAttachment)
    assert first.limit_price == pytest.approx(106.0)
    assert isinstance(second, StopAttachment)
    assert second.stop_price == pytest.approx(97.0)
    assert isinstance(third, LimitAttachment)
    assert third.limit_price == pytest.approx(102.0)


def test_multiple_legs_preserve_input_order_not_grouped_by_kind_short() -> None:
    """The short-side mirror of the interleaving test above."""
    legs = [
        _leg(OrderType.LIMIT, pct=0.06),
        _leg(OrderType.STOP, pct=0.03),
        _leg(OrderType.LIMIT, pct=0.02),
    ]
    attachments = resolve_exit_leg_attachments(legs, OrderSide.SHORT, 100.0)
    first, second, third = attachments
    assert isinstance(first, LimitAttachment)
    assert first.limit_price == pytest.approx(94.0)
    assert isinstance(second, StopAttachment)
    assert second.stop_price == pytest.approx(103.0)
    assert isinstance(third, LimitAttachment)
    assert third.limit_price == pytest.approx(98.0)


# ---------------------------------------------------------------------------
# resolve_exit_leg_attachments: defense-in-depth kind check
# ---------------------------------------------------------------------------


def test_resolver_rejects_unsupported_leg_kind() -> None:
    """The resolver's STOP-family branch explicitly rejects an unsupported
    kind rather than silently treating it as a stop leg by implication.
    ``ExitLegSpec``'s own validator already blocks this at construction, so
    the only way to reach this defense-in-depth check is to bypass it via
    ``model_construct`` (no validation) — exactly what a future caller
    outside this module's control might do."""
    leg = ExitLegSpec.model_construct(
        kind=OrderType.MARKET, pct=0.03, limit_offset_pct=None, note=""
    )
    with pytest.raises(ValueError, match="unsupported kind"):
        resolve_exit_leg_attachments([leg], OrderSide.LONG, 100.0)


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
    with pytest.raises(ValueError, match="non-finite/non-positive/not-off-reference limit_price"):
        resolve_exit_leg_attachments([_leg(OrderType.LIMIT, pct=0.9)], OrderSide.LONG, 1e308)


def test_rejects_overflowed_stop_price() -> None:
    """The same overflow risk applies to the STOP-family branch (a short's
    ref_price * (1 + pct))."""
    with pytest.raises(ValueError, match="non-finite/non-positive/not-off-reference stop_price"):
        resolve_exit_leg_attachments([_leg(OrderType.STOP, pct=0.9)], OrderSide.SHORT, 1e308)


def test_rejects_limit_price_rounded_to_reference() -> None:
    """A vanishingly small (but valid, pct > 0) percentage can round away
    entirely in float64, leaving limit_price == ref_price bit-for-bit — a
    "correct side of ref_price" violation the bare positivity check alone
    would miss, since the rounded price is still finite and positive."""
    with pytest.raises(ValueError, match="non-finite/non-positive/not-off-reference limit_price"):
        resolve_exit_leg_attachments([_leg(OrderType.LIMIT, pct=1e-20)], OrderSide.LONG, 100.0)


def test_rejects_stop_price_rounded_to_reference() -> None:
    """The same float-rounding risk applies to the STOP-family branch."""
    with pytest.raises(ValueError, match="non-finite/non-positive/not-off-reference stop_price"):
        resolve_exit_leg_attachments([_leg(OrderType.STOP, pct=1e-20)], OrderSide.LONG, 100.0)


def test_rejects_negligible_limit_offset() -> None:
    """A vanishingly small (but valid, limit_offset_pct > 0) STOP_LIMIT
    secondary offset resolves to a genuinely nonzero limit_offset, but one
    too small to survive being added to or subtracted from stop_price —
    ``protective_limit_price`` at materialization would compute a limit
    equal to the stop itself, losing the requested protective distance
    entirely, so the resolver must reject it rather than accept a
    "positive but negligible" offset."""
    with pytest.raises(ValueError, match="non-finite/non-positive/negligible limit_offset"):
        resolve_exit_leg_attachments(
            [_leg(OrderType.STOP_LIMIT, pct=0.03, limit_offset_pct=1e-20)], OrderSide.LONG, 100.0
        )


def test_accepts_limit_offset_that_only_rounds_away_in_the_unused_direction() -> None:
    """A limit_offset that rounds away under addition but survives under
    subtraction must NOT be rejected for a long leg, since materialization
    (``protective_limit_price``) only ever applies subtraction on the long
    side (``closing_long``) — checking both directions would wrongly reject
    a leg whose actual downstream arithmetic is fine. ref_price=2.0,
    pct=0.5 -> stop_price=1.0; limit_offset_pct=2**-53 -> limit_offset
    rounds 1.0 + limit_offset back to 1.0 but 1.0 - limit_offset stays a
    distinct 0.9999999999999999."""
    [att] = resolve_exit_leg_attachments(
        [_leg(OrderType.STOP_LIMIT, pct=0.5, limit_offset_pct=2**-53)], OrderSide.LONG, 2.0
    )
    assert isinstance(att, StopAttachment)
    assert att.stop_price == pytest.approx(1.0)
    # Exact equality, not pytest.approx: stop_price is exactly 1.0 and
    # 1.0 * 2**-53 is exactly representable, so this is the precise value
    # under test. pytest.approx's default tolerances (rel=1e-6, abs=1e-12)
    # both dwarf 2**-53 (~1.11e-16), so an approx comparison here couldn't
    # fail even for a zeroed limit_offset — exactly the regression this
    # file's test_rejects_negligible_limit_offset guards against.
    assert att.limit_offset == 2**-53


def test_rejects_stop_limit_derived_price_that_underflows_to_zero() -> None:
    """limit_offset can itself be finite, positive, and non-negligible
    relative to stop_price, while the *derived* protective limit price
    (stop_price - limit_offset, what materialization actually submits)
    still underflows to exactly 0.0 at subnormal-scale stop_price -- a
    failure the limit_offset-only checks can't see, since they never
    compute this combination."""
    with pytest.raises(ValueError, match="non-finite/non-positive/negligible limit_offset"):
        resolve_exit_leg_attachments(
            [_leg(OrderType.STOP_LIMIT, pct=0.5, limit_offset_pct=0.9)], OrderSide.LONG, 1e-323
        )


def test_rejects_stop_limit_derived_price_that_overflows() -> None:
    """The same gap in the other direction: limit_offset is finite and
    non-negligible, but stop_price + limit_offset (the short-side derived
    protective limit) overflows to inf."""
    with pytest.raises(ValueError, match="non-finite/non-positive/negligible limit_offset"):
        resolve_exit_leg_attachments(
            [_leg(OrderType.STOP_LIMIT, pct=0.1, limit_offset_pct=0.9)], OrderSide.SHORT, 1e308
        )


def test_trailing_stop_leg_valid_at_subnormal_ref_price() -> None:
    """Since trail_offset is now a "bps" value derived purely from pct (not
    from ref_price), a subnormal-scale ref_price that would have underflowed
    an absolute trail_offset to 0.0 no longer causes any trail_offset
    problem — only stop_price's own (unrelated) validity is at stake here,
    and it is fine at this magnitude."""
    [att] = resolve_exit_leg_attachments(
        [_leg(OrderType.TRAILING_STOP, pct=0.5)], OrderSide.SHORT, 5e-323
    )
    assert isinstance(att, StopAttachment)
    assert att.trail_offset == pytest.approx(5000.0)
    assert att.trail_offset_kind == "bps"


def test_rejects_trailing_stop_whose_bps_round_trip_vanishes_at_ref_price() -> None:
    """A vanishingly small (but Pydantic-valid, in (0, 1)) pct can survive
    stop_price's own validity check yet still produce a trail_offset whose
    round-trip application (price * (trail_offset / BPS_DIVISOR)) rounds
    back to exactly 0 relative to a typical price's ULP: at ref_price=0.1
    and pct=5.6e-17, stop_price=0.09999999999999999 (distinct from 0.1, so
    the bare stop_price check alone would pass this), but the bps offset
    the materializer would apply at that same price rounds 0.1 back to
    exactly 0.1 — the trailing child would start at (not off) the entry."""
    with pytest.raises(ValueError, match="materialization round-trip vanishes"):
        resolve_exit_leg_attachments(
            [_leg(OrderType.TRAILING_STOP, pct=5.6e-17)], OrderSide.LONG, 0.1
        )


def test_validated_trail_offset_rejects_preview_stop_that_underflows_to_zero() -> None:
    """The round-trip *offset* can itself be finite and positive while the
    *preview stop* it produces still underflows to exactly 0.0: at
    ref_price=5e-324 (the smallest positive subnormal) and pct=0.9,
    preview_offset rounds to 5e-324 too (equal to ref_price itself), so
    preview_offset > 0 and preview_stop != ref_price both hold, yet
    ref_price - preview_offset == 0.0 — a non-positive price that checking
    preview_offset alone can't see. Calls _validated_trail_offset directly
    (rather than through resolve_exit_leg_attachments) because at this same
    ref_price/pct, stop_price's own check (ref_price * (1 - pct) == 0.0)
    already rejects the leg first — this pins _validated_trail_offset's own
    postcondition in isolation, as a defense-in-depth guard independent of
    whatever stop_price happens to compute."""
    with pytest.raises(ValueError, match="materialization round-trip vanishes"):
        _validated_trail_offset(
            _leg(OrderType.TRAILING_STOP, pct=0.9), i=0, ref_price=5e-324, is_long=True
        )


def test_validated_trail_offset_rejects_preview_stop_that_overflows() -> None:
    """The short-side mirror of the underflow case above: at a ref_price
    near DBL_MAX, preview_offset is itself finite, but ref_price +
    preview_offset overflows to inf. Also calls _validated_trail_offset
    directly for the same reason as above."""
    with pytest.raises(ValueError, match="materialization round-trip vanishes"):
        _validated_trail_offset(
            _leg(OrderType.TRAILING_STOP, pct=0.9), i=0, ref_price=1.7e308, is_long=False
        )


# ---------------------------------------------------------------------------
# resolve_exit_leg_attachments: invalid side
# ---------------------------------------------------------------------------


def test_rejects_side_that_is_not_a_valid_order_side() -> None:
    """An unrecognized side must fail loudly rather than being silently
    treated as SHORT by the == comparison, which would invert every leg's
    placement without raising."""
    with pytest.raises(ValueError, match="side must be OrderSide.LONG or OrderSide.SHORT"):
        resolve_exit_leg_attachments([_leg()], "not-a-side", 100.0)


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


# ---------------------------------------------------------------------------
# _as_bracket_attachment_pair: defense-in-depth shape check
# ---------------------------------------------------------------------------


def test_as_bracket_attachment_pair_narrows_valid_list() -> None:
    """The happy path: a well-formed [StopAttachment, LimitAttachment] list
    narrows to the identical tuple."""
    stop = StopAttachment(stop_price=97.0)
    limit = LimitAttachment(limit_price=106.0)
    assert _as_bracket_attachment_pair([stop, limit]) == (stop, limit)


def test_as_bracket_attachment_pair_rejects_wrong_shape() -> None:
    """A list that isn't exactly (StopAttachment, LimitAttachment) — e.g. the
    order swapped — is rejected rather than silently trusted, since nothing
    in the generic list return type of resolve_exit_leg_attachments proves
    this shape; only _bracket_to_leg_specs's own leg ordering does."""
    stop = StopAttachment(stop_price=97.0)
    limit = LimitAttachment(limit_price=106.0)
    with pytest.raises(TypeError, match="must produce \\(StopAttachment, LimitAttachment\\)"):
        _as_bracket_attachment_pair([limit, stop])  # type: ignore[list-item]


def test_as_bracket_attachment_pair_rejects_wrong_length() -> None:
    """A wrong-length list is also rejected with TypeError (not the ValueError
    plain tuple/list unpacking would raise) — this function's contract is
    "wrong shape is always a TypeError", covering both wrong length and
    wrong element types with one exception type."""
    stop = StopAttachment(stop_price=97.0)
    with pytest.raises(TypeError, match="must produce exactly 2 attachments, got 1"):
        _as_bracket_attachment_pair([stop])
