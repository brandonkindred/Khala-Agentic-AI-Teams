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
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class OrderSide(str, Enum):
    LONG = "long"
    SHORT = "short"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
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


class StopAttachment(BaseModel):
    """Stop-loss leg attached to an entry order; materialized into an OCO child on entry fill."""

    stop_price: float
    trail_offset: Optional[float] = None
    trail_offset_kind: Literal["abs", "bps"] = "abs"
    client_order_id: Optional[str] = None


class LimitAttachment(BaseModel):
    """Take-profit leg attached to an entry order; materialized into an OCO child on entry fill."""

    limit_price: float
    client_order_id: Optional[str] = None


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
    tif: TimeInForce = TimeInForce.DAY
    reason: str = ""  # free-form annotation; surfaced in logs / fills
    unfilled_policy: Optional[UnfilledPolicy] = None
    twap_slices: Optional[int] = None
    attached_stop_loss: Optional[StopAttachment] = None
    attached_take_profit: Optional[LimitAttachment] = None
    parent_order_id: Optional[str] = None
    oco_group_id: Optional[str] = None

    def validate_prices(self) -> None:
        """Enforce order_type / tif / policy / attachment constraints.

        Runtime-support gates run **before** the shape-consistency checks so
        a strategy that asks for an un-implemented feature gets the explicit
        ``NotImplementedError`` (which propagates as a structured
        ``unsupported_feature`` failure), not a generic ``ValueError`` that
        the broad ``except`` in ``TradingService`` would silently log-and-drop.
        """
        # Runtime-support gates. The schema fields below land in this PR (#383)
        # so callers and Pydantic models compile, but the execution engine does
        # not yet honor them — that lands in later steps of #379. Until those
        # steps ship, fail loudly at submission time rather than silently
        # producing never-filled orders or IOC/FOK that behave like GTC.
        if self.order_type == OrderType.TRAILING_STOP:
            raise UnsupportedOrderFeatureError(
                "trailing_stop is not yet supported by the execution engine; "
                "see #390 (Trading 5/5 Step 8) for runtime support"
            )
        if self.tif in (TimeInForce.IOC, TimeInForce.FOK):
            raise UnsupportedOrderFeatureError(
                f"{self.tif.value} time-in-force is not yet supported by the execution engine; "
                "see #388 (Trading 5/5 Step 6) for runtime support"
            )
        if self.attached_stop_loss is not None or self.attached_take_profit is not None:
            raise UnsupportedOrderFeatureError(
                "attached_stop_loss / attached_take_profit are not yet materialized "
                "as bracket children; see #389 (Trading 5/5 Step 7) for runtime support"
            )
        if self.parent_order_id is not None:
            raise UnsupportedOrderFeatureError(
                "parent_order_id is not yet honored; see #389 (Trading 5/5 Step 7) "
                "for bracket-child materialization"
            )
        if self.oco_group_id is not None:
            raise UnsupportedOrderFeatureError(
                "oco_group_id is not yet honored; see #389 (Trading 5/5 Step 7) "
                "for OCO sibling cancellation"
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
        if self.order_type == OrderType.TRAILING_STOP and self.stop_price is None:
            raise ValueError("trailing_stop order requires stop_price")
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
        if (
            self.attached_stop_loss is not None or self.attached_take_profit is not None
        ) and self.parent_order_id is not None:
            raise ValueError(
                "attachments may only be set on entry-creating orders (parent_order_id must be None)"
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
        # and by a synchronous in-process driver in unit tests.
        self._emit = emit
        self._history: Dict[str, List[Bar]] = {}
        self._positions: Dict[str, _PositionSnapshot] = {}
        self._capital: float = 0.0
        self._equity: float = 0.0
        self._now: str = ""
        self._is_warmup: bool = False
        self._next_client_order_id: int = 0

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

        The trailing keyword arguments (``unfilled_policy``,
        ``attached_stop_loss``, ``attached_take_profit``,
        ``parent_order_id``, ``oco_group_id``) belong to the partial-fill /
        bracket / OCO surface introduced in #383. They are accepted by the
        API but currently raise ``NotImplementedError`` from
        ``validate_prices`` until the relevant runtime step of #379 lands.
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
        self._emit({"kind": "order", "payload": req.model_dump(mode="json")})
        return cid

    def cancel(self, order_id: str) -> None:
        self._emit({"kind": "cancel", "payload": {"order_id": order_id}})

    # ------------------------------------------------------------------
    # Harness-private ingest methods — not part of the strategy API.
    # ------------------------------------------------------------------

    def _ingest_bar(self, bar: Bar) -> None:
        self._history.setdefault(bar.symbol, []).append(bar)
        # Bound the retained history to keep strategy subprocess memory sane;
        # strategies that need more are expected to maintain their own state.
        hist = self._history[bar.symbol]
        if len(hist) > 500:
            del hist[:-500]
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

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        """Called when a previously-submitted order fills."""

    def on_end(self, ctx: StrategyContext) -> None:
        """Called after the last bar (or on session termination)."""
