"""Strategy-facing contract.

Strategy-Lab-generated scripts run inside an isolated subprocess and receive
``Bar`` / ``Fill`` events delivered over stdin by :class:`StreamingHarness`.
They interact with the engine exclusively through :class:`StrategyContext`,
which is a narrow, backward-looking API:

* ``ctx.submit_order(...)`` — register intent; the engine owns the fill.
* ``ctx.cancel(order_id)`` — cancel a still-pending order.
* ``ctx.position(symbol)`` / ``ctx.capital`` / ``ctx.equity`` — current state.
* ``ctx.history(symbol, n)`` — last *n* bars the strategy has already received.
* ``ctx.now`` — timestamp of the currently-dispatching event.
* ``ctx.is_warmup`` — true during the live-mode warm-up pass (PR 2).

By construction the strategy process never holds a full market-data structure,
so "peeking" at future bars is structurally impossible — there is no accessor
for future data in this process at all.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ``runtime_window`` is a top-level module in the flat sandbox (copied in by
# ``StreamingHarness``, same as ``indicators``/``_streaming_indicators``), or
# the in-package module under tests / the shadow gate. Unlike the optional
# ``indicators`` module, every ``StrategyContext`` needs a retention bound, so
# this import is unconditional (not deferred into a method).
try:
    from runtime_window import STREAMING_WINDOW_BARS  # type: ignore[import-not-found]
except ImportError:
    from ...strategy_lab.runtime_window import STREAMING_WINDOW_BARS


class OrderSide(str, Enum):
    LONG = "long"
    SHORT = "short"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    TRAILING_STOP = "trailing_stop"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class UnfilledPolicy(str, Enum):
    """How the engine treats the unfilled remainder of a partially-filled order."""

    DROP = "drop"
    REQUEUE_NEXT_BAR = "requeue_next_bar"
    TWAP_N = "twap_n"


class FillKind(str, Enum):
    """Whether a Fill represents the full ordered qty, a partial slice, or a rejection."""

    FULL = "full"
    PARTIAL = "partial"
    REJECTED = "rejected"


class UnsupportedOrderFeatureError(NotImplementedError):
    """Raised by ``OrderRequest.validate_prices`` when a request asks for an
    order primitive whose runtime support has not yet shipped (gated by a
    later step of #379).

    A dedicated subclass keeps ``except NotImplementedError`` from
    misclassifying unrelated strategy bugs (e.g. ``raise NotImplementedError``
    placeholders inside ``on_bar``) as ``unsupported_feature`` failures.
    Catch this class — not bare ``NotImplementedError`` — when re-mapping
    gate violations to a structured error category.
    """


class InvalidTWAPOrderError(UnsupportedOrderFeatureError, ValueError):
    """Raised when a strategy emits a ``TWAP_N`` order with malformed
    ``twap_slices`` (missing, less than 2, or set without ``TWAP_N``).

    Inherits from ``UnsupportedOrderFeatureError`` so ``TradingService.run``
    surfaces it as a structured ``StrategyRuntimeError`` (matching how
    pre-#387 the blanket ``unfilled_policy`` gate raised this same
    superclass) — strategy bugs in TWAP parameters must NOT be silently
    dropped by the broad ``except Exception`` malformed-order handler,
    which would let typos in ``twap_slices`` quietly change trading
    behavior. Also subclasses ``ValueError`` so existing
    ``pytest.raises(ValueError, ...)`` assertions on the shape-
    consistency invariant continue to match.
    """


BPS_DIVISOR = 10_000.0
"""Basis-point scale for a ``"bps"``-kind :class:`StopAttachment` offset
(``trail_offset``/``limit_offset`` when their ``*_kind`` is ``"bps"``):
``value_bps / BPS_DIVISOR`` recovers the fraction a resolver such as
``resolve_exit_leg_attachments`` derived it from.
"""


def apply_bps_offset(base_price: float, offset_bps: float) -> float:
    """Convert a ``"bps"``-kind offset to an absolute distance anchored at ``base_price``.

    The single source of the ``base_price * (offset_bps / BPS_DIVISOR)``
    conversion, shared by every ``"bps"`` consumer of a
    :class:`StopAttachment` offset — ``trading_service.service``'s
    trailing-offset resolve-time preview and ``trading_service.engine.
    fill_simulator``'s bar-by-bar trailing ratchet, bps-mode stop-limit
    derivation, and entry-fill trailing seed — so the preview and the
    actual materialization can never independently drift from each other,
    the same role :func:`strategy_lab.spec_dsl.protective_limit_price`
    plays for the stop-limit sign convention.

    Preconditions: ``base_price`` and ``offset_bps`` are plain floats (no
    finiteness/sign constraint here — callers validate the result against
    their own contract, e.g. finite/positive/distinct-from-reference).
    Postconditions: returns ``base_price * (offset_bps / BPS_DIVISOR)``.
    """
    return base_price * (offset_bps / BPS_DIVISOR)


class StopAttachment(BaseModel):
    """Stop-loss leg attached to an entry order; materialized into an OCO child on entry fill.

    When ``limit_offset`` is set the materialized child is a STOP_LIMIT (a stop
    that, once triggered, rests as a limit order ``limit_offset`` away from the
    stop level) rather than a plain STOP. An *offset* (not an absolute limit
    price) is used because the stop level itself may trail/ratchet, so the limit
    is re-derived from the live stop. ``trail_offset`` and ``limit_offset`` are
    mutually exclusive — a ratcheting stop-limit child is not supported.
    """

    stop_price: float
    trail_offset: Optional[float] = None
    trail_offset_kind: Literal["abs", "bps"] = "abs"
    limit_offset: Optional[float] = None
    limit_offset_kind: Literal["abs", "bps"] = "abs"
    client_order_id: Optional[str] = None
    # When set, materialization re-derives ``stop_price`` from the parent
    # entry's ACTUAL fill price (``entry_fill_price * (1 ∓ entry_price_pct)``,
    # sign per the parent's side) instead of trusting this attachment's
    # ``stop_price`` preview verbatim. The preview is resolved at
    # entry-EMISSION time off the signal bar's close (see
    # ``resolve_exit_leg_attachments``'s ``ref_price``), which is only a
    # forecast of where the entry will actually fill — on a gap
    # (``fill_price != signal_close``) the preview and the true
    # entry-anchored level diverge. This matters specifically for a leg
    # whose trigger geometry is *also* independently recomputable from the
    # real fill price by another mechanism (e.g. the bar-close stop-loss
    # evaluator's ``rule_compiler.stop_loss_level``, which the
    # entry_price/market resting-stop-loss migration excludes from
    # evaluating this same rule while this resting order is selected for it
    # — see ``trading_service.service._resting_stop_loss_enabled``) — even
    # though the two mechanisms never act on the same trigger at once, they
    # must still agree on where the stop sits, since a run can select either
    # one. Unused (``None``) by every other leg kind/source, including
    # bracket legs, which have no such alternate evaluator to agree with.
    # Precondition: 0 < entry_price_pct < 1.0 (same bound as ExitLegSpec.pct /
    # _is_resting_stop_loss); enforced in OrderRequest.validate_prices.
    entry_price_pct: Optional[float] = None
    # When set, the fill-simulator materializer (``_materialize_attached_exit_children``)
    # uses this verbatim as the materialized child's ``reason`` instead of deriving
    # the generic ``engine_exit:exit_leg_{idx}`` label — same override-else-default
    # idiom as ``client_order_id`` above. Exists so a leg that carries semantic
    # meaning beyond "generic attached exit" (e.g. a resting stop-loss routed
    # through this rule-agnostic ``attached_exits`` plumbing rather than the
    # rule-aware fixed ``attached_stop_loss`` bracket field) can still preserve its
    # canonical ``engine_exit:<kind>`` attribution — several quality gates
    # (``alignment_checks``, ``exit_rule_conformance``) match that literal exactly.
    # Unused (``None``) by every other producer, including bracket legs (which
    # never go through ``attached_exits`` at all).
    reason: Optional[str] = None


class LimitAttachment(BaseModel):
    """Take-profit leg attached to an entry order; materialized into an OCO child on entry fill."""

    limit_price: float
    client_order_id: Optional[str] = None


class ExitLegSpec(BaseModel):
    """A single protective or target leg to attach to an entry order at fill time.

    Rule-agnostic input to ``resolve_exit_leg_attachments`` — decoupled from
    any specific DSL rule's field shape (e.g. an ``OcoBracketRule``'s
    ``BracketStopLeg``/``BracketTakeProfitLeg``), so any exit-rule kind can be
    translated into a list of these and resolved through one shared API.

    ``kind`` selects the resolved attachment shape: ``STOP``/``STOP_LIMIT``/
    ``TRAILING_STOP`` resolve to a :class:`StopAttachment`; ``LIMIT`` (a
    target leg) resolves to a :class:`LimitAttachment`. ``pct`` is the leg's
    distance off the entry reference price, as a positive fraction in
    ``(0, 1)`` (direction implied by side — the same convention as
    ``BracketStopLeg``/``BracketTakeProfitLeg``). For ``TRAILING_STOP``,
    ``pct`` is *also* the trailing distance, resolved as a ``"bps"``
    (basis-point) :class:`StopAttachment.trail_offset` rather than an
    absolute one — NOT from a second, independently-settable fraction:
    trailing-stop materialization seeds the live child's initial stop from
    the *actual* entry fill price rather than any separately-resolved
    ``stop_price``, and re-derives the offset from whatever price it is
    combined with each time it is applied (including on its bar-by-bar
    ratchet) — so a ``"bps"`` offset preserves the requested percentage
    distance regardless of where the entry actually fills (e.g. a gap),
    whereas a stale ``ref_price``-anchored absolute offset would not (and
    could even go non-positive on a large gap).
    ``limit_offset_pct`` is the ``STOP_LIMIT`` leg's secondary
    offset (a fraction of the resolved stop level, unaffected by the
    trailing case since ``limit_offset``/``trail_offset`` are mutually
    exclusive on :class:`StopAttachment`); required iff ``kind ==
    STOP_LIMIT``, the same coupling as ``BracketStopLeg._validate_limit_style``.
    ``note`` is a free-form, optional annotation for callers/maintainers
    (e.g. which DSL rule this leg was translated from); it plays no part in
    resolution or validation and is not carried onto the resolved
    ``StopAttachment``/``LimitAttachment``.

    Preconditions: ``pct`` in ``(0, 1)``; ``kind`` in ``{STOP, STOP_LIMIT,
    TRAILING_STOP, LIMIT}``; ``limit_offset_pct`` set iff ``kind ==
    STOP_LIMIT``.
    Postconditions: a validated, immutable leg spec ready for
    ``resolve_exit_leg_attachments``.
    """

    # Frozen so the "validated, immutable leg spec" postcondition below is
    # actually enforced — without this a caller could mutate ``kind`` or
    # ``limit_offset_pct`` after construction and silently break the
    # kind/offset coupling ``_validate_kind_fields`` exists to guarantee
    # (Pydantic validators run on construction, not on attribute assignment).
    # ``extra="forbid"`` (Pydantic's default is "ignore") so a misspelled or
    # unexpected keyword (e.g. ``limit_offset`` instead of
    # ``limit_offset_pct``) raises at construction instead of being silently
    # dropped and surfacing later as a confusing "requires limit_offset_pct"
    # error from ``_validate_kind_fields``.
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: OrderType
    pct: float = Field(gt=0, lt=1.0)
    limit_offset_pct: Optional[float] = Field(default=None, gt=0, lt=1.0)
    note: str = ""

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> "ExitLegSpec":
        """Tie ``limit_offset_pct`` to ``kind``.

        Preconditions: ``kind`` is a valid ``OrderType`` (Pydantic-enforced).
        Postconditions: returns ``self`` when consistent; raises
        ``ValueError`` when ``kind`` is not a supported leg kind, when
        ``kind == STOP_LIMIT`` and ``limit_offset_pct`` is missing, or when
        ``limit_offset_pct`` is set on a non-``STOP_LIMIT`` leg — the same
        coupling, and the same two distinct messages for the missing-vs-
        extraneous cases, as ``BracketStopLeg._validate_limit_style``.
        """
        if self.kind not in (
            OrderType.STOP,
            OrderType.STOP_LIMIT,
            OrderType.TRAILING_STOP,
            OrderType.LIMIT,
        ):
            raise ValueError(
                f"ExitLegSpec.kind must be one of STOP/STOP_LIMIT/TRAILING_STOP/LIMIT, got {self.kind!r}"
            )
        if self.kind == OrderType.STOP_LIMIT:
            if self.limit_offset_pct is None:
                raise ValueError("ExitLegSpec.kind=STOP_LIMIT requires limit_offset_pct")
        elif self.limit_offset_pct is not None:
            raise ValueError("ExitLegSpec.limit_offset_pct is only valid when kind == STOP_LIMIT")
        return self


class Bar(BaseModel):
    """One candle delivered to the strategy. Timeframe-agnostic.

    ``timestamp`` is ISO-8601. ``timeframe`` labels the candle duration
    (``"1d"``, ``"1m"``, ``"15m"``, …) so resampled candles remain
    self-describing.
    """

    symbol: str
    timestamp: str
    timeframe: str = "1d"
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class OrderRequest(BaseModel):
    """Intent emitted by the strategy. The engine assigns the final ``order_id``."""

    client_order_id: str  # strategy-side ID, opaque to engine
    symbol: str
    side: OrderSide
    qty: float = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    # Trailing-stop ratchet specifier (#390). Only consulted when
    # ``order_type == TRAILING_STOP`` (standalone) or when this request was
    # cloned from a ``StopAttachment`` with ``trail_offset`` set (bracket
    # child). ``stop_price`` is the initial water mark for standalone
    # trailing stops; the engine ratchets from there.
    trail_offset: Optional[float] = None
    trail_offset_kind: Literal["abs", "bps"] = "abs"
    tif: TimeInForce = TimeInForce.DAY
    reason: str = ""  # free-form annotation; surfaced in logs / fills
    # True when the engine entry dispatcher already clamped this order to
    # ``max_position_pct`` at the sizing price. ``RiskFilter.can_enter`` then
    # skips its position-cap check for this order (the dispatcher owns it),
    # avoiding a false rejection when the fill price gaps above the sizing
    # price. Custom-code orders submitted by the strategy subprocess leave this
    # False, so the gate remains their sole position-cap enforcement point.
    risk_presized: bool = False
    # True ONLY on the engine's scaled-take-profit rung scale-outs — a PARTIAL
    # close that leaves the runner open. The per-bar exit gate reads this flag to
    # tell an in-flight partial from a full close WITHOUT parsing the order's
    # free-form ``reason`` string: a partial must not stand the whole bar down (a
    # stop / take-profit / signal exit may still need to protect the runner), it
    # only defers the next rung. Set by the engine's ``_build_close_order``; every
    # other order — strategy orders and full-position engine closes — leaves it
    # False, so this is a structural discriminator decoupled from reason formatting.
    engine_scaled_partial: bool = False
    unfilled_policy: Optional[UnfilledPolicy] = None
    twap_slices: Optional[int] = None
    attached_stop_loss: Optional[StopAttachment] = None
    attached_take_profit: Optional[LimitAttachment] = None
    # Additional resting protective/target legs beyond the two fixed bracket
    # fields above, for entries with more than a stop-loss/take-profit pair
    # (e.g. multiple independently-attached, non-bracket exits). Kept as a
    # separate field rather than folding the bracket pair into it so existing
    # bracket call sites/tests constructing requests via
    # ``attached_stop_loss=``/``attached_take_profit=`` are unaffected.
    # Populated in production by ``_EngineEntryDispatcher`` (in
    # ``trading_service/service.py``) for a resting-eligible ``StopLossRule``
    # (``basis="entry_price"``, ``style="market"``, ``0 < pct < 1.0`` — the
    # exact predicate is the module-level ``_is_resting_stop_loss``, not a
    # method of the dispatcher) when the run's feature check
    # (``_resting_stop_loss_enabled``, off by default) selects this
    # mechanism; other rule kinds/bases are not yet migrated onto this path
    # and remain bar-close-only regardless. When this mechanism IS selected
    # for a rule, the bar-close evaluator excludes that exact rule from its
    # own evaluation at the ``rule_compiler._filtered_intent_for_rule``
    # chokepoint — the same one ``OcoBracketRule`` is unconditionally
    # skipped at — so the two mechanisms are mutually exclusive per rule; see
    # ``StopLossRule.style``'s docstring for the full contract and
    # ``StopAttachment.entry_price_pct`` for why the two must still agree on
    # price even though only one ever fires.
    attached_exits: List[Union[StopAttachment, LimitAttachment]] = Field(default_factory=list)
    parent_order_id: Optional[str] = None
    oco_group_id: Optional[str] = None

    @property
    def has_attached_exits(self) -> bool:
        """True iff this request carries any protective/target exit leg.

        Covers both the two fixed bracket fields (``attached_stop_loss`` /
        ``attached_take_profit``) and the generalized ``attached_exits``
        list, so callers have one predicate instead of duplicating the
        three-way OR at each materialization/validation call site.
        """
        return (
            self.attached_stop_loss is not None
            or self.attached_take_profit is not None
            or bool(self.attached_exits)
        )

    def validate_prices(self) -> None:
        """Enforce order_type / tif / policy / attachment constraints.

        Runtime-support gates run **before** the shape-consistency checks so
        a strategy that asks for an un-implemented feature gets the explicit
        ``NotImplementedError`` (which propagates as a structured
        ``unsupported_feature`` failure), not a generic ``ValueError`` that
        the broad ``except`` in ``TradingService`` would silently log-and-drop.
        """
        # Runtime-support gates. The schema fields below land in #383 so
        # callers and Pydantic models compile, but the execution engine
        # honors them only as their respective steps of #379 ship. Until
        # those steps land, fail loudly at submission time rather than
        # silently producing never-filled orders.
        #
        # Trailing-stop runtime support landed with #390. The
        # standalone ``TRAILING_STOP`` order type and bracketed
        # ``attached_stop_loss.trail_offset`` are validated by the
        # shape-consistency checks below (``stop_price`` required for
        # standalone TRAILING_STOP; non-negative ``trail_offset``).
        # Applies to every ``StopAttachment`` leg — the fixed
        # ``attached_stop_loss`` field and each ``StopAttachment`` in the
        # generalized ``attached_exits`` list — so a leg attached
        # via the list can't skip the offset checks the fixed field
        # enforces. ``label`` names the offending leg in the error so a
        # bad ``attached_exits`` entry is as easy to locate as a bad
        # ``attached_stop_loss``.
        stop_legs: List[Tuple[str, StopAttachment]] = []
        if self.attached_stop_loss is not None:
            stop_legs.append(("attached_stop_loss", self.attached_stop_loss))
        for idx, leg in enumerate(self.attached_exits):
            if isinstance(leg, StopAttachment):
                stop_legs.append((f"attached_exits[{idx}]", leg))
        for label, sl in stop_legs:
            if sl.trail_offset is not None and sl.trail_offset < 0:
                raise ValueError(
                    f"{label}.trail_offset must be non-negative, got {sl.trail_offset!r}"
                )
            if sl.limit_offset is not None:
                if sl.limit_offset < 0:
                    raise ValueError(
                        f"{label}.limit_offset must be non-negative, got {sl.limit_offset!r}"
                    )
                # A ratcheting stop-limit child (trailing stop whose limit re-derives
                # from the moving stop each bar) is out of scope; reject the combo
                # loudly so it surfaces at submission rather than materializing a
                # silently-wrong child.
                if sl.trail_offset is not None:
                    raise ValueError(
                        f"{label} cannot set both trail_offset and limit_offset "
                        "(a trailing stop-limit child is not supported)"
                    )
            if sl.entry_price_pct is not None and not (0.0 < sl.entry_price_pct < 1.0):
                raise ValueError(
                    f"{label}.entry_price_pct must satisfy 0 < entry_price_pct < 1.0, "
                    f"got {sl.entry_price_pct!r}"
                )
        # ``parent_order_id`` / ``oco_group_id`` are engine-internal: the
        # bracket materializer in ``FillSimulator`` calls
        # ``OrderBook.submit_attached`` which clones the request with these
        # fields cleared before re-running ``validate_prices``, so this
        # gate doesn't block the engine path. It DOES block strategy code
        # that tries to set them via ``StrategyContext.submit_order`` — a
        # bracket child must be created via the engine, not by the strategy
        # itself, so trapping here keeps the request stream well-formed
        # and routes a programming error through the structured
        # ``unsupported_feature`` rejection rather than crashing the run
        # at ``OrderBook.submit``'s defense-in-depth ``ValueError``.
        if self.parent_order_id is not None:
            raise UnsupportedOrderFeatureError(
                "parent_order_id is engine-internal; bracket children are "
                "created by the engine via OrderBook.submit_attached. Strategies "
                "must leave parent_order_id unset and rely on attached_stop_loss "
                "/ attached_take_profit instead."
            )
        if self.oco_group_id is not None:
            raise UnsupportedOrderFeatureError(
                "oco_group_id is engine-internal; bracket children are "
                "created by the engine via OrderBook.submit_attached. Strategies "
                "must leave oco_group_id unset and rely on attached_stop_loss "
                "/ attached_take_profit instead."
            )
        # Shape-consistency checks. Most are currently unreachable because
        # the gates above fire first, but they remain in place so that when
        # each gate is lifted by its corresponding step, the consistency
        # invariant becomes the live check (e.g. when #390 lifts the
        # trailing-stop gate, the "trailing_stop requires stop_price" check
        # below becomes the active validator).
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit order requires limit_price")
        if self.order_type == OrderType.STOP and self.stop_price is None:
            raise ValueError("stop order requires stop_price")
        if self.order_type == OrderType.STOP_LIMIT:
            if self.stop_price is None or self.limit_price is None:
                raise ValueError("stop_limit order requires both stop_price and limit_price")
            # Limit must sit on the protective side of the stop. A SHORT
            # stop-limit (sell — closes a long, triggers on a fall) submits a
            # sell-limit that must not be above the trigger: ``limit <= stop``.
            # A LONG stop-limit (buy — closes a short, triggers on a rise)
            # submits a buy-limit that must not be below the trigger:
            # ``limit >= stop``. A limit on the wrong side could never fill at
            # trigger and is almost certainly a sign error.
            if self.side == OrderSide.SHORT and self.limit_price > self.stop_price:
                raise ValueError(
                    "short stop_limit requires limit_price <= stop_price "
                    f"(got limit={self.limit_price!r}, stop={self.stop_price!r})"
                )
            if self.side == OrderSide.LONG and self.limit_price < self.stop_price:
                raise ValueError(
                    "long stop_limit requires limit_price >= stop_price "
                    f"(got limit={self.limit_price!r}, stop={self.stop_price!r})"
                )
        if self.order_type == OrderType.TRAILING_STOP:
            if self.stop_price is None:
                raise ValueError("trailing_stop order requires stop_price")
            if self.trail_offset is None:
                raise ValueError("trailing_stop order requires trail_offset")
            if self.trail_offset < 0:
                raise ValueError(
                    f"trailing_stop order trail_offset must be non-negative, "
                    f"got {self.trail_offset!r}"
                )
        if self.tif in (TimeInForce.IOC, TimeInForce.FOK) and self.order_type not in (
            OrderType.MARKET,
            OrderType.LIMIT,
        ):
            raise ValueError(f"{self.tif.value} only valid with market or limit orders")
        if self.unfilled_policy == UnfilledPolicy.TWAP_N:
            if self.twap_slices is None or self.twap_slices < 2:
                raise InvalidTWAPOrderError("twap_n policy requires twap_slices >= 2")
        elif self.twap_slices is not None:
            raise InvalidTWAPOrderError(
                "twap_slices may only be set when unfilled_policy is twap_n"
            )


class Fill(BaseModel):
    """Engine → strategy notification that a submitted order has filled."""

    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    qty: float
    price: float  # post-slippage fill price
    timestamp: str
    reason: str = ""
    # Partial-fill annotations populated by the realistic execution path in
    # #386 (Trading 5/5 Step 4). Default ``None`` means "engine has not
    # annotated this fill" — which is more honest than claiming
    # ``FillKind.FULL`` / ``unfilled_qty=0`` for fills the engine actually
    # clipped at the participation cap. Step 4 will start populating real
    # values; until then strategies should treat ``None`` as "unknown".
    fill_kind: Optional[FillKind] = None
    unfilled_qty: Optional[float] = None
    cumulative_filled_qty: Optional[float] = None


class CancelRequest(BaseModel):
    """Request from strategy to cancel a still-pending order."""

    order_id: str


# ---------------------------------------------------------------------------
# StrategyContext — used inside the strategy subprocess.
#
# The parent-process engine has its own state; the context below is the *view*
# the strategy is allowed to see, and its mutators (submit_order / cancel) are
# serialized to stdout so the parent engine can process them.
# ---------------------------------------------------------------------------


class _PositionSnapshot(BaseModel):
    symbol: str
    side: OrderSide
    qty: float
    entry_price: float
    entry_timestamp: str

    @property
    def quantity(self) -> float:
        """Read-only alias for :attr:`qty`.

        Strategy authors (and LLM-generated ``on_bar`` code) routinely reach for
        the natural name ``position.quantity``; without this alias that read
        raises ``AttributeError`` at runtime and aborts the whole backtest. The
        alias makes the natural name a faithful synonym for the canonical
        ``qty`` field so an otherwise-correct strategy never crashes on it.

        Postconditions:
          - Returns exactly ``self.qty`` (same sign and magnitude); read-only —
            there is no setter, so ``qty`` remains the single source of truth.
        """
        return self.qty


class StrategyContext:
    """Narrow, backward-looking API exposed to strategy code.

    This class is instantiated by the child-side harness and mutated as events
    arrive. It never receives a full market-data frame — ``history()`` only
    returns bars that have already been delivered via ``on_bar``.
    """

    # Sentinel class used so the harness can type-check context without
    # importing anything from the parent process.
    #
    # NOTE: this object lives in the *strategy* subprocess. Its submit_order /
    # cancel implementations write protocol lines to the child's stdout. The
    # parent engine reads those lines, applies its authoritative state, and
    # echoes fills back as FillEvents.

    def __init__(self, *, emit) -> None:
        # ``emit`` is an injection point (callable taking a dict) so the same
        # class can be driven by the real stdout-backed harness in production
        # and by a synchronous in-process driver in unit tests. Under the
        # chunked protocol (issue #377), the harness substitutes a tagging
        # wrapper so emitted ``order`` / ``cancel`` records get a
        # harness-managed ``bar_index`` injected without any strategy-
        # mutable attribute being involved (PR #425 review defense).
        self._emit = emit
        self._history: Dict[str, List[Bar]] = {}
        # Symbol of the bar currently being dispatched to ``on_bar`` — the
        # default subject of ``ctx.indicator(...)`` when no symbol is given.
        self._current_symbol: Optional[str] = None
        self._positions: Dict[str, _PositionSnapshot] = {}
        self._capital: float = 0.0
        self._equity: float = 0.0
        self._now: str = ""
        self._is_warmup: bool = False
        self._next_client_order_id: int = 0
        # indicator() shares one IndicatorRegistry per (symbol, source) across
        # this instance's calls for performance (see
        # strategy_indicators._shared_registry). Owned here — not a
        # module/thread-level cache — so this execution's indicator state is
        # never visible to any other StrategyContext, including one for the
        # same symbol constructed on the same thread whose bar ingestion
        # happens to interleave with this one's rather than running to
        # completion first (a thread-local cache can't tell those apart; a
        # fresh dict per instance doesn't need to).
        self._indicator_registries: dict = {}
        # This dict only covers indicator() calls. Generated strategy code
        # may instead call the 16 standalone wrapper functions directly
        # (`from indicators import sma`, a documented, supported call shape
        # — see strategy_indicators' module docstring), which never see this
        # dict directly either. The harness instead sets strategy_indicators'
        # _active_registries contextvar to this dict right after
        # constructing ctx (see streaming_harness.py's _HARNESS_SCRIPT), so a
        # standalone wrapper call resolves to it too — see
        # _shared_registry's docstring for the mechanism.

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

    @property
    def capital(self) -> float:
        return self._capital

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def now(self) -> str:
        return self._now

    @property
    def is_warmup(self) -> bool:
        return self._is_warmup

    def position(self, symbol: str) -> Optional[_PositionSnapshot]:
        return self._positions.get(symbol)

    def history(self, symbol: str, n: int) -> List[Bar]:
        bars = self._history.get(symbol, [])
        if n <= 0:
            return []
        return bars[-n:]

    def indicator(
        self,
        name: str,
        *,
        symbol: Optional[str] = None,
        source: str = "close",
        **params,
    ) -> Optional[float]:
        """Latest value of indicator ``name`` for ``symbol`` (current bar's symbol by default).

        Reads the indicator straight off the bars already delivered to this
        strategy, computed by the same engine indicator math (the shared scalar
        ``indicators`` module), so the value matches what the engine sees on the
        current bar. This is the prescribed way to read indicators in generated
        strategies — do not import ``indicators`` or recompute inline.

        Preconditions:
            ``name`` is a known DSL indicator and ``params`` satisfy it
            (``sma``/``ema`` require ``period``); a bar has been dispatched (so a
            default ``symbol`` exists) unless ``symbol`` is passed explicitly.
            Contract violations raise ``ValueError`` — never silently coerced.
        Postconditions:
            Returns the latest indicator value as ``float``, or ``None`` during
            warm-up / when no bars for ``symbol`` have arrived yet.
        """
        sym = symbol if symbol is not None else self._current_symbol
        if sym is None:
            raise ValueError("indicator() needs a symbol when no bar has been dispatched yet")
        history = self._history.get(sym, [])
        if not history:
            return None
        # ``indicators`` is the scalar API: a top-level module in the flat
        # sandbox (copied in by the harness), or the in-package module under
        # tests / the shadow gate.
        try:
            from indicators import indicator_value  # type: ignore[import-not-found]
        except ImportError:
            from ...strategy_lab.executor.strategy_indicators import indicator_value
        return indicator_value(
            name, history, source=source, registries=self._indicator_registries, **params
        )

    # ------------------------------------------------------------------
    # Mutators — produce OrderRequest / CancelRequest records that the
    # harness serialises to the parent engine.
    # ------------------------------------------------------------------

    def submit_order(
        self,
        *,
        symbol: str,
        side: OrderSide | str,
        qty: float,
        order_type: OrderType | str = OrderType.MARKET,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        trail_offset: Optional[float] = None,
        trail_offset_kind: Literal["abs", "bps"] = "abs",
        tif: TimeInForce | str = TimeInForce.DAY,
        reason: str = "",
        unfilled_policy: Optional[UnfilledPolicy | str] = None,
        twap_slices: Optional[int] = None,
        attached_stop_loss: Optional[StopAttachment] = None,
        attached_take_profit: Optional[LimitAttachment] = None,
        parent_order_id: Optional[str] = None,
        oco_group_id: Optional[str] = None,
    ) -> str:
        """Submit an order. Returns the strategy-side ``client_order_id``.

        The trailing keyword arguments belong to the partial-fill / bracket
        / OCO surface introduced in #383. ``unfilled_policy`` /
        ``attached_stop_loss`` / ``attached_take_profit`` are runtime-
        supported as of #389; ``parent_order_id`` / ``oco_group_id`` are
        engine-internal (set on bracket children by ``OrderBook.submit_attached``
        — strategies do not pass them).
        """
        self._next_client_order_id += 1
        cid = f"c{self._next_client_order_id}"
        req = OrderRequest(
            client_order_id=cid,
            symbol=symbol,
            side=OrderSide(side) if not isinstance(side, OrderSide) else side,
            qty=qty,
            order_type=(
                OrderType(order_type) if not isinstance(order_type, OrderType) else order_type
            ),
            limit_price=limit_price,
            stop_price=stop_price,
            trail_offset=trail_offset,
            trail_offset_kind=trail_offset_kind,
            tif=TimeInForce(tif) if not isinstance(tif, TimeInForce) else tif,
            reason=reason,
            unfilled_policy=(
                UnfilledPolicy(unfilled_policy)
                if unfilled_policy is not None and not isinstance(unfilled_policy, UnfilledPolicy)
                else unfilled_policy
            ),
            twap_slices=twap_slices,
            attached_stop_loss=attached_stop_loss,
            attached_take_profit=attached_take_profit,
            parent_order_id=parent_order_id,
            oco_group_id=oco_group_id,
        )
        req.validate_prices()
        # The chunked harness wraps ``self._emit`` to inject ``bar_index``
        # using a harness-private closure (issue #377 / PR #425). We
        # deliberately do NOT read ``self._current_bar_index`` here:
        # strategy code can mutate that attribute, and a strategy that
        # set it to an earlier bar after observing later bars in the
        # chunk could backdate emissions and bypass look-ahead safety.
        # Letting the harness be the sole source of truth makes that
        # forge unreachable from strategy code.
        self._emit({"kind": "order", "payload": req.model_dump(mode="json")})
        return cid

    def cancel(self, order_id: str) -> None:
        # See ``submit_order``: bar_index is injected by the harness's
        # wrapped emit, not from any strategy-writable attribute.
        self._emit({"kind": "cancel", "payload": {"order_id": order_id}})

    # ------------------------------------------------------------------
    # Harness-private ingest methods — not part of the strategy API.
    # ------------------------------------------------------------------

    def _ingest_bar(self, bar: Bar) -> None:
        self._history.setdefault(bar.symbol, []).append(bar)
        # Bound the retained history to keep strategy subprocess memory sane;
        # strategies that need more are expected to maintain their own state.
        # ``STREAMING_WINDOW_BARS`` is the single source of truth for this
        # ceiling — the alignment/coverage audit and the conformance shadow
        # context must trim to the same bound this production context uses.
        hist = self._history[bar.symbol]
        if len(hist) > STREAMING_WINDOW_BARS:
            del hist[:-STREAMING_WINDOW_BARS]
        self._current_symbol = bar.symbol
        self._now = bar.timestamp

    def _ingest_state(
        self,
        *,
        capital: float,
        equity: float,
        positions: List[_PositionSnapshot],
        is_warmup: bool,
    ) -> None:
        self._capital = capital
        self._equity = equity
        self._is_warmup = is_warmup
        self._positions = {p.symbol: p for p in positions}


class Strategy:
    """Base class for Strategy-Lab-generated scripts.

    Subclasses override the ``on_*`` hooks they care about. The default
    implementations are no-ops so minimal strategies stay terse.
    """

    def on_start(self, ctx: StrategyContext) -> None:  # noqa: D401 - hook
        """Called once before the first bar."""

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        """Called once per finalized bar. Primary decision point."""

    def on_bars(self, ctx: StrategyContext, bars: List[Bar]) -> None:
        """Reserved for future vectorised dispatch — **do not override**
        under the chunked protocol introduced in issue #377.

        The chunked harness rejects override of this method with a
        ``contract_error`` because a vectorised override would receive
        the whole chunk before the parent replays bars one-by-one,
        letting a strategy peek at later bars and emit orders tagged to
        earlier bar indices. The parent trusts ``bar_index`` for
        ``submitted_at``, so the override path would bypass look-ahead
        safety. Vectorised authors should run with ``BAR_CHUNK_SIZE=1``
        (per-bar dispatch) and implement :meth:`on_bar` instead.

        The default body is a no-op kept here so :meth:`type(instance).on_bars`
        compares true to ``contract.Strategy.on_bars`` in the harness's
        override check; subclasses that don't define ``on_bars`` skip the
        rejection branch.
        """

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        """Called when a previously-submitted order fills."""

    def on_end(self, ctx: StrategyContext) -> None:
        """Called after the last bar (or on session termination)."""
