"""Mode-agnostic Trading Service event loop.

Takes a ``MarketDataStream`` and a strategy code string, runs them through the
streaming subprocess harness, and collects the resulting trades and fills.

The fill simulator has a one-bar forward view (it looks at *t+1* to decide
fills for orders submitted on bar *t*). The strategy subprocess never sees
future bars — the look-ahead safety boundary is the subprocess itself, not
a convention. See ``strategy/streaming_harness.py`` and
``docs/system_design`` for details.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import date as date_cls
from functools import cached_property
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import numpy as np

from ..execution.bar_safety import LookAheadError
from ..execution.metrics import EquityCurve, weekday_range
from ..execution.risk_filter import RiskFilter, RiskLimits
from ..models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    OrderLifecycleEvent,
    TradeRecord,
    scaled_level_key,
)
from ..strategy_lab.executor.predicate_evaluator import (
    BarRecord,
    StreamingHistoryView,
)
from ..strategy_lab.executor.predicate_evaluator import (
    evaluate_entry_rules as _evaluate_entry_rules_pred,
)
from ..strategy_lab.executor.rule_compiler import (
    _EMPTY_CURSOR,
    BarSnapshot,
    ExitIntent,
    PositionState,
    evaluate_exit_rules,
    first_exit_intent_for_position,
    is_limit_stop_rule,
)
from ..strategy_lab.spec_dsl import (
    EntryRule,
    ExitRule,
    FixedFractionSizing,
    FixedNotionalSizing,
    OcoBracketRule,
    ScaledTakeProfitRule,
    StopLossRule,
    VolatilityTargetSizing,
    first_side_stop_factor,
    is_bracket_exit,
    protective_limit_price,
    protective_stop_price,
)
from ..strategy_lab_context import is_fractional_asset_class
from .data_stream.protocol import BarEvent, EndOfStreamEvent, StreamEvent
from .engine.execution_model import build_execution_model
from .engine.fill_simulator import (
    ENGINE_EXIT_REASON_PREFIX,
    FillOutcome,
    FillSimulator,
    FillSimulatorConfig,
)
from .engine.order_book import FILL_QTY_REL_TOL, OrderBook, PendingOrder
from .engine.portfolio import Portfolio, Position
from .strategy.contract import (
    BPS_DIVISOR,
    Bar,
    ExitLegSpec,
    LimitAttachment,
    OrderRequest,
    OrderSide,
    OrderType,
    StopAttachment,
    TimeInForce,
    UnfilledPolicy,
    UnsupportedOrderFeatureError,
    apply_bps_offset,
)
from .strategy.streaming_harness import StrategyRuntimeError, StreamingHarness

logger = logging.getLogger(__name__)

# Cap on how many recent order-lifecycle events are retained in
# ``diagnostics.last_order_events`` — the tail is trimmed to the most recent 20 (see
# the trim in ``_record_event``) to bound per-run diagnostics memory while
# keeping enough trailing context to explain the latest fills.
_MAX_ORDER_EVENTS = 20

# A scaled rung "empties" the position when its (original-qty-sized) close covers
# the whole remaining qty. The "essentially full" comparison reuses the fill
# layer's established relative qty tolerance (``FILL_QTY_REL_TOL``) — the same
# constant the simulator uses to clamp a tiny residual fill qty to zero — so the
# "is this qty effectively the whole position" judgement is sourced once. It only
# absorbs float noise in the fraction arithmetic (e.g. 0.3 + 0.3 + 0.4 not summing
# to exactly 1.0); it never treats a genuine residual as a full close.

# ``ENGINE_EXIT_REASON_PREFIX`` is the reserved order ``reason`` prefix the
# engine stamps on every close it owns (rule-triggered emissions and reconciled
# strategy closes). The conformance quality gate reads it off
# ``OrderLifecycleEvent`` records to verify each trade obeyed the structured
# rules. Canonical definition lives in the engine layer (``fill_simulator``) and
# is re-exported here for the many call sites and external importers that read it
# off this module.

#: The ``engine_exit:stop_loss`` literal, defined once — unlike
#: ``ENGINE_EXIT_REASON_PREFIX`` above, this one is NOT engine-layer-canonical
#: and re-exported; it is defined directly in this module because every site
#: that stamps it also lives in this module: ``_EngineExitDispatcher.
#: _build_close_order`` on the bar-close path, ``resolve_resting_stop_loss_attachment``
#: on the resting-order path, and ``_build_exit_reconciler``'s closure for a
#: reconciled strategy close. The ``alignment_checks`` / ``exit_rule_conformance``
#: quality gates match it byte-exactly. Referencing this one constant from all
#: three same-module sites means an edit to the suffix can't desynchronize one
#: from another.
ENGINE_EXIT_REASON_STOP_LOSS = f"{ENGINE_EXIT_REASON_PREFIX}stop_loss"


def _engine_exit_kind(reason: str) -> str:
    """The bare ``rule_kind`` encoded in an engine-exit order ``reason``.

    Single source of engine-reason → kind parsing: strips the
    ``ENGINE_EXIT_REASON_PREFIX`` and any trailing ``[idx]`` rule-index suffix
    (which ``signal_exit`` closes carry) so the result is the diagnostics key
    (``stop_loss`` / ``take_profit`` / ``scaled_take_profit`` / ``signal_exit``).

    Preconditions: ``reason`` starts with ``ENGINE_EXIT_REASON_PREFIX`` (enforced
    with an explicit raise so a non-engine reason fails loudly rather than being
    silently mis-sliced into a bogus kind).
    Postconditions: returns the ``rule_kind`` substring — prefix removed and any
    ``[...]`` index suffix dropped.
    """
    if not reason.startswith(ENGINE_EXIT_REASON_PREFIX):
        raise ValueError(
            f"_engine_exit_kind requires an {ENGINE_EXIT_REASON_PREFIX!r}-prefixed reason, got {reason!r}"
        )
    kind = reason[len(ENGINE_EXIT_REASON_PREFIX) :]
    bracket = kind.find("[")
    return kind[:bracket] if bracket != -1 else kind


def _build_exit_reconciler(
    exit_rules: Optional[Sequence[ExitRule]],
    views: Mapping[str, StreamingHistoryView],
    position_tracker: Optional[Mapping[str, "_TrackedPosition"]] = None,
) -> Optional[Callable[..., Optional[str]]]:
    """Build the engine-side exit-attribution reconciler for a run.

    A strategy-initiated close that complies with a structured exit rule at
    the close bar must still carry ``engine_exit:<kind>`` attribution so the
    engine stays the single source of exit truth. This returns the closure
    :class:`~.engine.fill_simulator.FillSimulator` invokes at its
    TradeRecord exit-stamping site.

    Preconditions:
      * ``exit_rules`` is the run's structured ``spec.exit_rules`` (possibly
        empty).
      * ``views`` is the live per-symbol streaming-history map the run
        appends to each bar; signal-exit predicates read its latest bar.
      * ``position_tracker`` is the run's live per-symbol tracker (or
        ``None`` in isolation). It is consulted only to mirror the engine
        exit dispatcher's entry-bar skip (see below).

    Postconditions:
      * Returns ``None`` iff ``exit_rules`` is empty — the FillSimulator
        then keeps its no-op default and the existing fill-simulator unit
        tests are unaffected.
      * Otherwise returns a callable
        ``(*, symbol, side, entry_price, qty) -> Optional[str]`` — arguments are
        keyword-only (the FillSimulator invokes it with keywords) — yielding
        ``"engine_exit:<kind>"`` when a structured rule *fires* at the
        **signal bar** (the bar the strategy acted on, one before the fill),
        else ``None``. It does not mutate ``exit_rules`` or ``views``.

    Firing semantics: stamp on rule-fire, with no realized-return bound. This
    mirrors the engine exit dispatcher (which stamps ``engine_exit:<kind>``
    whenever a rule fires on the streaming view) and the post-#915 alignment
    gate, which treats a stop-loss / take-profit as a TRIGGER, not a hard price
    cap: a next-bar market fill can legitimately gap past the nominal
    ceiling/floor and is still an engine-owned exit. Magnitude/firing-rate
    enforcement is owned by ``ExitRuleConformanceGate``, not this per-trade
    attribution step.
      * ``take_profit`` / ``stop_loss`` (entry-price basis) — stamp
        ``engine_exit:<kind>`` (unbracketed) when the rule fires; the
        alignment gate matches these by exact string.
      * ``signal_exit`` — stamp ``engine_exit:signal_exit[<idx>]`` with the
        spec rule index, matching the engine's emitted form so the
        rule-firing-rate gate counts the reconciled close.
      * trailing-basis stops are path-dependent and deferred (never stamped).

    Entry-bar skip: a close whose signal bar is the entry bar of a
    *non-market* entry is never reconciled (``position_tracker[sym]
    .just_opened``), mirroring the dispatcher — the entry bar's pre-fill OHLC
    is ambiguous and the engine deliberately does not own an exit there.
    """
    if not exit_rules:
        return None

    rules = list(exit_rules)

    def _reconcile(
        *,
        symbol: str,
        side: str,
        entry_price: float,
        qty: float,
    ) -> Optional[str]:
        if qty <= 0:
            return None
        # Mirror the engine exit dispatcher's entry-bar skip. ``just_opened``
        # is True only for a non-market (limit/stop) entry on the bar it first
        # appears; the dispatcher does not evaluate exit rules there because
        # the bar's pre-fill OHLC is ambiguous. At this call site (during
        # process_bar, before this bar's tracker update) the flag still
        # reflects the signal bar, so reconciling would falsely attribute an
        # exit the engine deliberately did not own. Market entries leave the
        # flag False and may fire same-bar, matching the dispatcher.
        if position_tracker is not None:
            tracked = position_tracker.get(symbol)
            if tracked is not None and tracked.just_opened:
                return None
        # Evaluate every rule at the *signal bar* — the bar the strategy acted
        # on, one before the fill. At the fill simulator's exit-stamping site
        # the run loop has not yet appended the fill bar to ``views``, so the
        # view's latest bar IS the signal bar. Using it for the price (TP/SL)
        # snapshot as well as signal-exit predicates matches the engine exit
        # dispatcher (which fires rules on the streaming view) and the
        # alignment gate (signal bar = fill bar − 1). Evaluating TP/SL against
        # the fill bar instead would miss a close queued on the rule's bar when
        # the fill bar doesn't re-cross, and mislabel a discretionary close
        # when only the fill bar crosses.
        view = views.get(symbol)
        if view is None or view.length() == 0:
            return None
        i = view.length() - 1
        bs = BarSnapshot(
            high=view.bar_field("high", i),
            low=view.bar_field("low", i),
            close=view.bar_field("close", i),
        )
        ps = PositionState(
            symbol=symbol,
            side=side,  # type: ignore[arg-type]  # "long" | "short" Literal
            qty=qty,
            entry_price=entry_price,
            # Trailing watermarks are not tracked at the close site; collapse
            # them to ``entry_price``. The collapse is inert for the rule kinds
            # reconciled here: trailing-basis stops are skipped below, and
            # signal-exit predicates evaluate over bar history (indicators /
            # ``bar.*`` fields), never the position's running peak/trough, so
            # they do not consult these fields. (A signal rule that genuinely
            # needed running watermarks is unsupported and simply would not be
            # reconciled — it is not silently mis-stamped.)
            high_since_entry=entry_price,
            low_since_entry=entry_price,
        )
        # Stamp the first rule that fires at the signal bar (spec order =
        # priority), with no realized-return bound — a fired rule is an
        # engine-owned exit even if the next-bar fill gapped past the level.
        for idx, rule in enumerate(rules):
            kind = getattr(rule, "kind", None)
            # Trailing-basis stops are path-dependent (running peak/trough);
            # deferred to the alignment gate, never reconciled here.
            if kind == "stop_loss" and getattr(rule, "basis", "entry_price") != "entry_price":
                continue
            # Scaled take-profits are PARTIAL, engine-emitted scale-outs — the
            # strategy never authors them, and a manual FULL close is not one of
            # their rungs — so they are not reconcilable to a strategy close. Skip
            # explicitly (rather than relying on fall-through past the kind-dispatch
            # below) and avoid evaluating the rung needlessly.
            if kind == "scaled_take_profit":
                continue
            if not evaluate_exit_rules([rule], {symbol: ps}, {symbol: bs}, views=views):
                continue
            if kind == "take_profit":
                return f"{ENGINE_EXIT_REASON_PREFIX}take_profit"
            if kind == "stop_loss":
                return ENGINE_EXIT_REASON_STOP_LOSS
            if kind == "signal_exit":
                # Match the engine's emitted form ``engine_exit:signal_exit[N]``
                # (``_build_close_order``) so the rule-firing-rate gate, which
                # only counts the bracketed form, credits the reconciled close.
                # ``idx`` is the spec ``exit_rules`` index (== ``rule_index``).
                return f"{ENGINE_EXIT_REASON_PREFIX}signal_exit[{idx}]"
        return None

    return _reconcile


@dataclass
class _ScaledLadderCursor:
    """A position's progress through its scaled-take-profit ladder(s).

    Owns the "next un-fired rung" invariant so the pure evaluator (which *picks*
    the rung to offer) and the dispatcher (which *advances* past a rung that
    fired) share one definition instead of each doing index arithmetic. Keyed by
    a ladder's ``rule_index`` in ``exit_rules``; an absent key means cursor ``0``
    (no rung fired yet). Rungs fire in strict order one tranche per bar, so the
    cursor rung is the only one the evaluator ever offers.

    Invariant: a rung fires only when it equals its ladder's current cursor, and
    firing advances the cursor by exactly one.

    Invariant: ``_next_rung`` is only ever mutated IN PLACE (``advance`` does
    ``self._next_rung[...] = ...``) and never reassigned — the memoized ``mapping``
    proxy wraps this exact dict, so reassigning it would leave the proxy stale.
    """

    _next_rung: Dict[int, int] = field(default_factory=dict)
    _view: Optional[Mapping[int, int]] = field(default=None, init=False, repr=False, compare=False)

    @property
    def mapping(self) -> Mapping[int, int]:
        """The ``rule_index -> next un-fired rung`` view handed to the evaluator.

        Preconditions: none. Postconditions: returns a read-only
        :class:`~types.MappingProxyType` over the live cursor mapping — mutation
        attempts raise ``TypeError`` (advancing goes through :meth:`advance`),
        while a cursor advanced after this call is still reflected (the proxy is a
        live view, not a copy). The proxy is built once and memoized (it wraps the
        same underlying dict for the cursor's lifetime), so per-bar reads on the
        hot path allocate nothing.
        """
        if self._view is None:
            self._view = MappingProxyType(self._next_rung)
        return self._view

    def advance(self, rule_index: int, fired_rung: int) -> None:
        """Advance past the rung that just fired.

        Preconditions: ``fired_rung`` is the ladder's current cursor (the only rung
        the evaluator offers); enforced with an explicit raise so the invariant
        holds even under ``python -O``. Postconditions: the ladder's cursor becomes
        ``fired_rung + 1``.
        """
        expected = self._next_rung.get(rule_index, 0)
        if fired_rung != expected:
            raise ValueError(
                f"scaled rungs must fire in cursor order: rule {rule_index} got rung "
                f"{fired_rung}, expected {expected}"
            )
        self._next_rung[rule_index] = fired_rung + 1


@dataclass
class _PositionStateView:
    """Mutable, reusable stand-in for :class:`PositionState` on the hot path.

    Structurally matches ``PositionState`` (identical field names), so the pure
    evaluator reads it unchanged — but a single instance per tracked position is
    mutated in place each bar instead of allocating a fresh frozen snapshot.

    Invariant: only :meth:`_TrackedPosition.snapshot` mutates it, and the
    evaluator treats it as read-only within one call (it never retains the
    reference past the call — :class:`ExitIntent` copies the values it needs), so
    reusing the instance across bars is safe.

    Contract: this view's fields must stay name- and type-compatible with
    ``PositionState`` (the evaluator reads them interchangeably). That parity is
    enforced by ``test_position_state_view_matches_position_state_fields`` — if the
    two shapes drift, that test fails rather than the mismatch surfacing as silent
    stale data on the hot path.
    """

    symbol: str
    side: str
    qty: float
    entry_price: float
    high_since_entry: float
    low_since_entry: float


@dataclass
class _TrackedPosition:
    """Per-symbol state the parent engine maintains to evaluate exit rules.

    Mirrors the public :class:`PositionState` shape but is mutable so the bar
    loop can update watermarks in place. The snapshot handed to
    :func:`evaluate_exit_rules` is a fresh immutable copy.

    ``entry_order_id`` pins the tracker to a specific :class:`Position`
    instance. When a same-bar exit-then-re-entry replaces the underlying
    position, ``portfolio.positions[sym]`` swaps to a new ``Position`` with
    a different ``entry_order_id``; the tracker reset path in
    :meth:`TradingService._update_position_tracker` detects that and starts
    fresh, so a stale trailing watermark can't fire a rule on the
    brand-new trade.

    ``just_opened`` is ``True`` on the bar a position first appears.
    :meth:`_EngineExitDispatcher.maybe_emit` skips rule evaluation
    while this flag is set so the entry bar's pre-fill price action (a
    limit fill at ``bar.low`` doesn't entitle a take-profit to fire from
    a pre-entry ``bar.high``) can't queue impossible close orders. The
    next bar's tracker update clears the flag.

    Remaining fields: ``high_since_entry`` / ``low_since_entry`` are the running
    watermarks since entry (consumed by trailing stops and laddered take-profit
    eligibility); ``scaled_cursor`` is this position's progress through its
    scaled-take-profit ladder(s). All three reset with the tracker on a position
    swap (the ``entry_order_id`` change above), so no rung-fired / watermark state
    leaks across two distinct trades.
    """

    side: OrderSide
    entry_price: float
    entry_order_id: str
    just_opened: bool
    high_since_entry: float
    low_since_entry: float
    # Progress through this position's scaled-take-profit ladder(s). Because a
    # fresh ``_TrackedPosition`` is built whenever the underlying position is
    # replaced (``entry_order_id`` change — see
    # ``TradingService._update_position_tracker``), the cursor never leaks across
    # two distinct trades.
    scaled_cursor: _ScaledLadderCursor = field(default_factory=_ScaledLadderCursor)
    # Side conversions, fixed for the tracker's life and derived once in
    # ``__post_init__``: the evaluator wants the side as a ``"long"/"short"``
    # string, and the engine close wants the opposite ``OrderSide``. Caching them
    # keeps the per-bar snapshot / close build from recomputing the conversion.
    side_str: str = field(init=False, repr=False, compare=False)
    close_side: OrderSide = field(init=False, repr=False, compare=False)
    # Reusable per-bar evaluator view (see ``snapshot``), mutated in place so the
    # hot path allocates no fresh ``PositionState``. Created lazily on first use.
    _snap: Optional[_PositionStateView] = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Derive the cached side conversions once.

        Preconditions: ``side`` is ``OrderSide.LONG`` or ``OrderSide.SHORT``.
        Postconditions: ``side_str`` is the evaluator's ``"long"``/``"short"``
        form and ``close_side`` is the opposite ``OrderSide`` (the side that
        closes this position) — both invariant for the tracker's life.
        """
        self.side_str = "long" if self.side == OrderSide.LONG else "short"
        self.close_side = OrderSide.SHORT if self.side == OrderSide.LONG else OrderSide.LONG

    def snapshot(self, symbol: str, qty: float) -> _PositionStateView:
        """Reusable evaluator view of this position as of the current bar.

        Preconditions: ``qty`` is the live position qty for ``symbol``, and
        ``symbol`` is the symbol this tracker tracks. The tracker doesn't store its
        own symbol (it lives in a ``{symbol: tracker}`` map and is only ever looked
        up by that key, then ``snapshot``-ed with the same key — see ``_evaluate``),
        so the match is a caller guarantee enforced by that single lookup site.
        ``symbol`` is (re)written onto the view on EVERY call — including the reuse
        path — so the returned view can never carry a stale symbol from a prior bar
        even if this precondition were ever violated.
        Postconditions: returns a :class:`_PositionStateView` (structurally a
        ``PositionState``) whose fields (``symbol``, ``qty``, watermarks, and
        ``entry_price``) reflect this call — the SAME instance across bars, so the
        hot path allocates nothing after the first call. ``side`` is fixed for the
        tracker's life; ``entry_price`` is NOT (``Portfolio.extend`` refreshes it to
        the weighted-average on a scale-in / partial-fill continuation, mirrored onto
        the tracker by ``_update_position_tracker``), so it is re-read here every bar.
        """
        snap = self._snap
        if snap is None:
            self._snap = _PositionStateView(
                symbol=symbol,
                side=self.side_str,
                qty=qty,
                entry_price=self.entry_price,
                high_since_entry=self.high_since_entry,
                low_since_entry=self.low_since_entry,
            )
            return self._snap
        snap.symbol = symbol
        snap.qty = qty
        snap.entry_price = self.entry_price
        snap.high_since_entry = self.high_since_entry
        snap.low_since_entry = self.low_since_entry
        return snap


class _EvalGate(NamedTuple):
    """What :meth:`_EngineExitDispatcher._should_evaluate` found for one bar.

    A named result (vs. a bare tuple) so the per-position gate facts are
    self-documenting and safe to extend. ``resting_limit_stop_id`` is the
    ``order_id`` of an already-resting engine STOP_LIMIT (or ``None``);
    ``entry_continuation_in_flight`` is ``True`` when the position's own entry is
    still filling (a same-side partially-filled continuation rests);
    ``scaled_partial_in_flight`` is ``True`` when a prior scaled-take-profit rung's
    market scale-out is still pending (so the next rung is deferred this bar, but a
    full-position exit may still fire to protect the runner). ``pending`` is the
    symbol's pending-order snapshot scanned to derive those facts — carried so the
    full-close path reuses it instead of rebuilding it several more times.
    """

    tracked: "_TrackedPosition"
    pos: Position
    resting_limit_stop_id: Optional[str]
    entry_continuation_in_flight: bool
    scaled_partial_in_flight: bool
    pending: List[PendingOrder]


@dataclass(frozen=True)
class _EmitContext:
    """The per-bar state both exit-emission handlers need.

    Built once in :meth:`_EngineExitDispatcher.maybe_emit` after gating and
    evaluation, then handed to :meth:`_emit_partial_scale_out` /
    :meth:`_emit_full_close` so neither repeats a long shared parameter list —
    new per-bar state is threaded by adding one field here, not by editing both
    handler signatures.
    """

    sym: str
    tracked: "_TrackedPosition"
    pos: Position
    pending_for_prev: List[OrderRequest]
    order_book: OrderBook
    cur_bar: Any
    result: "TradingServiceResult"
    resting_limit_stop_id: Optional[str]
    # The symbol's pending-order snapshot from ``_should_evaluate`` — reused by the
    # full-close path instead of re-querying the order book per helper.
    pending: List[PendingOrder]


@dataclass
class _EngineExitDispatcher:
    """Per-run owner of engine-side ``exit_rules`` enforcement.

    Per-bar pipeline (:meth:`maybe_emit`): gate the symbol
    (:meth:`_should_evaluate` → :class:`_EvalGate`), pick the highest-priority
    triggered intent (:meth:`_evaluate` over :func:`evaluate_exit_rules`), then
    dispatch by close type — :meth:`_emit_partial_scale_out` for a laddered
    scale-out rung (leaves the remainder and its protective orders working) or
    :meth:`_emit_full_close` for a stop / take-profit / signal exit (runs the
    whole-position cleanups). Shared per-bar state travels in an
    :class:`_EmitContext`.

    Splits the per-bar engine-side enforcement loop into one method
    per concern so each can be tested and extended in isolation. Holds
    the run-scoped state that used to be threaded through helper
    keyword arguments:

    * ``exit_rules`` — the spec's structured close conditions (immutable
      across the run).
    * ``engine_exit_bindings`` — ``client_order_id → entry_order_id``
      bindings consumed by the bar-loop submit step to stamp
      ``working_against_entry_order_id`` on the resulting
      ``PendingOrder``. Same map covers engine emissions and same-bar
      piggybacked strategy orders.
    * ``_next_seq`` — monotonic counter for engine-issued
      ``client_order_id``\\ s. Strategy ids are emitted client-side; engine
      ids must not collide, hence the ``e`` prefix vs the strategy's
      ``c`` prefix.
    * ``exclude_rule_index`` — the spec index of a rule ceded to a
      resting-order mechanism for this run (currently only the
      entry_price/market stop-loss migration's resting ``STOP``
      attachment — see ``_EngineEntryDispatcher.resting_stop_loss_enabled``);
      ``None`` (the default) evaluates every rule as today. Enforces mutual
      exclusion between the two mechanisms: see
      ``rule_compiler._filtered_intent_for_rule`` for where this is applied.

    Empty ``exit_rules`` makes :meth:`maybe_emit` a no-op.

    ``@dataclass`` is used only for its generated ``__init__`` — it injects the
    run-scoped fields below (``exit_rules`` / ``engine_exit_bindings`` / ``views``)
    in one place rather than a hand-written constructor. This is a behavioural
    service class, not a pure data container; the decorator is a constructor
    convenience, nothing more.
    """

    exit_rules: Sequence[ExitRule]
    engine_exit_bindings: Dict[str, str] = field(default_factory=dict)
    _next_seq: int = 0
    views: Optional[Dict[str, StreamingHistoryView]] = None
    exclude_rule_index: Optional[int] = None

    # ------------------------------------------------------------------

    def maybe_emit(
        self,
        *,
        cur_bar,
        position_tracker: Mapping[str, _TrackedPosition],
        portfolio: Portfolio,
        pending_for_prev: List[OrderRequest],
        order_book: OrderBook,
        result: "TradingServiceResult",
    ) -> None:
        """Top-level entry point — call once per bar after strategy orders
        have been queued into ``pending_for_prev``.

        ``pending_for_prev`` is the engine's standing name for the list of order
        requests being accumulated for the NEXT bar's submission (orders queued on
        the current bar submit on the following one, for look-ahead safety) — engine
        closes built here are appended to it. The name is shared across the bar
        loop; it is the queue this emission contributes to, not a prior bar's state.

        Dedup model: the engine always emits at the position's full open
        qty and lets the fill simulator + the position-identity binding
        handle the rest. Specifically:

        * Engine orders carry ``working_against_entry_order_id`` via
          ``engine_exit_bindings``. If a same-bar strategy order closes
          the position first on the next bar, the fill simulator's
          stale-continuation guard drops the engine close before it
          falls through to ``_fill_entry``.
        * If the strategy's same-bar order is partial / clipped (FOK
          rejection, IOC drop, participation cap, REQUEUE_NEXT_BAR
          residual), the engine close sits behind it in submission order
          and ``_fill_exit`` clips ``req.qty`` to ``existing_pos.qty`` —
          residual exposure gets closed on the same bar rather than
          waiting for the rule to fire again.
        * The one explicit guard is on in-flight engine markets: if a
          prior bar's engine exit is still pending (e.g. REQUEUE
          residual across bars), skip re-emission so the order book
          doesn't accumulate redundant engine markets while the rule
          keeps re-triggering.

        Engine-emitted orders carry ``reason="engine_exit:<rule_kind>"``
        so the conformance gate can count them off the
        order-lifecycle event stream.
        """
        if not self.exit_rules:
            return

        sym = cur_bar.symbol
        gate = self._should_evaluate(sym, position_tracker, portfolio, order_book)
        if gate is None:
            return
        exclude_resting_limit_stop = gate.resting_limit_stop_id is not None

        # Resting structured exit: when a limit-style STOP_LIMIT already rests on
        # the book (``resting_limit_stop_id`` set), the spec's (single — see
        # StrategySpec validation) limit-style stop is treated as in flight, so the
        # chosen intent excludes that stop rule. This both (a) suppresses a
        # duplicate stop-limit emission and (b) lets a lower-priority rule
        # (take-profit / signal-exit) still fire and close the position.
        # (Standing the whole bar down would instead starve any rule listed after
        # the stop on every bar the stop re-triggers.) When a replacement intent
        # is emitted we cancel the resting stop-limit below — otherwise it could
        # fill first at its stale limit price on a recovery bar and pre-empt the
        # intended close.
        # Defer scale-outs (exclude scaled rungs) in two cases, while still
        # evaluating so a lower-priority full-position exit (e.g. a stop after the
        # ladder) can fire this bar:
        #   * the position's own entry is still filling — a rung sized off the
        #     not-yet-settled ``original_qty`` would under-close (true even for a
        #     ``qty_fraction == 1.0`` rung); and
        #   * a prior rung's scale-out market is still in flight — fire the next rung
        #     only once this one completes (the one-rung-per-bar ordering the
        #     full-MARKET standdown used to enforce), without blocking the runner's
        #     protective exits.
        exclude_scaled = gate.entry_continuation_in_flight or gate.scaled_partial_in_flight
        intent = self._evaluate(
            sym,
            gate.tracked,
            gate.pos,
            cur_bar,
            exclude_resting_limit_stop=exclude_resting_limit_stop,
            exclude_scaled=exclude_scaled,
        )
        if intent is None:
            return

        ctx = _EmitContext(
            sym=sym,
            tracked=gate.tracked,
            pos=gate.pos,
            pending_for_prev=pending_for_prev,
            order_book=order_book,
            cur_bar=cur_bar,
            result=result,
            resting_limit_stop_id=gate.resting_limit_stop_id,
            pending=gate.pending,
        )
        # A scaled-ladder rung and a full-position close have distinct emission
        # flows (the scale-out path sizes off the original qty and skips the
        # whole-position cleanups *unless* the rung empties the position) — so
        # dispatch on the intent's origin, not on how much it closes.
        if intent.is_scaled_rung:
            self._emit_partial_scale_out(intent, ctx)
        else:
            self._emit_full_close(intent, ctx)

    def _emit_partial_scale_out(self, intent: ExitIntent, ctx: "_EmitContext") -> None:
        """Emit a scaled-take-profit rung's scale-out close and advance the cursor.

        A scale-out is sized off the ORIGINAL entry qty and ordinarily leaves the
        rest of the position open, so — unlike :meth:`_emit_full_close` — it does
        NOT oversize for scale-ins. It runs the whole-position cleanups (retire
        competing resting exits, cancel entry continuations / a replaced resting
        stop-limit) ONLY when this rung actually EMPTIES the position (a
        ``qty_fraction == 1.0`` rung, or the final rung of a ladder summing to 1.0),
        since for a true partial the remainder must keep its protective exits.

        Deferral while the entry is still filling is handled UPSTREAM: ``maybe_emit``
        excludes scaled rungs (``exclude_scaled``) when an entry continuation is in
        flight, so a deferred rung is simply never produced here — and a
        lower-priority full-position exit can still fire that bar.

        Preconditions: ``intent.is_scaled_rung`` (``intent.level_index`` is set) and
        the position's entry has settled (no continuation in flight).
        Postconditions: appends one scale-out close to ``ctx.pending_for_prev``,
        binds it to the position, advances the ladder cursor past the fired rung,
        runs the whole-position cleanups iff the close empties the position, and
        records the emission.
        """
        req = self._build_close_order(intent, ctx.tracked, ctx.pos)
        if req is None:
            return
        ctx.pending_for_prev.append(req)
        # Bind to the position so the stale-continuation guard drops this scale-out
        # if a full exit (e.g. a stop) closes the position first.
        self._register_binding(req, ctx.pos)
        level_index = intent.level_index
        if level_index is None:  # pragma: no cover - is_scaled_rung guarantees a level_index
            raise ValueError("scaled-rung intent requires a level_index")
        ctx.tracked.scaled_cursor.advance(intent.rule_index, level_index)
        # If this rung closes the entire remaining position, it is effectively a
        # full exit — run the same cleanups so nothing keeps working against the
        # now-closed position. ``req.qty`` is sized off the ORIGINAL qty, so when a
        # scale-in grew the position above it this is correctly False (a residual
        # remains and must keep its exits). ``FILL_QTY_REL_TOL`` (the fill layer's
        # shared relative qty tolerance) only absorbs float noise in the fraction
        # arithmetic.
        #
        # ``ctx.pos.qty`` is the live portfolio qty, which is exact for this backtest
        # engine: one tranche fires per bar and the in-flight-engine-MARKET guard in
        # ``_should_evaluate`` stands the bar down while a prior rung's close is still
        # pending, so by the time a later rung fires every earlier rung has already
        # filled and reduced ``pos.qty``. (A live venue with delayed fills could see
        # an earlier rung still unfilled here; that is out of scope for the backtest.)
        #
        # Both ``req.qty`` and ``ctx.pos.qty`` are ABSOLUTE quantities (the position's
        # side is tracked separately via ``tracked.side`` / ``close_side``), so this
        # "rung empties the position" comparison holds identically for longs and shorts.
        if req.qty >= ctx.pos.qty * (1.0 - FILL_QTY_REL_TOL):
            self._retire_orders_against_closed_position(intent, ctx)
        self._record_emission(req, intent, ctx.cur_bar, ctx.result)

    def _emit_full_close(self, intent: ExitIntent, ctx: "_EmitContext") -> None:
        """Emit a full-position close and run the whole-position cleanups.

        Preconditions: ``intent`` closes the full position in one firing (stop-loss
        / take-profit / signal-exit; ``not intent.is_scaled_rung``).
        Postconditions: appends one close to ``ctx.pending_for_prev``, binds it,
        retires competing resting exits, cancels entry continuations (market style)
        and the replaced resting stop-limit (when one rested), and records the
        emission.
        """
        # Contract guard: only non-scaled closes reach here (the dispatch routes
        # scaled rungs to ``_emit_partial_scale_out``, which runs the same cleanups
        # itself only when a rung empties the position). Enforced with a raise (not
        # an assert) so it holds even under ``python -O``.
        if intent.is_scaled_rung:  # pragma: no cover - dispatch routes scaled rungs elsewhere
            raise ValueError("_emit_full_close must not receive a scaled rung")
        scale_in_qty = self._scale_in_oversize(intent, ctx)
        req = self._build_close_order(intent, ctx.tracked, ctx.pos, scale_in_qty)
        if req is None:
            return

        ctx.pending_for_prev.append(req)
        self._register_binding(req, ctx.pos)
        self._retire_orders_against_closed_position(intent, ctx)
        self._record_emission(req, intent, ctx.cur_bar, ctx.result)

    def _scale_in_oversize(self, intent: ExitIntent, ctx: "_EmitContext") -> float:
        """Extra qty to add to a full close so it still fully covers any same-side
        scale-in that could grow the position before the close fills next bar.

        Two scale-in sources, both settling BEFORE the engine close on the next bar:
        a same-bar strategy order already queued in ``ctx.pending_for_prev``, and a
        GTC/limit scale-in still resting on the order book (``ctx.pending`` — the
        snapshot ``_should_evaluate`` already scanned, reused here). ``_fill_exit``
        clips the engine close at ``min(req.qty, existing_pos.qty)``, so without this
        oversize the residual exposure would stay open even though the structured
        exit fired; and if a scale-in is rejected / clipped at fill time the close
        clips back down to the actual live qty — no over-close risk.

        Preconditions: ``intent`` is a full-position close (not a scaled rung);
        ``ctx.pending`` is this symbol's pending-order snapshot.
        Postconditions: returns ``>= 0`` — ``0`` for the common close with nothing
        else working for the symbol (every order-book-derived term is a no-op then).
        """
        qty = self._sum_same_side_queued(ctx.sym, ctx.tracked.side, ctx.pending_for_prev)
        if ctx.pending:
            qty += self._sum_same_side_resting(ctx.tracked.side, ctx.pending)
            # A limit-style stop does NOT cancel the position's in-flight entry
            # continuation (a market close cancels it outright, so it must not
            # double-count this term), so its still-unfilled remainder can grow the
            # position before this close fills — oversize to cover it.
            if intent.style == "limit":
                qty += self._sum_entry_continuation_remainder(ctx.pos, ctx.pending)
        return qty

    def _retire_orders_against_closed_position(
        self, intent: ExitIntent, ctx: "_EmitContext"
    ) -> None:
        """Bind/cancel everything that must not keep working against a position
        this close fully empties.

        Shared by :meth:`_emit_full_close` and by an EMPTYING scaled rung, so the
        same dangling-order protection applies however the position leaves. A rung
        "empties" the position only when its close qty actually covers the whole
        remaining qty — a ladder summing to 1.0 is NOT sufficient on its own: if a
        scale-in grew the position past the original entry qty, a rung sized off
        ``original_qty`` leaves the scale-in portion open and must NOT trigger these
        cleanups.

        Preconditions: the caller has verified this close empties the position —
        the close qty just appended to ``ctx.pending_for_prev`` is ``>=`` the
        current ``pos.qty`` (``_emit_partial_scale_out`` guards on
        ``req.qty >= pos.qty``; ``_emit_full_close`` always closes the full qty).
        ``intent.style != "limit"`` whenever ``ctx.resting_limit_stop_id`` is set
        (guaranteed: a resting limit stop is excluded from evaluation, so the
        chosen intent is a different rule).
        Postconditions: binds competing opposite-side resting exits and same-bar
        queued exits to the position; for a market close cancels the position's
        entry continuations; and cancels a replaced resting stop-limit.
        """
        sym, tracked, pos, order_book, pending = (
            ctx.sym,
            ctx.tracked,
            ctx.pos,
            ctx.order_book,
            ctx.pending,
        )
        if pending:
            # Bind competing opposite-side exits to the position. This is safe for
            # BOTH styles: binding only *retires* an order via the fill simulator's
            # stale-continuation guard once the position is actually closed/replaced,
            # so a ``style="limit"`` stop that gaps through and rests leaves the
            # bound competing orders working. Binding is what prevents an unbound
            # competing exit (e.g. a resting take-profit) from surviving the close
            # and later firing as a fresh reverse entry — so it must run for the
            # limit-style stop too, not just the guaranteed market close.
            self._retire_competing_resting_orders(tracked.side, pos, pending)
        self._bind_same_bar_queued_exits(sym, tracked.side, pos, ctx.pending_for_prev)
        # Cancelling the entry continuation, by contrast, *actively* removes a
        # legitimate scale-in remainder on the assumption the close will fill.
        # A market close is guaranteed next bar; a limit-style stop may gap
        # through unfilled, so deferring this avoids stripping a scale-in for a
        # position that stays open. The continuation's remainder was already
        # folded into ``scale_in_qty`` above, so if the limit close later fills
        # after the continuation grew the position, it still covers the grown
        # size (``_fill_exit`` clips to the live qty) — no residual exposure.
        if intent.style != "limit" and pending:
            self._cancel_pending_entry_continuations(pos, order_book, pending)
        # A resting limit-style STOP_LIMIT is excluded from evaluation, so when one
        # rests the chosen intent is always a *different*, market-style rule
        # (take-profit / signal-exit / scaled rung) — a guaranteed close next bar.
        # Cancel the now redundant resting stop-limit: left on the book it sits
        # ahead of this close in submission order, so on a recovery bar that makes
        # its latched limit marketable it would fill first at the stale limit price
        # and the intended close would be dropped by the stale-continuation guard.
        # Binding alone cannot prevent this — it only retires the stop *after* the
        # position is gone, which is too late once the stop itself is what closed it.
        if ctx.resting_limit_stop_id is not None:
            # A limit-style intent never reaches here (a resting limit stop is
            # excluded from evaluation, so the chosen rule is a different one) —
            # enforce it with a raise so the stop-cancel can't fire for a re-emitted
            # limit stop even under ``python -O``.
            if intent.style == "limit":  # pragma: no cover - excluded by _evaluate
                raise ValueError("cannot cancel a resting limit stop for a limit-style intent")
            order_book.cancel(ctx.resting_limit_stop_id)

    # ------------------------------------------------------------------
    # Sub-steps. Kept as private methods so subclasses or sibling unit
    # tests can override / poke at a single concern.
    # ------------------------------------------------------------------

    def _should_evaluate(
        self,
        sym: str,
        position_tracker: Mapping[str, _TrackedPosition],
        portfolio: Portfolio,
        order_book: OrderBook,
    ) -> Optional[_EvalGate]:
        """Return an :class:`_EvalGate` if rule evaluation should run for this
        symbol on this bar, else ``None``.

        Gates:
        * Tracker has the symbol (a position is open).
        * Portfolio agrees and has positive qty.
        * ``tracked.just_opened`` is False — skip the entry bar for
          non-market fills (see ``_update_position_tracker``).
        * No in-flight engine MARKET *full* exit already pending on the order book —
          a guaranteed market close fills next bar, so re-emitting while it is in
          flight would stack a redundant market. Two deliberate exceptions keep a
          protective exit from being starved: a *resting* limit-style STOP_LIMIT
          does NOT stand evaluation down (a higher-priority take-profit / signal
          exit must still be able to fire while the stop-limit rests unfilled), and
          an in-flight *scaled-take-profit rung* market (a PARTIAL scale-out) does
          NOT stand the bar down either — it only defers the next rung, so a stop /
          take-profit / signal exit can still close the runner the partial leaves
          open.

        The single pending-order pass also derives (a) the resting limit-stop's
        ``order_id`` so ``maybe_emit`` can drop the in-flight limit stop from the
        chosen intent and cancel that resting STOP_LIMIT when a different rule
        replaces it, and (b) whether the position's own entry is still filling (a
        same-side partially-filled continuation rests) so the scaled-take-profit
        deferral needs no second order-book scan. A spec has at most one
        limit-style stop (enforced by ``StrategySpec``), so "an engine STOP_LIMIT
        rests" unambiguously means "that stop is in flight" — no per-order
        identity bookkeeping beyond the single id is needed. The id is only
        captured when ``_has_limit_stop_rule`` holds, so it is directly actionable
        (always ``None`` for a market-only spec).

        Preconditions: ``order_book`` is the live order book for this run, and
        ``position_tracker``/``portfolio`` reflect state as of the current bar.
        Postconditions: returns ``None`` when evaluation should be skipped;
        otherwise an :class:`_EvalGate` carrying the open ``(tracked, pos)``, the
        ``order_id`` of the already-resting limit-style stop (non-``None`` only when
        the spec has a limit stop AND an engine STOP_LIMIT for this position is
        resting; a non-``None`` id means the chosen intent must exclude that stop
        rule), and ``entry_continuation_in_flight`` — ``True`` iff a
        scaled-take-profit spec has a same-side partially-filled entry continuation
        still resting (so the
        scaled deferral can read it without rescanning the book).
        """
        tracked = position_tracker.get(sym)
        if tracked is None:
            return None
        pos = portfolio.positions.get(sym)
        if pos is None or pos.qty <= 0:
            return None
        if tracked.just_opened:
            return None
        # Snapshot the symbol's pending orders ONCE here; the full-close path
        # reuses this list rather than rebuilding it per helper. The single pass
        # both decides whether to stand the bar down and derives the per-order gate
        # facts (see :meth:`_scan_pending_for_gate`).
        pending = order_book.pending_for_symbol(sym)
        scan = self._scan_pending_for_gate(tracked, pos, pending)
        if scan is None:
            return None  # an in-flight engine MARKET full close is already pending
        resting_limit_stop_id, entry_continuation_in_flight, scaled_partial_in_flight = scan
        return _EvalGate(
            tracked=tracked,
            pos=pos,
            resting_limit_stop_id=resting_limit_stop_id,
            entry_continuation_in_flight=entry_continuation_in_flight,
            scaled_partial_in_flight=scaled_partial_in_flight,
            pending=pending,
        )

    def _scan_pending_for_gate(
        self,
        tracked: _TrackedPosition,
        pos: Position,
        pending: List[PendingOrder],
    ) -> Optional[tuple[Optional[str], bool, bool]]:
        """Derive the per-order gate facts from one bar's pending-order snapshot.

        A single pass over the symbol's ``pending`` orders. The derived facts only
        matter when the spec can author the relevant rule, so the per-order checks
        are gated on ``_has_limit_stop_rule`` / ``_has_scaled_take_profit_rule`` and
        skipped for the common market-only spec.

        Preconditions: ``pending`` is the order book's pending list for this
        position's symbol; ``tracked`` / ``pos`` describe the open position.
        Postconditions: returns ``None`` when an in-flight engine MARKET *full*
        close is already pending — the bar must stand down, since re-emitting would
        stack a redundant guaranteed close while the rule keeps re-triggering. An
        in-flight *scaled-take-profit rung* market does NOT stand the bar down (see
        ``scaled_partial_in_flight`` below). Otherwise returns
        ``(resting_limit_stop_id, entry_continuation_in_flight,
        scaled_partial_in_flight)``:
          * ``resting_limit_stop_id`` — the ``order_id`` of an already-resting
            limit-style STOP_LIMIT (or ``None``; a non-``None`` id means the chosen
            intent must exclude that stop rule);
          * ``entry_continuation_in_flight`` — ``True`` iff a scaled-take-profit
            spec has the position's own same-side, partially-filled entry
            continuation still resting (so the scaled deferral can read it without a
            second scan);
          * ``scaled_partial_in_flight`` — ``True`` iff a prior scaled rung's market
            scale-out is still pending. The bar keeps evaluating (so a stop /
            take-profit / signal can still close the runner the partial leaves open),
            but ``maybe_emit`` defers any further rung — exactly the "complete this
            rung before the next fires" guarantee the full-MARKET standdown gave,
            now WITHOUT also blocking the full-position exits that protect the
            remainder.
        """
        track_resting = self._has_limit_stop_rule
        track_continuation = self._has_scaled_take_profit_rule
        resting_limit_stop_id: Optional[str] = None
        entry_continuation_in_flight = False
        scaled_partial_in_flight = False
        for po in pending:
            po_req = po.request
            if po_req.side == tracked.side:
                # Same-side: the position's own partially-filled entry continuation
                # (a REQUEUE_NEXT_BAR / TWAP_N remainder, identified by
                # ``order_id == entry_order_id`` with some qty already filled). Both
                # policies requeue the SAME order under its original ``order_id``
                # (the fill simulator calls ``order_book.requeue(po.order_id, ...)``
                # and never mints child orders with fresh ids), so this single test
                # covers TWAP slices too.
                if (
                    track_continuation
                    and po.order_id == pos.entry_order_id
                    and po.cumulative_filled_qty > 0
                ):
                    entry_continuation_in_flight = True
                continue
            reason = po_req.reason or ""
            if not reason.startswith(ENGINE_EXIT_REASON_PREFIX):
                continue
            if po_req.order_type == OrderType.MARKET:
                if po_req.engine_scaled_partial:
                    # In-flight PARTIAL scale-out: a scaled rung's market is still
                    # pending (e.g. a participation-capped rung requeued across
                    # bars), identified by the structural ``engine_scaled_partial``
                    # flag the emitter set (no reason-string parsing). The flag is
                    # set only by ``_build_close_order`` on scaled rungs, so it is
                    # self-sufficient — no ``track_continuation`` guard needed (a
                    # spec without a ladder can never produce this order). Do NOT
                    # stand the bar down — that would also block a stop / take-profit
                    # / signal exit from closing the runner the partial leaves open.
                    # Just flag it so ``maybe_emit`` defers the NEXT rung
                    # (``exclude_scaled``), preserving the one-rung-at-a-time
                    # ordering without starving the runner's protective exits.
                    scaled_partial_in_flight = True
                    continue
                # In-flight guaranteed FULL close — stand the whole bar down.
                return None
            if track_resting and po_req.order_type == OrderType.STOP_LIMIT:
                resting_limit_stop_id = po.order_id
        return resting_limit_stop_id, entry_continuation_in_flight, scaled_partial_in_flight

    def _evaluate(
        self,
        sym: str,
        tracked: _TrackedPosition,
        pos: Position,
        cur_bar: Any,  # structurally a bar exposing high/low/close — see evaluate_exit_rules_for_position
        *,
        exclude_resting_limit_stop: bool = False,
        exclude_scaled: bool = False,
    ) -> Optional[ExitIntent]:
        """Run the pure rule evaluator and pick the intent to act on.

        Default: returns the first triggered rule's intent per spec priority
        (:func:`evaluate_exit_rules` stops at the first trigger).

        ``exclude_resting_limit_stop`` is set by ``maybe_emit`` when the spec's
        (single) limit-style stop already has a STOP_LIMIT resting: the evaluator
        returns all triggered intents in priority order and this method skips the
        in-flight limit-style intent, returning the first lower-priority rule
        instead (take-profit / signal-exit), or ``None`` if the limit stop is the
        only trigger. Because a spec has at most one limit-style stop (enforced by
        ``StrategySpec``), skipping every limit-style intent skips exactly that
        one rule. The pure evaluator stays unaware of order-book state; the skip
        decision lives here in the dispatcher.

        Already-fired scaled-take-profit rungs need no skip pass here: the
        position's ``scaled_cursor`` is handed to the evaluator, which only ever
        offers the next un-fired rung, so each rule already contributes at most one
        intent and the default single-trigger path applies.

        ``exclude_scaled`` is set by ``maybe_emit`` while the position's entry is
        still filling: a rung sized off the not-yet-settled ``original_qty`` would
        under-close (true even for a ``qty_fraction == 1.0`` rung), so scale-outs
        are deferred — but a lower-priority full-position exit (e.g. a stop listed
        after the ladder) must still be free to fire this bar, so the rung is
        skipped rather than standing the whole bar down.

        ``self.exclude_rule_index`` (constructor-set, not a ``maybe_emit`` parameter
        since it is fixed for the whole run) drops the rule ceded to a resting-order
        mechanism outright, before the pure evaluator even builds its intent — see
        :class:`_EngineExitDispatcher`'s docstring for the mutual-exclusion contract
        this enforces.

        Preconditions: ``tracked``/``pos`` describe the same open position
        (``pos.qty > 0``); ``cur_bar`` exposes ``high``/``low``/``close``.
        Postconditions: returns an ``ExitIntent`` to emit, or ``None`` (no rule
        triggered, the only trigger is an excluded resting limit stop / deferred
        partial scale-out, or the only trigger is the rule at ``exclude_rule_index``).
        """
        snapshot = tracked.snapshot(sym, pos.qty)
        # Hot path: evaluate this one position directly — no per-bar ``{sym: ...}``
        # wrapper dicts, and no ``BarSnapshot`` rebuild (``cur_bar`` already exposes
        # ``high``/``low``/``close``). The ladder cursor is passed only when the
        # spec has a ladder; otherwise the shared empty cursor is used.
        cursor_map = (
            tracked.scaled_cursor.mapping if self._has_scaled_take_profit_rule else _EMPTY_CURSOR
        )
        view = self.views.get(sym) if self.views is not None else None
        # ``exclude_resting_limit_stop`` skips the spec's (single) limit-style stop
        # in the same pass — its STOP_LIMIT already rests, so the first non-resting
        # rule wins without a collect-all-then-filter second walk. The dispatcher
        # acts on a single intent, so it uses the allocation-free first-intent scan
        # (no throwaway one-element list per bar of every open position).
        return first_exit_intent_for_position(
            self.exit_rules,
            sym,
            snapshot,
            cur_bar,
            view=view,
            cursor_map=cursor_map,
            exclude_limit_style=exclude_resting_limit_stop,
            exclude_scaled=exclude_scaled,
            exclude_rule_index=self.exclude_rule_index,
        )

    @cached_property
    def _has_limit_stop_rule(self) -> bool:
        """Whether the spec contains a ``style="limit"`` stop-loss rule. Lets the
        per-bar dispatch skip the resting-order re-ranking for the common
        market-only specs.

        Postconditions: ``True`` iff any exit rule is a limit-style stop-loss.
        Cached: ``exit_rules`` is immutable across the run.
        """
        return any(is_limit_stop_rule(r) for r in self.exit_rules)

    @cached_property
    def _has_scaled_take_profit_rule(self) -> bool:
        """Whether the spec contains a laddered (``ScaledTakeProfitRule``) exit, so
        the per-bar dispatch evaluates all triggered intents and consults the
        position's fired-rung set instead of taking the first trigger blindly.

        Postconditions: ``True`` iff any exit rule is a ``ScaledTakeProfitRule``.
        Cached: ``exit_rules`` is immutable across the run.
        """
        return any(isinstance(r, ScaledTakeProfitRule) for r in self.exit_rules)

    @staticmethod
    def _entry_continuations(pos: Position, pending: List[PendingOrder]) -> List[PendingOrder]:
        """The position's own in-flight entry continuations — the partially-filled
        ``REQUEUE_NEXT_BAR`` / ``TWAP_N`` remainder of its entry order, identified
        by ``po.order_id == pos.entry_order_id`` and ``cumulative_filled_qty > 0``.

        Single source of the continuation-identity rule shared by the limit-style
        oversize (:meth:`_sum_entry_continuation_remainder`) and the market-style
        cancel (:meth:`_cancel_pending_entry_continuations`).

        Preconditions: ``pos`` is the open position; ``pending`` is the symbol's
        pending-order snapshot for this bar.
        Postconditions: returns the matching pending orders (possibly empty).
        """
        return [
            po
            for po in pending
            if po.order_id == pos.entry_order_id and po.cumulative_filled_qty > 0
        ]

    @classmethod
    def _sum_entry_continuation_remainder(cls, pos: Position, pending: List[PendingOrder]) -> float:
        """Unfilled remainder of the position's in-flight entry continuation(s).

        Preconditions: ``pos`` is the open position; ``pending`` is the symbol's
        pending-order snapshot for this bar.
        Postconditions: returns the summed unfilled qty (``>= 0``), so a limit-style
        close can be oversized to cover growth it does not cancel. ``_fill_exit``
        clips to the live qty, so the oversize is safe.
        """
        return sum(
            max(po.request.qty - po.cumulative_filled_qty, 0.0)
            for po in cls._entry_continuations(pos, pending)
        )

    @staticmethod
    def _sum_same_side_queued(
        sym: str,
        tracked_side: OrderSide,
        pending_for_prev: List[OrderRequest],
    ) -> float:
        """Sum the qty of same-side strategy orders queued for the
        same symbol — i.e. scale-ins the strategy submitted on this
        bar that will fill on the next bar before the engine close.
        """
        total = 0.0
        for queued in pending_for_prev:
            if queued.symbol != sym:
                continue
            if queued.side != tracked_side:
                continue
            total += queued.qty
        return total

    @staticmethod
    def _sum_same_side_resting(
        tracked_side: OrderSide,
        pending: List[PendingOrder],
    ) -> float:
        """Sum the unfilled qty of same-side orders already resting
        on the book — i.e. scale-ins the strategy submitted on a
        prior bar that are still working and could fill on the next
        bar alongside the engine close (e.g. GTC limits at a deeper
        price).

        Mirrors :meth:`_sum_same_side_queued` but for the resting
        side of the world. The two are summed at the call site and
        passed to :meth:`_build_close_order` as ``scale_in_qty``.

        Excludes already-bound orders — those will be retired by the
        stale-continuation guard once the engine close fills, so they
        won't add to the position. ``cumulative_filled_qty`` is
        subtracted off the unfilled portion: a partially filled
        scale-in's already-filled qty is already accounted for in
        ``pos.qty``.
        """
        total = 0.0
        for po in pending:
            req = po.request
            if req.side != tracked_side:
                continue
            if po.working_against_entry_order_id is not None:
                continue
            remaining = req.qty - po.cumulative_filled_qty
            if remaining <= 0:
                continue
            total += remaining
        return total

    def _build_close_order(
        self,
        intent: ExitIntent,
        tracked: _TrackedPosition,
        pos: Position,
        scale_in_qty: float = 0.0,
    ) -> Optional[OrderRequest]:
        """Construct + validate the engine's market close. Returns
        ``None`` on validation failure (logged, run continues).

        ``scale_in_qty`` is added to ``pos.qty`` so any same-side
        same-bar strategy order that grows the position next bar is
        also closed by this emission (see ``_sum_same_side_queued``).

        A ``style="limit"`` stop-loss intent builds a *resting* STOP_LIMIT
        (GTC, ``REQUEUE_NEXT_BAR``) from the evaluator-resolved ``intent.stop_price``
        and ``intent.limit_price`` (see ``_build_stop_limit_close``), rather than
        the guaranteed market close every other intent uses. The fill simulator
        owns its arm/latch/gap-through lifecycle from there.

        Preconditions: ``tracked``/``pos`` describe the same open position. For a
        scaled rung (``intent.is_scaled_rung``), the position's entry has fully
        settled — the dispatcher defers a rung while an entry continuation is in
        flight (``exclude_scaled``) — so ``pos.original_qty`` holds the FULL opened
        size and the rung's fraction sizes off it. ``scale_in_qty`` is meaningful
        only for a full-position close (it is ignored for a rung, which deliberately
        leaves the remainder open).
        Postconditions: returns a validated ``OrderRequest`` for the close — a
        scaled rung sized ``qty_fraction * original_qty`` (falling back to the live
        ``pos.qty`` only when ``original_qty`` is unset, where the two are equal), a
        full close sized ``pos.qty + scale_in_qty`` — or ``None`` if the built
        request fails price validation (logged; the run continues).
        """
        self._next_seq += 1
        close_side = tracked.close_side
        if intent.rule_kind == "signal_exit":
            reason = f"{ENGINE_EXIT_REASON_PREFIX}{intent.rule_kind}[{intent.rule_index}]"
        elif intent.rule_kind == "stop_loss":
            # Must be the byte-stable ENGINE_EXIT_REASON_STOP_LOSS literal, not
            # folded into the generic branch below — alignment_checks /
            # exit_rule_conformance match it exactly (no [index] suffix).
            reason = ENGINE_EXIT_REASON_STOP_LOSS
        else:
            reason = f"{ENGINE_EXIT_REASON_PREFIX}{intent.rule_kind}"
        if intent.is_scaled_rung:
            # Scaled rung: close this rung's fraction of the full opened position
            # (``scale_in_qty`` is irrelevant — a true partial deliberately leaves
            # the rest open; an emptying rung's fraction already covers it).
            # ``original_qty`` is the cumulative entry-filled qty; the
            # caller defers the rung until the entry has fully settled, so it now
            # equals the full opened size. The ``> 0`` fallback to the live qty
            # never mis-sizes in practice: the engine's fill path always pins
            # ``original_qty`` at open, and the only state where it could be unset
            # is a freshly opened position with no partial exits yet — where the
            # live ``pos.qty`` still equals the original. (The fill simulator also
            # clips to the live qty, so a fraction larger than what remains simply
            # closes the remainder.)
            base = pos.original_qty if pos.original_qty > 0 else pos.qty
            qty = intent.qty_fraction * base
        else:
            qty = pos.qty + scale_in_qty
        # A scaled rung is always a market scale-out so it never takes the resting
        # STOP_LIMIT path below (that path has no REQUEUE_NEXT_BAR and would strand a
        # participation-capped rung remainder, since a rung fires at most once and
        # cannot re-emit). This invariant is enforced at the source — ``ExitIntent``'s
        # ``__post_init__`` rejects a scaled rung with ``style != "market"`` at
        # construction — so no defensive re-check is needed here.
        if intent.style == "limit":
            req = self._build_stop_limit_close(intent, close_side, qty, reason)
        else:
            req = OrderRequest(
                client_order_id=f"e{self._next_seq}",
                symbol=intent.symbol,
                side=close_side,
                qty=qty,
                order_type=OrderType.MARKET,
                tif=TimeInForce.DAY,
                reason=reason,
                # A full-position exit (stop / take-profit / signal) self-heals a
                # participation-capped partial fill: its rule re-triggers next bar
                # and re-emits a fresh close for the residual, so the dropped
                # remainder is closed then. A scaled rung fires AT MOST ONCE, so it
                # cannot re-emit — without a requeue a capped fill would close less
                # than the rung's fraction permanently. Force REQUEUE_NEXT_BAR on a
                # scaled close so its remainder completes across bars regardless of
                # the run's default policy. Leave every other exit's policy unset
                # (``None``) so the service-level ``default_unfilled_policy`` still
                # applies to it exactly as before.
                unfilled_policy=(
                    UnfilledPolicy.REQUEUE_NEXT_BAR if intent.is_scaled_rung else None
                ),
                # Structural marker the per-bar exit gate reads to tell this PARTIAL
                # scale-out apart from a full-position close without parsing
                # ``reason`` (see ``_scan_pending_for_gate``). Only scaled rungs set
                # it; a full close leaves it False.
                engine_scaled_partial=intent.is_scaled_rung,
            )
        try:
            req.validate_prices()
        except Exception as exc:  # pragma: no cover — engine-built request
            logger.error(
                "engine-issued exit order failed validation (rule=%s symbol=%s): %s",
                intent.rule_kind,
                intent.symbol,
                exc,
            )
            return None
        return req

    def _build_stop_limit_close(
        self,
        intent: ExitIntent,
        close_side: OrderSide,
        qty: float,
        reason: str,
    ) -> OrderRequest:
        """Build the resting STOP_LIMIT close for a ``style="limit"`` stop.

        Preconditions: ``intent.style == "limit"`` with ``intent.stop_price`` and
        ``intent.limit_price`` resolved by the evaluator (the limit already sits
        on the protective side — below the stop for a SHORT close, above for a
        LONG close — via ``spec_dsl.protective_limit_price``).
        Postconditions: returns an unvalidated STOP_LIMIT ``OrderRequest`` with a
        strictly-positive limit price (guaranteed by the DSL's ``limit_offset_pct
        < 1.0`` bound) on the protective side, matching ``validate_prices``'s sign
        rule.
        """
        assert intent.stop_price is not None, "limit-style exit intent missing stop_price"
        assert intent.limit_price is not None, "limit-style exit intent missing limit_price"
        assert intent.limit_price > 0, "limit-style exit intent has non-positive limit_price"
        return OrderRequest(
            client_order_id=f"e{self._next_seq}",
            symbol=intent.symbol,
            side=close_side,
            qty=qty,
            order_type=OrderType.STOP_LIMIT,
            stop_price=intent.stop_price,
            limit_price=intent.limit_price,
            tif=TimeInForce.GTC,
            unfilled_policy=UnfilledPolicy.REQUEUE_NEXT_BAR,
            reason=reason,
        )

    def _register_binding(self, req: OrderRequest, pos: Position) -> None:
        """Record the binding so the bar-loop submit step can set
        ``working_against_entry_order_id`` on the resulting
        ``PendingOrder``.
        """
        self.engine_exit_bindings[req.client_order_id] = pos.entry_order_id

    def _retire_competing_resting_orders(
        self,
        tracked_side: OrderSide,
        pos: Position,
        pending: List[PendingOrder],
    ) -> None:
        """Bind any unbound opposite-side resting orders to the position
        so they retire when the engine close removes the position.

        Precondition (caller's responsibility): the emission that triggered this
        is a WHOLE-position close — a full exit or an emptying scaled rung. It must
        NOT be called for a true partial scale-out (which leaves the position, and
        its competing orders, working). The sole caller,
        :meth:`_retire_orders_against_closed_position`, enforces this; the dispatch
        in :meth:`_emit_partial_scale_out` only invokes that cleanup once a rung's
        close covers the whole remaining qty.

        Without this, an unbound GTC/limit strategy exit
        (``cumulative_filled_qty==0`` AND
        ``working_against_entry_order_id is None``) would survive the
        engine close and, on a later trigger, fall through to
        ``_fill_entry`` (``existing_pos is None``) — opening an
        unintended reverse position.

        Carve-outs:
        * Already-bound orders (prior engine exits, bracket children)
          keep their binding.
        * Same-side resting orders are scale-in intents, not closes —
          left alone.
        * Partially filled orders are already bound to the position via
          ``_fill_exit``'s auto-binding.

        ``pending`` is the symbol's pending-order snapshot for this bar; the
        binding mutates each matching :class:`PendingOrder` in place, so it is
        reflected on the live book regardless of the snapshot.
        """
        for resting in pending:
            if resting.working_against_entry_order_id is not None:
                continue
            if resting.cumulative_filled_qty > 0:
                continue
            if resting.request.side == tracked_side:
                continue
            resting.working_against_entry_order_id = pos.entry_order_id

    def _bind_same_bar_queued_exits(
        self,
        sym: str,
        tracked_side: OrderSide,
        pos: Position,
        pending_for_prev: List[OrderRequest],
    ) -> None:
        """Bind same-bar opposite-side strategy orders queued in
        ``pending_for_prev`` (not yet on the order book). Same effect
        as :meth:`_retire_competing_resting_orders` but for orders that
        haven't reached the book yet — the binding goes into
        ``engine_exit_bindings`` and the submit step applies it.
        """
        for queued in pending_for_prev:
            if queued.symbol != sym:
                continue
            if queued.client_order_id in self.engine_exit_bindings:
                continue
            if queued.side == tracked_side:
                continue
            self.engine_exit_bindings[queued.client_order_id] = pos.entry_order_id

    def _cancel_pending_entry_continuations(
        self,
        pos: Position,
        order_book: OrderBook,
        pending: List[PendingOrder],
    ) -> None:
        """Cancel any in-flight continuation of the position's entry
        order. A partial-fill remainder (``REQUEUE_NEXT_BAR`` or
        ``TWAP_N``) still on the book would fill on the next bar
        before the engine's close, growing the position past what the
        engine sized for and leaving residual exposure after
        ``_fill_exit`` clips at ``min(req.qty, existing_pos.qty)``.

        Continuations are identified by :meth:`_entry_continuations` over the
        bar's ``pending`` snapshot (``po.order_id == pos.entry_order_id`` — the
        strategy can't reuse an engine-issued order_id, and same-side new strategy
        entries have different order_ids); each match is cancelled on ``order_book``.

        Precondition (caller's responsibility): invoked only for a WHOLE-position
        close (full exit or emptying rung), never for a true partial scale-out —
        a partial must leave its entry continuation working. Enforced by the sole
        caller, :meth:`_retire_orders_against_closed_position`.
        """
        for po in self._entry_continuations(pos, pending):
            order_book.cancel(po.order_id)

    def _record_emission(
        self,
        req: OrderRequest,
        intent: ExitIntent,
        cur_bar,
        result: "TradingServiceResult",
    ) -> None:
        """Bump diagnostics counters (global + per-symbol firings,
        ``orders_emitted`` / ``exits_emitted``) and append the
        ``OrderLifecycleEvent``.
        """
        diag = result.execution_diagnostics
        diag.orders_emitted += 1
        diag.exits_emitted += 1
        diag.exit_rule_firings[intent.rule_kind] = (
            diag.exit_rule_firings.get(intent.rule_kind, 0) + 1
        )
        sym_firings = diag.exit_rule_firings_by_symbol.setdefault(intent.symbol, {})
        sym_firings[intent.rule_kind] = sym_firings.get(intent.rule_kind, 0) + 1
        # Finer-grained, additive: distinguish trailing vs fixed stop fires
        # without perturbing ``rule_kind`` / the close ``reason`` that the
        # conformance + alignment gates match exactly.
        basis_label = (
            f"{intent.rule_kind}:{intent.basis}" if intent.basis is not None else intent.rule_kind
        )
        diag.exit_rule_firings_by_basis[basis_label] = (
            diag.exit_rule_firings_by_basis.get(basis_label, 0) + 1
        )
        # Per-rung counts for scaled take-profits, additive and keyed by
        # ``"{rule_index}:{level_index}"`` so each ladder rung is attributable
        # without perturbing the byte-stable ``rule_kind`` / close ``reason``.
        if intent.is_scaled_rung:
            level_key = scaled_level_key(intent.rule_index, intent.level_index)
            diag.scaled_take_profit_level_firings[level_key] = (
                diag.scaled_take_profit_level_firings.get(level_key, 0) + 1
            )
        _record_event(
            diag,
            "emitted",
            timestamp=cur_bar.timestamp,
            symbol=intent.symbol,
            side=req.side.value,
            order_type=req.order_type.value,
            reason=req.reason,
        )


ENGINE_ENTRY_REASON_PREFIX = "engine_entry:"


def _validated_stop_limit_offset(
    leg: ExitLegSpec, i: int, stop_price: float, is_long: bool
) -> float:
    """Compute and validate a STOP_LIMIT leg's limit_offset (an absolute
    distance off ``stop_price``; ``limit_offset_pct`` is a fraction of the
    stop level, bounded ``< 1.0`` by the leg validator so in exact
    arithmetic the offset stays inside ``(0, stop_price)`` and the derived
    protective limit stays positive and on the protective side — but that
    guarantee doesn't survive float64 rounding, so the derived price is
    validated directly rather than trusted from this bound alone.

    Combining ``limit_offset`` with ``stop_price`` (via the single shared
    sign-convention helper also used by ``rule_compiler`` and the fill
    simulator's bracket materialization, so this can never drift from what
    materialization actually submits) can round the result to exactly
    ``stop_price``, underflow to non-positive, or overflow to ``inf`` —
    none of which ``limit_offset`` alone rules out, since it can itself be
    finite/positive/non-negligible while the *combination* still
    misbehaves. So the derived price is validated directly, on top of
    validating ``limit_offset`` itself.

    Preconditions: ``leg.kind == OrderType.STOP_LIMIT`` (so
    ``leg.limit_offset_pct`` is set); ``stop_price`` is the finite,
    positive, correctly-signed value already resolved for this leg.
    Postconditions: returns a finite, strictly positive ``limit_offset``
    whose derived protective limit price is finite, strictly positive, and
    distinct from ``stop_price``.
    Raises:
        ValueError: if ``limit_offset`` is non-finite or non-positive, or if
            the derived protective limit price is non-finite, non-positive,
            or equal to ``stop_price``.
    """
    limit_offset = stop_price * leg.limit_offset_pct
    derived_limit_price = protective_limit_price(stop_price, limit_offset, closing_long=is_long)
    if (
        not math.isfinite(limit_offset)
        or limit_offset <= 0
        or not math.isfinite(derived_limit_price)
        or derived_limit_price <= 0
        or derived_limit_price == stop_price
    ):
        raise ValueError(
            f"exit leg #{i} ({leg.kind!r}) resolved non-finite/non-positive/negligible "
            f"limit_offset={limit_offset!r} (derived limit price {derived_limit_price!r}) "
            f"from stop_price={stop_price!r}, limit_offset_pct={leg.limit_offset_pct!r}"
        )
    return limit_offset


def _validated_trail_offset(leg: ExitLegSpec, i: int, ref_price: float, is_long: bool) -> float:
    """Compute and validate a TRAILING_STOP leg's trail_offset (a ``"bps"``
    value; see :class:`ExitLegSpec` for why this is basis points rather
    than an absolute distance).

    ``trail_offset`` itself is always finite in ``(0, BPS_DIVISOR)`` since
    ``leg.pct`` is Pydantic-bounded to ``(0, 1)`` — but that doesn't
    guarantee the materializer's own round-trip application (``price *
    (trail_offset / BPS_DIVISOR)``, then combined with that same price)
    survives float64: a vanishingly small ``pct`` can produce an offset
    that, scaled back down by a typical price, rounds to exactly ``0.0``
    relative to that price's ULP — e.g. ``pct=5.6e-17`` at a ``0.1`` price
    round-trips to an offset of ``5.6e-18``, and ``0.1 - 5.6e-18 == 0.1``
    bit-for-bit, so the trailing child would start at (not off) the entry
    fill despite the requested positive distance. The actual entry fill
    price isn't known here (that's the reason this is "bps" and not "abs"
    in the first place), so this previews the same round-trip — via
    :func:`apply_bps_offset`, the single source of that conversion shared
    with every ``fill_simulator`` application site, so this preview can
    never independently drift from what materialization actually computes
    — at ``ref_price`` as the best available proxy. Even so, a large
    enough gap between ``ref_price`` and the real fill could still
    reintroduce the failure mode, since materialization has no better
    input to validate against ahead of the actual fill.

    Preconditions: ``leg.kind == OrderType.TRAILING_STOP``; ``ref_price``
    is the finite, positive reference price already validated by the
    caller.
    Postconditions: returns a finite, strictly positive ``trail_offset``
    whose round-trip application at ``ref_price`` yields a finite,
    strictly positive, distinct-from-``ref_price`` preview stop.
    Raises:
        ValueError: if the round-trip-previewed offset or effective stop
            is non-finite, non-positive, or equal to ``ref_price``.
    """
    trail_offset = leg.pct * BPS_DIVISOR
    preview_offset = apply_bps_offset(ref_price, trail_offset)
    preview_stop = ref_price - preview_offset if is_long else ref_price + preview_offset
    if (
        not math.isfinite(preview_offset)
        or preview_offset <= 0
        or not math.isfinite(preview_stop)
        or preview_stop <= 0
        or preview_stop == ref_price
    ):
        raise ValueError(
            f"exit leg #{i} ({leg.kind!r}) resolved trail_offset={trail_offset!r} whose "
            f"materialization round-trip vanishes or misbehaves at ref_price={ref_price!r} "
            f"(preview_offset={preview_offset!r}, preview_stop={preview_stop!r}, pct={leg.pct!r})"
        )
    return trail_offset


def resolve_exit_leg_attachments(
    legs: Sequence[ExitLegSpec], side: OrderSide, ref_price: float
) -> List[Union[StopAttachment, LimitAttachment]]:
    """Resolve an ordered list of protective/target exit-leg specs into entry-order attachments.

    Pure function (no engine/dispatcher state) so the price math is
    unit-testable in isolation. Generalizes the bracket-only price math that
    was previously inlined in the bracket-only resolution path;
    :func:`resolve_bracket_attachments` below is now a thin adapter that
    translates an ``OcoBracketRule``'s two fixed legs into
    :class:`ExitLegSpec` instances and delegates here. Anchors every leg at
    ``ref_price`` independently (the
    signal-bar close, the same reference ``_compute_qty`` sizes against). For
    a long, protective legs (``STOP``/``STOP_LIMIT``/``TRAILING_STOP``) sit
    below and target legs (``LIMIT``) sit above the reference; for a short
    the signs flip. A ``STOP_LIMIT`` leg's secondary offset (``limit_offset``)
    is an absolute distance off the resolved stop level, not an absolute
    price, and a ``TRAILING_STOP`` leg's ``trail_offset`` is a ``"bps"``
    (basis-point) value, not ``"abs"`` — both because the stop level this
    attachment previews can trail/ratchet or seed from a different price by
    the time it materializes into a live child order. Materialization
    semantics (how/when each offset is re-applied) live with the
    materializer, not here; see :class:`ExitLegSpec` for the design
    rationale behind each offset's representation.

    Preconditions: ``ref_price`` is a finite number ``> 0``; ``side`` is the
    entry's ``OrderSide``; each element of ``legs`` is a validated
    :class:`ExitLegSpec`.
    Postconditions: returns a list the same length and order as ``legs``;
    each element is a :class:`StopAttachment` (``STOP``/``STOP_LIMIT``/
    ``TRAILING_STOP`` legs) or :class:`LimitAttachment` (``LIMIT`` legs) whose
    absolute price is finite, strictly positive, and strictly on the correct
    side of ``ref_price``; a ``STOP_LIMIT`` leg's ``limit_offset`` is finite,
    strictly positive, and large enough to survive the side-specific
    addition/subtraction materialization actually applies to ``stop_price``,
    and the *derived* protective limit price itself (``stop_price ∓
    limit_offset``, the value materialization actually submits) is finite,
    strictly positive, and distinct from ``stop_price`` — checking
    ``limit_offset`` alone doesn't rule this out, since it's an independent
    secondary quantity; a ``TRAILING_STOP`` leg's ``trail_offset`` is a
    ``"bps"`` value in ``(0, 10_000)`` whose round-trip application at
    ``ref_price`` (``ref_price * (trail_offset / BPS_DIVISOR)``, the same
    formula the materializer applies at the real entry fill price) is
    finite, strictly positive, and yields an effective stop distinct from
    ``ref_price`` — checked as a proxy for the real fill price, which isn't
    known at resolve time. Empty ``legs`` yields ``[]``.
    Raises:
        ValueError: if ``ref_price`` is non-finite (``NaN``/``inf``) or
            ``<= 0``, if ``side`` is not ``OrderSide.LONG`` or
            ``OrderSide.SHORT``, if a leg's ``kind`` is not one of
            ``OrderType.LIMIT``/``OrderType.STOP``/``OrderType.STOP_LIMIT``/
            ``OrderType.TRAILING_STOP`` (defense-in-depth; ``ExitLegSpec``
            already rejects such kinds at construction), or if a resolved
            price, a ``STOP_LIMIT`` leg's ``limit_offset`` or derived
            protective limit price, or a ``TRAILING_STOP`` leg's
            round-trip-previewed effective stop is non-finite, non-positive,
            or too small to survive its downstream arithmetic — a defensive
            guard that would only trip if a leg field bound were loosened
            without updating this math, an extreme ``ref_price`` overflowed
            the resolved price to ``inf``, or a vanishingly small
            ``pct``/``limit_offset_pct`` rounded away to nothing in float64
            on the side-specific direction materialization applies.
    """
    # Explicit raises (not ``assert``, which ``python -O`` strips) so the
    # contract stays enforced in optimized production runs. ``ref_price`` is a
    # plain float (unlike ``ExitLegSpec.pct``, whose Pydantic ``gt``/``lt``
    # bounds already reject NaN/inf), and NaN's reflexive-false comparisons
    # would otherwise slip past a bare ``<= 0`` check and propagate NaN/inf
    # prices into the returned attachments instead of raising here.
    if not math.isfinite(ref_price) or ref_price <= 0:
        raise ValueError(f"exit leg reference price must be positive and finite, got {ref_price!r}")
    # Same defense-in-depth posture as the per-leg ``kind`` check below: an
    # unrecognized ``side`` must fail loudly rather than being silently
    # treated as SHORT by the ``==`` comparison, which would invert every
    # leg's placement (protective legs above the reference, targets below)
    # without raising.
    if side not in (OrderSide.LONG, OrderSide.SHORT):
        raise ValueError(f"exit leg side must be OrderSide.LONG or OrderSide.SHORT, got {side!r}")
    is_long = side == OrderSide.LONG
    attachments: List[Union[StopAttachment, LimitAttachment]] = []
    # Every resolved-price check below (LIMIT's limit_price, the stop-family's
    # stop_price, and STOP_LIMIT's derived protective limit further down)
    # guards against the same two float64 failure modes: overflow to inf
    # (Python floats don't raise on overflow, and ``inf <= 0`` is False, so a
    # bare positivity check alone would miss it), and a valid-but-vanishingly
    # small fraction rounding away to exactly its reference value bit-for-bit
    # (e.g. ``ref * (1 ± pct) == ref`` for ``pct`` like ``1e-20`` — still
    # positive, but not actually off the reference). Neither is ruled out by
    # ``ExitLegSpec``'s field bounds alone, since those hold only in exact
    # arithmetic, so each resolved value is validated directly rather than
    # trusted from its inputs. A violation means the child would trigger at
    # (or through) the entry rather than after the requested move — fail
    # loudly at emit rather than materialize an unfillable/mistimed child.
    for i, leg in enumerate(legs):
        if leg.kind == OrderType.LIMIT:
            limit_price = ref_price * (1.0 + leg.pct) if is_long else ref_price * (1.0 - leg.pct)
            wrong_side = limit_price <= ref_price if is_long else limit_price >= ref_price
            if not math.isfinite(limit_price) or limit_price <= 0 or wrong_side:
                raise ValueError(
                    f"exit leg #{i} ({leg.kind!r}) resolved non-finite/non-positive/not-off-reference "
                    f"limit_price={limit_price!r} from ref={ref_price!r}, pct={leg.pct!r}"
                )
            attachments.append(LimitAttachment(limit_price=limit_price))
            continue
        # Defense-in-depth: don't let a non-LIMIT kind fall through to the
        # STOP-family math below by implication. ``ExitLegSpec`` already
        # restricts ``kind`` to this set at construction, but the resolver
        # shouldn't rely solely on that upstream guarantee — an unrecognized
        # kind here must fail loudly, not be silently treated as a stop leg.
        if leg.kind not in (OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP):
            raise ValueError(f"exit leg #{i} has unsupported kind {leg.kind!r}")
        stop_price = protective_stop_price(ref_price, leg.pct, is_long=is_long)
        wrong_side = stop_price >= ref_price if is_long else stop_price <= ref_price
        if not math.isfinite(stop_price) or stop_price <= 0 or wrong_side:
            raise ValueError(
                f"exit leg #{i} ({leg.kind!r}) resolved non-finite/non-positive/not-off-reference "
                f"stop_price={stop_price!r} from ref={ref_price!r}, pct={leg.pct!r}"
            )
        # ``limit_offset``/``trail_offset`` are each validated against their
        # own downstream combination (not just their own inputs) — see
        # ``_validated_stop_limit_offset``/``_validated_trail_offset`` for
        # the float64 rationale.
        is_trailing = leg.kind == OrderType.TRAILING_STOP
        limit_offset = (
            _validated_stop_limit_offset(leg, i, stop_price, is_long)
            if leg.kind == OrderType.STOP_LIMIT
            else None
        )
        trail_offset = _validated_trail_offset(leg, i, ref_price, is_long) if is_trailing else None
        attachments.append(
            StopAttachment(
                stop_price=stop_price,
                limit_offset=limit_offset,
                limit_offset_kind="abs",
                # ``trail_offset_kind`` is a don't-care whenever
                # ``trail_offset`` is None (trailing is disabled by the
                # ``None`` value itself); "abs" is kept for parity with the
                # legacy bracket attachment shape rather than left unset.
                trail_offset=trail_offset,
                trail_offset_kind="bps" if is_trailing else "abs",
            )
        )
    return attachments


def _as_bracket_attachment_pair(
    attachments: List[Union[StopAttachment, LimitAttachment]],
) -> Tuple[StopAttachment, LimitAttachment]:
    """Narrow a generalized attachment list to the bracket-specific
    ``(StopAttachment, LimitAttachment)`` pair.

    ``_bracket_to_leg_specs`` always produces exactly ``[stop_leg,
    target_leg]``, so ``resolve_exit_leg_attachments`` always resolves them
    to ``[StopAttachment, LimitAttachment]`` — but that invariant lives in
    ``_bracket_to_leg_specs``, not in the generic list return type, so
    static typing alone can't prove it here. Verifying it explicitly (and
    failing loudly if it's ever violated) is preferable to a bare ``#
    type: ignore``, which would silently trust the invariant forever,
    including if a future change to ``_bracket_to_leg_specs`` broke it.

    Preconditions: ``attachments`` is the result of resolving a
    ``_bracket_to_leg_specs``-produced leg list.
    Postconditions: returns ``(attachments[0], attachments[1])`` narrowed to
    ``(StopAttachment, LimitAttachment)``.
    Raises:
        TypeError: if ``attachments`` is not exactly a two-element
            ``(StopAttachment, LimitAttachment)`` pair. Checked explicitly
            (rather than relying on tuple/list unpacking's own arity check)
            because unpacking a wrong-length sequence raises ``ValueError``,
            not ``TypeError`` — this function's contract is "wrong shape is
            always a ``TypeError``", covering both wrong length and wrong
            element types with one exception type.
    """
    if len(attachments) != 2:
        raise TypeError(
            f"bracket attachment resolution must produce exactly 2 attachments, got {len(attachments)}"
        )
    stop_attachment, limit_attachment = attachments
    if not isinstance(stop_attachment, StopAttachment) or not isinstance(
        limit_attachment, LimitAttachment
    ):
        raise TypeError(
            "bracket attachment resolution must produce (StopAttachment, LimitAttachment); "
            f"got ({type(stop_attachment).__name__}, {type(limit_attachment).__name__})"
        )
    return stop_attachment, limit_attachment


def _bracket_to_leg_specs(bracket: OcoBracketRule) -> List[ExitLegSpec]:
    """Translate an OCO bracket's two fixed legs into generic exit-leg specs.

    Preconditions: ``bracket`` is a validated :class:`OcoBracketRule`.
    Postconditions: returns ``[stop_leg, target_leg]`` — the stop leg first
    (``STOP_LIMIT`` when ``bracket.stop_loss.style == "limit"``, else
    ``STOP``), the take-profit leg second (``LIMIT``) — matching the order
    :func:`resolve_bracket_attachments` has always returned them in.
    """
    stop_kind = OrderType.STOP_LIMIT if bracket.stop_loss.style == "limit" else OrderType.STOP
    return [
        ExitLegSpec(
            kind=stop_kind,
            pct=bracket.stop_loss.pct,
            limit_offset_pct=bracket.stop_loss.limit_offset_pct
            if stop_kind == OrderType.STOP_LIMIT
            else None,
        ),
        ExitLegSpec(kind=OrderType.LIMIT, pct=bracket.take_profit.pct),
    ]


def resolve_bracket_attachments(
    bracket: OcoBracketRule, side: OrderSide, ref_price: float
) -> Tuple[StopAttachment, LimitAttachment]:
    """Resolve an OCO bracket's percentage legs into entry-order attachments.

    Thin adapter over the generalized :func:`resolve_exit_leg_attachments`:
    translates the bracket's fixed (stop, target) legs into
    :class:`ExitLegSpec`\\ s via :func:`_bracket_to_leg_specs` and unpacks the
    two-element result back into a tuple, preserving this function's
    original signature and price math byte-for-byte. See
    :func:`resolve_exit_leg_attachments` for the shared price-resolution
    contract (anchoring, sign convention, limit-offset handling, and raises).

    Preconditions: ``ref_price`` is a finite number ``> 0``; ``side`` is the
    entry's ``OrderSide``; ``bracket`` is a validated :class:`OcoBracketRule`
    (its leg ``pct`` values lie in ``(0, 1)``).
    Postconditions: returns a ``(StopAttachment, LimitAttachment)`` pair whose
    absolute prices are finite, strictly positive, and strictly on the
    correct side of ``ref_price``; a ``limit``-style stop leg additionally
    carries a ``limit_offset`` that is finite, strictly positive, and large
    enough to survive being added to or subtracted from ``stop_price``.
    Raises:
        ValueError: if ``ref_price`` is non-finite or ``<= 0``, or if a
            resolved ``stop_price``/``limit_price``/``limit_offset`` is
            non-finite, non-positive, not strictly on the correct side of
            ``ref_price``, or (for ``limit_offset``) too small to survive
            its downstream arithmetic.
    """
    attachments = resolve_exit_leg_attachments(_bracket_to_leg_specs(bracket), side, ref_price)
    return _as_bracket_attachment_pair(attachments)


# The only ``StopLossRule.basis`` value ``_is_resting_stop_loss`` admits;
# shared with the ``engine_exit_attached`` branch of
# ``_apply_fill_outcome_events`` so the basis-label literal has one home.
_RESTING_STOP_LOSS_BASIS = "entry_price"


def _is_resting_stop_loss(rule: Any) -> bool:
    """Return True for the resting-eligible stop-loss variant migrated here.

    Eligible only for ``StopLossRule(basis="entry_price", style="market")``
    with ``0 < pct < 1.0`` — the open upper bound matches ``ExitLegSpec.pct``'s
    own ``(0, 1)`` constraint. That bound also excludes, by construction, the
    ``pct=1.0`` short-safety auto-stop ``TradingService.__init__`` injects
    when a spec allows shorts with no explicit stop covering them: that rule
    is a deliberate no-op for longs (``entry * (1 - 1.0) == 0``) and would
    fail ``ExitLegSpec``'s strict upper bound if fed through this path.
    Excluding it leaves it exactly as it behaves today — bar-close-only —
    rather than crashing every long entry on a spec where shorts are
    possible.
    """
    return (
        isinstance(rule, StopLossRule)
        and rule.basis == _RESTING_STOP_LOSS_BASIS
        and rule.style == "market"
        and 0.0 < rule.pct < 1.0
    )


# Env var gating which of the two mechanisms handles the resting-eligible
# entry_price/market stop-loss variant for a run — see
# ``_resting_stop_loss_enabled`` for the default and
# ``_first_resting_stop_loss_index`` for how the two mechanisms are kept
# mutually exclusive once this selects the resting path.
_STOP_LOSS_RESTING_ORDER_ENV = "STOP_LOSS_RESTING_ORDER_ENABLED"


def _resting_stop_loss_enabled() -> bool:
    """Whether this run submits the resting-eligible entry_price/market
    stop-loss variant as a resting ``STOP`` order (the #7495 migration)
    instead of leaving it to the legacy bar-close evaluator.

    Defaults to ``False`` — the bar-close evaluator (``_EngineExitDispatcher``
    / ``rule_compiler.stop_loss_level``) is the long-established mechanism
    every existing spec already runs against, including the vast majority of
    ``StopLossRule`` usage across the test suite, which relies on that
    rule's own defaults (``basis="entry_price"``, ``style="market"``) and so
    is resting-eligible by construction. The resting-order path stays
    opt-in, via ``STOP_LOSS_RESTING_ORDER_ENABLED=true``, until the
    migration's remaining variants exist and the bar-close detection path is
    removed for good — a run that sets no explicit configuration keeps
    today's bar-close behavior unchanged.

    The two mechanisms are mutually exclusive for the affected rule by
    construction, not by convention: see ``_first_resting_stop_loss_index``
    (the single source of "which ``exit_rules`` entry this migration step
    affects") and its two call sites — ``_EngineEntryDispatcher.__post_init__``
    attaches the resting order only when this returns ``True``, and
    ``TradingService.run`` excludes that same rule index from
    ``_EngineExitDispatcher``'s bar-close evaluation only when this returns
    ``True`` — so a bar can never see both a resting-stop fill and a
    bar-close exit for the same rule.
    """
    return os.environ.get(_STOP_LOSS_RESTING_ORDER_ENV, "false").strip().lower() in {
        "true",
        "1",
        "yes",
    }


def _first_resting_stop_loss_index(exit_rules: Sequence[Any]) -> Optional[int]:
    """Index of the first resting-eligible ``StopLossRule`` in ``exit_rules``.

    Single source of "which rule this migration step affects" — shared by
    ``_EngineEntryDispatcher.__post_init__`` (which rule to resolve a resting
    attachment for) and ``TradingService.run`` (which rule index to exclude
    from the bar-close evaluator), so the two can never pick different rules
    when a spec carries more than one resting-eligible ``StopLossRule``
    (unusual, but not DSL-forbidden) — both mirror ``first_side_stop_factor``'s
    spec-order "first wins" precedent by construction, since both scan the
    same list with the same predicate.

    Postconditions: returns the lowest ``i`` such that
    ``_is_resting_stop_loss(exit_rules[i])``, or ``None`` if no rule
    qualifies.
    """
    return next((i for i, r in enumerate(exit_rules) if _is_resting_stop_loss(r)), None)


def _stop_loss_rule_to_leg_specs(rule: StopLossRule) -> List[ExitLegSpec]:
    """Translate a resting-eligible ``StopLossRule`` into a generic exit leg.

    Preconditions: ``_is_resting_stop_loss(rule)`` is True.
    Postconditions: returns ``[ExitLegSpec(kind=STOP, pct=rule.pct)]`` —
    the same shape :func:`_bracket_to_leg_specs` builds for a market-style
    bracket stop leg, so :func:`resolve_exit_leg_attachments` resolves
    identical price math for both (see ``rule_compiler.stop_loss_level``,
    which this mirrors: ``ref_price * (1 ∓ pct)``).
    """
    # Explicit raise (not assert, which ``python -O`` strips) so the contract
    # stays enforced in optimized production runs — the same posture
    # ``resolve_exit_leg_attachments`` documents for its own preconditions.
    # ``rule!r`` (not its individual attributes) so the message itself can't
    # raise on a non-StopLossRule input (the case ``_is_resting_stop_loss``'s
    # isinstance check exists to catch), where ``rule.basis``/``rule.style``
    # may not exist at all.
    if not _is_resting_stop_loss(rule):
        raise ValueError(
            "_stop_loss_rule_to_leg_specs requires a resting-eligible StopLossRule "
            f"(basis='entry_price', style='market', 0 < pct < 1.0); got {rule!r}"
        )
    return [ExitLegSpec(kind=OrderType.STOP, pct=rule.pct)]


def resolve_resting_stop_loss_attachment(
    rule: StopLossRule, side: OrderSide, ref_price: float
) -> StopAttachment:
    """Resolve a resting-eligible ``StopLossRule`` into an entry-order attachment.

    Thin adapter over the generalized :func:`resolve_exit_leg_attachments`,
    parallel to :func:`resolve_bracket_attachments`: translates the rule
    into a single :class:`ExitLegSpec` via :func:`_stop_loss_rule_to_leg_specs`
    and unwraps the one-element result. See
    :func:`resolve_exit_leg_attachments` for the shared price-resolution
    contract (anchoring, sign convention, and raises).

    Preconditions: ``_is_resting_stop_loss(rule)`` is True; ``ref_price`` is
    a finite number ``> 0``; ``side`` is the entry's ``OrderSide``.
    Postconditions: returns a :class:`StopAttachment` whose ``stop_price``
    is finite, strictly positive, and strictly on the protective side of
    ``ref_price`` (a preview only — see ``entry_price_pct`` below), and
    whose ``entry_price_pct == rule.pct`` so materialization re-anchors
    ``stop_price`` to the entry's actual fill price rather than trusting
    this ``ref_price``-anchored preview verbatim (``ref_price`` is the
    signal bar's close, which can gap away from where the entry actually
    fills — see :class:`StopAttachment`'s ``entry_price_pct`` field for why
    that matters here specifically). Also carries
    ``reason == ENGINE_EXIT_REASON_STOP_LOSS`` — the same named constant
    :meth:`_EngineExitDispatcher._build_close_order` stamps for a
    ``StopLossRule`` close on the bar-close path — so materialization (see
    :class:`StopAttachment`'s ``reason`` field) tags the resting fill with
    the same, gate-relied-upon attribution regardless of which path actually
    closes the position, instead of the generic ``exit_leg_{idx}`` label the
    rule-agnostic ``attached_exits`` plumbing would otherwise derive.

    Raises:
        ValueError: if ``rule`` is not resting-eligible (via
            :func:`_stop_loss_rule_to_leg_specs`), or per
            :func:`resolve_exit_leg_attachments` for an invalid
            ``ref_price``/``side`` or an unresolvable leg.
        TypeError: defense-in-depth if the single resolved attachment is
            not a :class:`StopAttachment` (unreachable for a STOP-kind leg).
    """
    attachments = resolve_exit_leg_attachments(_stop_loss_rule_to_leg_specs(rule), side, ref_price)
    (attachment,) = attachments
    # Explicit raise (not assert) so this stays enforced under python -O, matching
    # this file's stated posture — unreachable in practice since a STOP-kind leg
    # always resolves to a StopAttachment and the tuple-unpack above already
    # raises on any length mismatch.
    if not isinstance(attachment, StopAttachment):
        raise TypeError(f"expected StopAttachment for STOP leg, got {attachment!r}")
    # Immutable-style update (not a post-construction mutation) so the
    # attachment's final shape is established in one step; StopAttachment is a
    # Pydantic BaseModel, so model_copy (not dataclasses.replace) is the
    # correct mechanism here.
    return attachment.model_copy(
        update={
            "entry_price_pct": rule.pct,
            "reason": ENGINE_EXIT_REASON_STOP_LOSS,
        }
    )


@dataclass
class _EngineEntryDispatcher:
    """Per-run owner of engine-side entry-rule enforcement.

    Parallel to :class:`_EngineExitDispatcher`.  Evaluates structured
    entry predicates deterministically using a per-symbol
    :class:`StreamingHistoryView` and auto-submits entry orders with
    spec-derived sizing when a predicate fires.

    Empty ``entry_rules`` (or ``None`` sizing) makes
    :meth:`maybe_emit` a no-op — the strategy subprocess handles
    entries via its ``on_bar`` code as before.
    """

    entry_rules: Sequence[EntryRule]
    sizing: Any
    exit_rules: Sequence[Any] = ()
    target_symbols: frozenset[str] = frozenset()
    #: Runtime risk limits. When set, ``_compute_qty`` clamps order sizes so
    #: vol-target / fixed-notional sizing never deploys past ``max_position_pct``
    #: (the deployed cap, which is also the per-trade loss cap). ``None`` (the
    #: default) disables the clamp — used by unit tests that exercise raw sizing.
    risk_limits: Optional[RiskLimits] = None
    #: Canonical asset class of the run. Determines whether ``_compute_qty``
    #: produces whole-share (equities) or fractional (crypto / forex) order
    #: sizes. Empty (the default, used by raw-sizing unit tests) is treated as
    #: whole-share.
    asset_class: str = ""
    #: Whether the resting-eligible entry_price/market ``StopLossRule`` (if
    #: any) is attached here as a resting ``STOP`` order. Defaults to
    #: ``_resting_stop_loss_enabled()`` (env-gated, off by default) so a
    #: dispatcher built without this argument matches the run's feature
    #: check; unit tests exercising this path directly override it explicitly.
    #: See that function's docstring for the full mutual-exclusion contract
    #: with ``_EngineExitDispatcher.exclude_rule_index``.
    resting_stop_loss_enabled: bool = field(default_factory=_resting_stop_loss_enabled)
    _next_seq: int = 0

    def __post_init__(self) -> None:
        # Derive the per-run sizing constants once: asset_class and sizing kind
        # are fixed at construction, so the fractional-venue flag and the
        # position-clamp gate never change across bars. The sizing hot loop
        # (``_compute_qty``) reads these cached values instead of re-normalizing
        # the asset class on every entry.
        self._fractional: bool = is_fractional_asset_class(self.asset_class)
        # Whether the dispatcher applies the runtime position clamp for this
        # sizing kind. EVERY engine sizing kind is clamped to ``max_position_pct``
        # at the sizing price so the cap is a true pre-entry sizing bound: we
        # decide how much capital may be deployed BEFORE placing the order, then
        # let the order fill like a real broker fill — a price gap between the
        # sizing bar and the fill bar may leave the realised position marginally
        # above the cap, and that is acceptable holding behaviour (post-fill
        # notional drift on committed shares), not a reason to drop the entry.
        # This also
        # gates ``risk_presized``: a clamped order tells ``RiskFilter.can_enter``
        # to skip the fill-time cap re-check (which would otherwise falsely reject
        # the gap). ``fixed_fraction`` deploys ``fraction`` of current equity, so
        # for a readiness-clean spec (``fraction <= max_position_pct``) the clamp
        # is a no-op; for a readiness-bypassing spec it now clamps here rather than
        # relying on a fill-time rejection.
        self._cap_position: bool = isinstance(
            self.sizing, (FixedFractionSizing, FixedNotionalSizing, VolatilityTargetSizing)
        )
        # At most one OCO bracket per spec (enforced by ``StrategySpec``); when
        # present it is attached to every entry order so the engine materializes
        # the protective stop / target as a resting OCO group on entry-fill. The
        # bracket legs are NOT bar-by-bar dispatcher exits (the exit evaluator
        # skips ``OcoBracketRule``), so this is the only place they take effect.
        self._bracket: Optional[OcoBracketRule] = next(
            (r for r in self.exit_rules if isinstance(r, OcoBracketRule)), None
        )
        # First resting-eligible entry_price/market StopLossRule (if any),
        # attached to every entry order the same way ``self._bracket`` is —
        # but ONLY when ``resting_stop_loss_enabled`` selects this mechanism
        # for the run (see ``_resting_stop_loss_enabled`` for the default and
        # the mutual-exclusion contract). ``None`` when the flag is off, so
        # ``_resting_stop_loss_attachments`` is a no-op and the rule is left
        # exactly as it behaves without this migration — bar-close-only.
        # ``_first_resting_stop_loss_index`` is the single source of "which
        # rule", shared with ``TradingService.run``'s exclusion of that same
        # index from ``_EngineExitDispatcher`` (see ``_is_resting_stop_loss``
        # for the eligibility bound and why the short-safety auto-stop is
        # deliberately excluded).
        #
        # UNLIKE ``self._bracket``, this rule is not unconditionally excluded
        # from the bar-by-bar exit evaluator at the ``rule_compiler``
        # chokepoint (``OcoBracketRule`` is, unconditionally, since a bracket
        # is never bar-close-evaluated regardless of any run configuration).
        # This rule kind CAN be bar-close-evaluated — via
        # ``rule_compiler._intent_for_rule`` — so the two mechanisms must be
        # kept mutually exclusive by construction instead: when this dispatcher
        # attaches the resting order for a rule, ``TradingService.run`` passes
        # that same rule's index as ``_EngineExitDispatcher.exclude_rule_index``,
        # so ``rule_compiler._filtered_intent_for_rule`` drops it before the
        # bar-close evaluator ever produces an intent for it. A bar can
        # therefore never see both a resting-stop fill and a bar-close exit
        # for the same rule — the exact failure this coexistence step exists
        # to prevent (closing the position twice / a duplicate exit trade).
        #
        # When the resting order IS attached, its firing credit is carried
        # independent of the (now-excluded) bar-close evaluator:
        # ``FillSimulator._materialize_stop_child`` emits its own
        # ``"engine_exit_attached"`` diagnostic event at materialization time
        # (``_apply_fill_outcome_events`` in this module translates it into
        # the same counters ``_record_emission`` bumps for the bar-close
        # evaluator), so ``exit_rule_conformance.py::_check_stop_loss`` never
        # sees a below-floor resting-stop trade with zero firing credit.
        # Materialization-time counting is one-directional-safe on its own:
        # an entry that gets a resting stop attached but never triggers
        # merely inflates the denominator without a corresponding trade.
        #
        # The resting child's ``stop_price`` is resolved here at
        # entry-EMISSION time off the signal bar's close (a preview that can
        # gap away from where the entry actually fills), but
        # ``FillSimulator._materialize_stop_child`` re-anchors it to the
        # entry's real fill price before submitting the child (see
        # ``StopAttachment.entry_price_pct``) — the same
        # ``entry_price * (1 ∓ pct)`` formula ``rule_compiler.stop_loss_level``
        # uses for the bar-close evaluator, so a spec that flips this flag
        # between runs sees the same stop level either way.
        resting_idx = (
            _first_resting_stop_loss_index(self.exit_rules)
            if self.resting_stop_loss_enabled
            else None
        )
        self._resting_stop_loss: Optional[StopLossRule] = (
            self.exit_rules[resting_idx] if resting_idx is not None else None
        )

    def maybe_emit(
        self,
        *,
        cur_bar,
        portfolio: Portfolio,
        pending_for_prev: List[OrderRequest],
        views: Dict[str, StreamingHistoryView],
        result: "TradingServiceResult",
    ) -> None:
        if not self.entry_rules or self.sizing is None:
            return
        if self.target_symbols and cur_bar.symbol not in self.target_symbols:
            return
        sym = cur_bar.symbol
        if portfolio.positions.get(sym) is not None:
            # Entry evaluation dropped because the symbol already has an open
            # position. Bump a counter AND record an event so a zero/sparse-trade
            # run driven by concurrency-limiting is distinguishable in the final
            # category/summary from a dead entry predicate ("no signal").
            result.execution_diagnostics.already_in_position_skips += 1
            _record_event(
                result.execution_diagnostics,
                "already_in_position_skip",
                timestamp=cur_bar.timestamp,
                symbol=sym,
                detail="entry evaluation skipped: symbol already has an open position",
            )
            return
        if any(
            req.symbol == sym and req.side in (OrderSide.LONG, OrderSide.SHORT)
            for req in pending_for_prev
        ):
            return
        view = views.get(sym)
        if view is None or view.length() == 0:
            return
        match = _evaluate_entry_rules_pred(self.entry_rules, view, view.length() - 1)
        if match is None:
            return
        rule, rule_idx = match
        qty = self._compute_qty(rule.side, cur_bar, portfolio, views)
        if qty <= 0:
            # A matched entry signal that risk-sizing reduced to zero — a sub-1
            # whole-share order whose one-share floor would push past
            # max_position_pct, or non-positive equity. Bump a counter AND record
            # an event so a zero-trade run driven by risk-capping is distinguishable
            # in the final category/summary from a dead entry predicate ("no signal").
            result.execution_diagnostics.risk_capped_entries += 1
            _record_event(
                result.execution_diagnostics,
                "risk_capped_skip",
                timestamp=cur_bar.timestamp,
                symbol=sym,
                side=rule.side,
                reason=f"{ENGINE_ENTRY_REASON_PREFIX}entry[{rule_idx}]",
                detail="entry sized to 0 by max_position_pct",
            )
            return
        self._next_seq += 1
        side = OrderSide.LONG if rule.side == "long" else OrderSide.SHORT
        attached_stop_loss, attached_take_profit = self._bracket_attachments(side, cur_bar.close)
        attached_exits = self._resting_stop_loss_attachments(side, cur_bar.close)
        req = OrderRequest(
            client_order_id=f"e_entry_{self._next_seq}",
            symbol=sym,
            side=side,
            qty=qty,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            attached_stop_loss=attached_stop_loss,
            attached_take_profit=attached_take_profit,
            attached_exits=attached_exits,
            reason=f"{ENGINE_ENTRY_REASON_PREFIX}entry[{rule_idx}]",
            # Every dispatcher-emitted order is clamped to ``max_position_pct`` at
            # the sizing price (see ``_cap_position``), so it is presized: this
            # tells ``RiskFilter.can_enter`` to skip the fill-time cap re-check,
            # which would otherwise falsely reject an order whose fill price gapped
            # above the sizing price. The cap is a pre-entry sizing bound, not a
            # fill-time gate. ``can_enter`` remains the sole cap enforcement point
            # only for custom-code orders, which bypass the dispatcher and leave
            # ``risk_presized`` False.
            risk_presized=self._cap_position,
        )
        try:
            req.validate_prices()
        except Exception as exc:
            logger.error(
                "engine-issued entry order failed validation (rule=%d symbol=%s): %s",
                rule_idx,
                sym,
                exc,
            )
            return
        pending_for_prev.append(req)
        diag = result.execution_diagnostics
        diag.orders_emitted += 1
        _record_event(
            diag,
            "emitted",
            timestamp=cur_bar.timestamp,
            symbol=sym,
            side=req.side.value,
            order_type=OrderType.MARKET.value,
            reason=req.reason,
        )

    def _bracket_attachments(
        self, side: OrderSide, ref_price: float
    ) -> Tuple[Optional[StopAttachment], Optional[LimitAttachment]]:
        """Resolve this run's OCO bracket (if any) into entry-order attachments.

        Builds the bracket's legs via :func:`_bracket_to_leg_specs` and
        resolves them through the generalized :func:`resolve_exit_leg_attachments`;
        returns ``(None, None)`` when the spec has no bracket. See that
        function for the price-resolution contract (anchoring, sign
        convention, limit-offset handling, and raises).
        """
        if self._bracket is None:
            return None, None
        # Deliberately calls the generalized resolver directly (the same
        # composition ``resolve_bracket_attachments`` wraps) rather than
        # delegating to that adapter: a later change will let this dispatcher
        # build its leg list from non-bracket exit rules too, at which point
        # ``_bracket_to_leg_specs(self._bracket)`` becomes one leg-list source
        # among several rather than the bracket adapter's fixed shape. Keeping
        # this call site on the generalized API now means that change won't
        # have to un-delegate it later. Any change to the bracket-to-leg-spec
        # translation still applies to both call sites, since both go through
        # ``_bracket_to_leg_specs``.
        attachments = resolve_exit_leg_attachments(
            _bracket_to_leg_specs(self._bracket), side, ref_price
        )
        return _as_bracket_attachment_pair(attachments)

    def _resting_stop_loss_attachments(
        self, side: OrderSide, ref_price: float
    ) -> List[StopAttachment]:
        """Resolve this run's resting-eligible ``StopLossRule`` (if any) into an ``attached_exits`` entry.

        Kept on ``attached_exits`` rather than the fixed ``attached_stop_loss``
        field so this mechanism can never collide with a bracket's stop leg —
        a spec carrying both an ``OcoBracketRule`` and a resting-eligible
        standalone ``StopLossRule`` (unusual, but not DSL-forbidden) attaches
        both without either overwriting the other.

        Preconditions: ``self._resting_stop_loss`` (set once in
        ``__post_init__``) is either ``None`` or a rule already verified
        resting-eligible by ``_is_resting_stop_loss`` — this method never
        re-checks eligibility itself. ``ref_price`` is a finite number ``> 0``;
        ``side`` is the entry's ``OrderSide``.
        Postconditions: returns ``[]`` when the spec has no resting-eligible
        stop-loss rule; otherwise a single-element list containing the
        resolved :class:`StopAttachment`.
        """
        if self._resting_stop_loss is None:
            return []
        return [resolve_resting_stop_loss_attachment(self._resting_stop_loss, side, ref_price)]

    def _compute_qty(
        self,
        side: str,
        cur_bar,
        portfolio: Portfolio,
        views: Dict[str, StreamingHistoryView],
    ) -> float:
        equity = portfolio.mark_to_market()
        close = cur_bar.close
        if close <= 0:
            return 0
        sizing = self.sizing
        # ``self._cap_position`` (precomputed) gates the runtime position clamp,
        # which runs for EVERY engine sizing kind so ``max_position_pct`` is a true
        # pre-entry sizing bound enforced once, at the sizing price, before the
        # order is placed. For ``fixed_fraction`` a readiness-clean spec
        # (``fraction <= max_position_pct``) makes the clamp a no-op since it
        # deploys exactly ``fraction`` of CURRENT equity; it still clamps a
        # readiness-bypassing spec here rather than leaving it to a fill-time
        # rejection. ``fixed_notional`` and ``volatility_target`` need the clamp
        # more actively — their fraction OF EQUITY drifts as equity moves (a fixed
        # dollar notional becomes a larger share of a shrunken account; vol-target
        # is data-dependent), so a check against initial capital can be breached on
        # later entries. ``max_position_pct`` is the only deployed cap — it is also
        # the per-trade loss cap (the deployed size is the most a trade can lose),
        # so there is no separate loss clamp.
        if isinstance(sizing, FixedFractionSizing):
            raw_qty = equity * float(sizing.fraction) / close
        elif isinstance(sizing, FixedNotionalSizing):
            raw_qty = float(sizing.notional_usd) / close
        elif isinstance(sizing, VolatilityTargetSizing):
            atr_ref = self._find_atr_ref()
            view = views.get(cur_bar.symbol)
            if view is None or view.length() == 0:
                atr_val = None
            else:
                atr_val = view.indicator(atr_ref, view.length() - 1)
            if atr_val is None or atr_val <= 0:
                # ATR not yet available (warmup / missing view): fall back to a
                # one-share probe, but still run it through the caps below so an
                # early bar cannot emit an unclamped order that breaches the
                # position cap or loss tolerance.
                raw_qty = 1.0
            else:
                raw_qty = equity * float(sizing.target_annual_vol) / (close * atr_val)
        else:
            raw_qty = 1.0

        qty = raw_qty
        if self._cap_position:
            qty = self._cap_qty_to_position(qty, equity=equity, close=close)
        if self._fractional:
            # Crypto / forex trade in fractional units, so a risk-capped sub-1
            # order is a valid trade — submit the fractional size as-is, with no
            # whole-share floor or skip. A cap that drove qty to ~0 is dropped.
            return qty if qty > 0.0 else 0.0
        return self._floor_or_skip_whole_share(qty, side=side, equity=equity, close=close)

    def _floor_or_skip_whole_share(
        self, qty: float, *, side: str, equity: float, close: float
    ) -> float:
        """Resolve a whole-share order size from a (possibly capped) ``qty``.

        ``qty >= 1`` floors down to ``int(qty)`` (always within the caps). A
        sub-1 ``qty`` cannot be submitted as a fraction on a whole-share venue;
        flooring up to one share is only safe when one share is itself within
        every active risk cap, so the position cap is re-probed at exactly one
        share and the entry is skipped (``0.0``) when it would clip below one.
        The probe runs even though ``_compute_qty`` already clamped ``qty``,
        because flooring a sub-1 order up to a whole share can re-cross
        ``max_position_pct`` (high price / low equity) and emit an order
        ``RiskFilter.can_enter`` would reject. With no caps the probe is a no-op
        and the legacy 1-share floor stands.

        ``side`` is accepted for call-site symmetry but does not affect the
        result — the position cap is side-independent.

        Preconditions: ``close`` > 0.
        Postconditions: returns ``0.0`` (skip) or a positive whole number of
        shares whose one-share floor respects every active cap.
        """
        if qty >= 1.0:
            return float(int(qty))
        one_share = self._cap_qty_to_position(1.0, equity=equity, close=close)
        return 1.0 if one_share >= 1.0 else 0.0

    def _trades_fractional(self) -> bool:
        """Whether this dispatcher's asset class trades in fractional units.

        Reads the flag derived once in ``__post_init__`` from the shared
        ``is_fractional_asset_class`` predicate (crypto / forex are fractional;
        equities / futures / commodities are whole-lot).
        """
        return self._fractional

    def _cap_qty_to_position(self, qty: float, *, equity: float, close: float) -> float:
        """Clamp ``qty`` so its notional does not exceed ``max_position_pct``.

        Preconditions: ``close`` > 0. Postconditions: returns ``qty`` unchanged
        when no risk limits are attached or ``qty`` <= 0; returns ``0.0`` when a
        cap is set but ``equity`` <= 0 (no capital to deploy — no positive size
        can satisfy a percent-of-equity cap); otherwise a share count whose
        notional is <= ``equity × max_position_pct%``.
        """
        limits = self.risk_limits
        if limits is None or qty <= 0:
            return qty
        if equity <= 0:
            # A percent-of-equity cap admits no positive position on a non-
            # positive account; computing equity*pct/close would yield a
            # negative max_qty (a broken clamp). Skip the entry instead.
            return 0.0
        max_qty = equity * float(limits.max_position_pct) / 100.0 / close
        return min(qty, max_qty)

    def _find_atr_ref(self):
        """Find an ATR IndicatorRef from the spec's entry or exit rules.

        Scans entry-rule predicates first, then signal-exit predicates,
        and returns the first ATR indicator so vol-target sizing uses the
        spec's configured period. Falls back to default ATR(14) when no
        ATR appears in any rule.
        """
        from ..strategy_lab.spec_dsl import (
            IndicatorRef,
            SignalExitRule,
            iter_tree_indicator_refs,
        )

        for rule in self.entry_rules:
            if not isinstance(rule, EntryRule):
                continue
            for side in iter_tree_indicator_refs(rule.when):
                if side.name == "atr":
                    return side
        for rule in self.exit_rules:
            if not isinstance(rule, SignalExitRule):
                continue
            for side in iter_tree_indicator_refs(rule.when):
                if side.name == "atr":
                    return side
        return IndicatorRef(name="atr")


# Default chunk size for the batched-bar protocol (issue #377). 1 keeps
# byte-identical behaviour with the per-bar codepath; values >1 only take
# effect when the strategy subprocess advertises ``chunked_bars`` in its
# first ready. Paper-trade mode pins this to 1 regardless of env.
_DEFAULT_BAR_CHUNK_SIZE = 1


def _resolve_bar_chunk_size() -> int:
    """Read ``BAR_CHUNK_SIZE`` from env, clamping to a positive int.

    Default 1 (per-bar mode). Values >1 enable the chunked protocol when
    the child advertises ``chunked_bars``. Invalid values fall back to
    the default with a logged warning so a typo doesn't silently force
    a 0-bar chunk that would deadlock the run loop.
    """
    raw = os.environ.get("BAR_CHUNK_SIZE")
    if raw is None or raw == "":
        return _DEFAULT_BAR_CHUNK_SIZE
    try:
        n = int(raw)
    except ValueError:
        logger.warning("invalid BAR_CHUNK_SIZE=%r; using default %d", raw, _DEFAULT_BAR_CHUNK_SIZE)
        return _DEFAULT_BAR_CHUNK_SIZE
    if n < 1:
        logger.warning(
            "BAR_CHUNK_SIZE=%d must be >= 1; using default %d", n, _DEFAULT_BAR_CHUNK_SIZE
        )
        return _DEFAULT_BAR_CHUNK_SIZE
    return n


def _partial_fill_defaults_enabled() -> bool:
    """Whether parent-side application of ``default_unfilled_policy`` is on.

    On by default since #386 (Step 4) wired ``REQUEUE_NEXT_BAR`` into
    ``FillSimulator``. Set ``TRADING_PARTIAL_FILL_DEFAULTS_ENABLED=false``
    to fall back to the pre-Step-4 behavior (silent drop of partial-fill
    remainders) — useful for parity comparisons against legacy snapshots.
    """
    return os.environ.get("TRADING_PARTIAL_FILL_DEFAULTS_ENABLED", "true").lower() in {
        "true",
        "1",
        "yes",
    }


@dataclass
class TradingServiceResult:
    trades: List[TradeRecord] = field(default_factory=list)
    terminated_reason: Optional[str] = None
    lookahead_violation: bool = False
    error: Optional[str] = None
    #: Orders the strategy tried to submit during a warm-up bar. These are
    #: dropped as a belt-and-suspenders guard — strategies should check
    #: ``ctx.is_warmup``. Populated only during paper-trade warm-up phase.
    warmup_orders_dropped: int = 0
    #: Number of non-warmup bars delivered to the strategy.  Phase 4's
    #: ``signals_per_bar`` diagnostic divides ``len(trades) / bars_processed``.
    #: Populated for every ``run`` regardless of data source (legacy
    #: pre-fetched vs provider-driven).
    bars_processed: int = 0
    execution_diagnostics: BacktestExecutionDiagnostics = field(
        default_factory=BacktestExecutionDiagnostics
    )
    #: Per-trading-day end-of-day mark-to-market equity, populated as the
    #: run progresses (#430). When non-empty at end-of-stream, supplied to
    #: ``compute_performance_metrics`` so it can skip rebuilding the curve
    #: from the closed-trade ledger. ``None`` when no bars were processed
    #: (e.g. ``harness.send_start`` failure or empty stream).
    streaming_equity_curve: Optional[EquityCurve] = None
    #: Aggregated coverage-probe events from the strategy subprocess
    #: (#450). Populated only when the service was constructed with
    #: ``coverage_probe_mode=True`` *and* the child flushed a
    #: ``probe_event`` frame (currently emitted on clean ``end``).
    #: Shape: ``{"events": [{rule_id, hit_count, first_true_bar,
    #: last_true_bar}, ...], "truncated": bool}``.
    probe_events: Optional[Dict[str, Any]] = None
    #: Entry reasons from positions still open at end-of-stream. The
    #: rule-firing gate unions these with closed-trade entry_reasons so
    #: a rule whose only firing left an unclosed position is not
    #: misreported as dead code.
    open_position_entry_reasons: List[str] = field(default_factory=list)


def _record_event(
    diagnostics: BacktestExecutionDiagnostics,
    event_type: str,
    *,
    timestamp: Optional[str] = None,
    symbol: Optional[str] = None,
    side: Optional[str] = None,
    order_type: Optional[str] = None,
    reason: str = "",
    detail: str = "",
) -> None:
    diagnostics.last_order_events.append(
        OrderLifecycleEvent(
            event_type=event_type,
            timestamp=timestamp,
            symbol=symbol,
            side=side,
            order_type=order_type,
            reason=reason,
            detail=detail,
        )
    )
    if len(diagnostics.last_order_events) > _MAX_ORDER_EVENTS:
        del diagnostics.last_order_events[:-_MAX_ORDER_EVENTS]


def _increment_rejection(diagnostics: BacktestExecutionDiagnostics, reason: str) -> None:
    reason_key = reason or "unknown"
    diagnostics.orders_rejected += 1
    diagnostics.orders_rejection_reasons[reason_key] = (
        diagnostics.orders_rejection_reasons.get(reason_key, 0) + 1
    )


def _apply_fill_outcome_events(
    diagnostics: BacktestExecutionDiagnostics, outcome: FillOutcome
) -> None:
    """Drain ``FillSimulator``-side lifecycle/rejection events into diagnostics.

    Called once per ``process_bar`` in both per-bar and chunked run loops.
    Translates fill-simulator events (#410) into:

    - ``entry_filled`` lifecycle events + ``entries_filled`` counter bumps;
    - ``exit_filled`` lifecycle events;
    - ``rejected`` events + ``orders_rejected`` / ``orders_rejection_reasons``
      bumps for fill-side rejections (``zero_fill_qty``,
      ``risk_gate:<reason>``, ``insufficient_capital``,
      ``same_side_order_ignored``);
    - ``engine_exit_attached`` — a resting stop-loss leg materialized at
      entry-fill — into ``exit_rule_firings`` / ``exit_rule_firings_by_symbol``
      / ``exit_rule_firings_by_basis``, mirroring ``_record_emission``.

    Fill-side rejections happen *after* the order was accepted, so they
    don't decrement ``orders_accepted``. ``_finalize_diagnostics`` already
    gates the ``ORDERS_REJECTED`` zero-trade category on
    ``orders_accepted == 0``, so this won't mis-classify an SMA round-trip
    that hit a single same-side rejection along the way.
    """
    for ev in outcome.diagnostic_events:
        if ev.kind == "entry_filled":
            diagnostics.entries_filled += 1
            _record_event(
                diagnostics,
                "entry_filled",
                timestamp=ev.timestamp,
                symbol=ev.symbol,
                side=ev.side,
                order_type=ev.order_type,
                reason=ev.reason,
                detail=ev.detail,
            )
        elif ev.kind == "exit_filled":
            _record_event(
                diagnostics,
                "exit_filled",
                timestamp=ev.timestamp,
                symbol=ev.symbol,
                side=ev.side,
                order_type=ev.order_type,
                reason=ev.reason,
                detail=ev.detail,
            )
        elif ev.kind == "rejected":
            _increment_rejection(diagnostics, ev.reason)
            _record_event(
                diagnostics,
                "rejected",
                timestamp=ev.timestamp,
                symbol=ev.symbol,
                side=ev.side,
                order_type=ev.order_type,
                reason=ev.reason,
                detail=ev.detail,
            )
        elif ev.kind == "stop_limit_unfilled":
            # A stop-limit triggered (stop crossed) but gapped through its
            # limit, so it could not fill this bar and stays resting — the
            # position remains open. Informational telemetry only; not a
            # rejection (the order is still live) and not a leak.
            diagnostics.stop_limit_unfilled_triggers += 1
            _record_event(
                diagnostics,
                "stop_limit_unfilled",
                timestamp=ev.timestamp,
                symbol=ev.symbol,
                side=ev.side,
                order_type=ev.order_type,
                reason=ev.reason,
                detail=ev.detail,
            )
        elif ev.kind == "engine_exit_filled":
            # An engine-SUBMITTED exit order actually filled and closed a
            # position (``ev.reason`` is its un-reconciled ``engine_exit:<kind>``
            # reason). This counts only true engine fills — distinct from the
            # reconciled ``exit_reason`` on closed trades, which can relabel a
            # *strategy* close as ``engine_exit:*`` and would otherwise inflate
            # the fire-vs-fill telemetry (hiding a stop-limit that never
            # executed). Distinct, too, from emission-time ``exit_rule_firings``:
            # a limit-style stop can fire (emit) yet gap through unfilled, in
            # which case no engine fill is recorded here.
            # ``signal_exit`` stamps a ``[idx]`` suffix; ``_engine_exit_kind`` strips
            # the prefix and that suffix so the fill key matches the firing key.
            kind = _engine_exit_kind(ev.reason)
            diagnostics.exit_rule_fills[kind] = diagnostics.exit_rule_fills.get(kind, 0) + 1
            sym_fills = diagnostics.exit_rule_fills_by_symbol.setdefault(ev.symbol, {})
            sym_fills[kind] = sym_fills.get(kind, 0) + 1
        elif ev.kind == "engine_exit_attached":
            # A resting stop-loss leg (the entry_price/market migration's
            # exclusive marker — see ``FillSimulator._materialize_stop_child``)
            # was attached at entry-fill time. The bar-close evaluator's own
            # ``_record_emission`` never sees this leg (it has zero
            # visibility into resting orders — see the transitional-state
            # comment on ``_EngineEntryDispatcher.__post_init__`` below), so
            # without this branch a resting-order-only close would carry
            # zero firing credit and could trip
            # ``exit_rule_conformance.py::_check_stop_loss``'s below-floor
            # leak check as a false positive. Mirrors ``_record_emission``'s
            # per-symbol increment shape exactly, just triggered by
            # materialization instead of bar-close emission — safe even
            # when the bar-close evaluator *also* fires on the same bar
            # (a known, deliberate double-count; an inflated firing count
            # can only make the leak check more lenient, never trip a false
            # critical).
            kind = _engine_exit_kind(ev.reason)
            diagnostics.exit_rule_firings[kind] = diagnostics.exit_rule_firings.get(kind, 0) + 1
            sym_firings = diagnostics.exit_rule_firings_by_symbol.setdefault(ev.symbol, {})
            sym_firings[kind] = sym_firings.get(kind, 0) + 1
            # "{kind}:{basis}" composite key, matching _record_emission's own
            # basis_label format above. Additive telemetry only — surfaced by
            # exit_rule_conformance.py's diagnostics summary, not read by
            # _check_stop_loss's leak-check reconciliation (which keys off
            # exit_rule_firings_by_symbol instead).
            basis_label = f"{kind}:{_RESTING_STOP_LOSS_BASIS}"
            diagnostics.exit_rule_firings_by_basis[basis_label] = (
                diagnostics.exit_rule_firings_by_basis.get(basis_label, 0) + 1
            )


class _StreamingEquityBuffer:
    """Preallocated NumPy buffer for the streaming EOD-equity curve (#378).

    Replaces the old ``Dict[date, float]`` accumulator with a fixed-size
    ``np.ndarray`` indexed by the same weekday set that
    :func:`build_equity_curve_from_trades` uses, so the streaming curve
    and the reconstructed-from-trades curve align on every trading day.

    Sub-daily bars overwrite the same slot, so the last MTM of each
    trading day wins — matching the previous dict-based contract.

    An ``overflow`` dict catches days outside the preallocated range
    (paper-trade runs that extend past ``config.end_date``, weekend
    crypto bars inside the configured window, or runs where
    ``start_date == end_date`` falls on a weekend). At materialization
    time the in-range slice and the overflow tail are merged into a
    single chronologically sorted curve so ``compute_performance_metrics``
    sees adjacent ``(date, equity)`` pairs in time order.
    """

    __slots__ = (
        "_equity",
        "_dates",
        "_index_by_date",
        "_filled_indices",
        "_seen_indices",
        "_initial_capital",
        "_overflow",
    )

    def __init__(self, expected_days: List[date_cls], initial_capital: float) -> None:
        self._dates: List[date_cls] = expected_days
        self._equity: np.ndarray = np.empty(len(expected_days), dtype=np.float64)
        self._index_by_date: Dict[date_cls, int] = {d: i for i, d in enumerate(expected_days)}
        # Insertion-ordered (bars arrive chronologically), so no sort
        # needed at materialize time for the preallocated slice.
        self._filled_indices: List[int] = []
        self._seen_indices: set[int] = set()
        self._initial_capital: float = initial_capital
        self._overflow: Dict[date_cls, float] = {}

    def record(self, bar_timestamp: str, equity: float) -> None:
        day = date_cls.fromisoformat(bar_timestamp[:10])
        idx = self._index_by_date.get(day)
        if idx is None:
            # Outside the preallocated range (e.g. live paper-trade past
            # ``end_date``, weekend crypto bars). Falls back to a dict
            # tail; merged back into chronological order at materialize
            # time. Correctness over perf on the rare overflow path.
            self._overflow[day] = equity
            return
        if idx not in self._seen_indices:
            self._filled_indices.append(idx)
            self._seen_indices.add(idx)
        self._equity[idx] = equity

    def materialize(self) -> Optional[EquityCurve]:
        if not self._filled_indices and not self._overflow:
            return None
        # Materialize covers every preallocated weekday plus every
        # overflow date (weekend bars, paper-trade days past
        # ``end_date``). Forward-fill must operate over the *merged*
        # chronological sequence: a weekend overflow bar that updates
        # equity between two weekdays has to propagate into a
        # following gap weekday, otherwise the curve moves backward
        # at the sort step (regression caught by
        # ``test_streaming_buffer_overflow_carry_propagates_to_gap_weekday``).
        explicit: Dict[date_cls, float] = {
            self._dates[i]: float(self._equity[i]) for i in self._filled_indices
        }
        if self._overflow:
            explicit.update(self._overflow)
        all_dates = sorted(set(self._dates) | explicit.keys())
        dates: List[date_cls] = []
        equity: List[float] = []
        carry = self._initial_capital
        for d in all_dates:
            if d in explicit:
                carry = explicit[d]
            dates.append(d)
            equity.append(carry)
        return EquityCurve(
            dates=dates,
            equity=equity,
            initial_capital=self._initial_capital,
        )


def _finalize_diagnostics(result: TradingServiceResult) -> TradingServiceResult:
    diagnostics = result.execution_diagnostics
    diagnostics.bars_processed = result.bars_processed
    diagnostics.warmup_orders_dropped = result.warmup_orders_dropped
    diagnostics.closed_trades = len(result.trades)

    if diagnostics.closed_trades > 0:
        diagnostics.zero_trade_category = None
        diagnostics.summary = (
            f"Backtest closed {diagnostics.closed_trades} trade(s) "
            f"across {diagnostics.bars_processed} post-warmup bar(s)."
        )
        return result

    # An aborted run (subprocess crash, look-ahead violation, etc.) doesn't
    # let the lifecycle counters speak for the strategy's intent — preserve
    # the unknown category so callers don't misread a partial counter set
    # as a clean zero-trade signal. Refinement-loop callers see the
    # ``error``/``lookahead_violation`` fields on ``TradingServiceResult``
    # for the actual failure mode.
    if result.error is not None:
        diagnostics.zero_trade_category = "UNKNOWN_ZERO_TRADE_PATH"
        diagnostics.summary = f"Backtest aborted before completion: {result.error}"
        return result

    # Zero-trade categorisation. Counters populated by the run loop drive the
    # category; the precedence below mirrors the order in which the failure
    # would manifest along the strategy → submit → fill path.
    if diagnostics.orders_emitted == 0 and diagnostics.warmup_orders_dropped > 0:
        diagnostics.zero_trade_category = "ONLY_WARMUP_ORDERS"
        diagnostics.summary = (
            f"Backtest closed zero trades; dropped {diagnostics.warmup_orders_dropped} "
            f"warm-up order(s) across {diagnostics.bars_processed} post-warmup bar(s)."
        )
    elif diagnostics.orders_emitted == 0 and diagnostics.risk_capped_entries > 0:
        diagnostics.zero_trade_category = "ALL_ENTRIES_RISK_CAPPED"
        diagnostics.summary = (
            f"Backtest closed zero trades; {diagnostics.risk_capped_entries} matched "
            "entry signal(s) were sized to zero by max_position_pct across "
            f"{diagnostics.bars_processed} post-warmup "
            "bar(s) — risk sizing suppressed every entry, not a dead predicate."
        )
    elif diagnostics.orders_emitted == 0:
        diagnostics.zero_trade_category = "NO_ORDERS_EMITTED"
        diagnostics.summary = (
            f"Backtest closed zero trades; strategy emitted no orders across "
            f"{diagnostics.bars_processed} post-warmup bar(s)."
        )
    elif diagnostics.orders_rejected > 0 and diagnostics.orders_accepted == 0:
        reasons = ", ".join(
            f"{k}={v}" for k, v in sorted(diagnostics.orders_rejection_reasons.items())
        )
        diagnostics.zero_trade_category = "ORDERS_REJECTED"
        diagnostics.summary = (
            f"Backtest closed zero trades; all {diagnostics.orders_rejected} emitted "
            f"order(s) were rejected ({reasons or 'unknown'})."
        )
    elif diagnostics.orders_unfilled > 0 and diagnostics.entries_filled == 0:
        diagnostics.zero_trade_category = "ORDERS_UNFILLED"
        diagnostics.summary = (
            f"Backtest closed zero trades; {diagnostics.orders_unfilled} order(s) "
            "left unfilled with no entry fills recorded."
        )
    elif diagnostics.entries_filled > 0 and diagnostics.exits_emitted == 0:
        diagnostics.zero_trade_category = "ENTRY_WITH_NO_EXIT"
        diagnostics.summary = (
            f"Backtest closed zero trades; {diagnostics.entries_filled} entr(ies) "
            "filled but the strategy never emitted an exit order."
        )
    else:
        diagnostics.zero_trade_category = "UNKNOWN_ZERO_TRADE_PATH"
        diagnostics.summary = (
            f"Backtest closed zero trades across {diagnostics.bars_processed} "
            f"post-warmup bar(s); counters: emitted={diagnostics.orders_emitted}, "
            f"accepted={diagnostics.orders_accepted}, "
            f"rejected={diagnostics.orders_rejected}, "
            f"unfilled={diagnostics.orders_unfilled}, "
            f"entries_filled={diagnostics.entries_filled}, "
            f"exits_emitted={diagnostics.exits_emitted}."
        )

    return result


class TradingService:
    """One-shot driver that pipes a data stream through a strategy subprocess."""

    def __init__(
        self,
        *,
        strategy_code: str,
        config: BacktestConfig,
        risk_limits: Optional["RiskLimits | Dict"] = None,
        default_unfilled_policy: UnfilledPolicy = UnfilledPolicy.DROP,
        bar_chunk_size: Optional[int] = None,
        coverage_probe_mode: bool = False,
        exit_rules: Optional[List[ExitRule]] = None,
        entry_rules: Optional[List[EntryRule]] = None,
        sizing: Optional[Any] = None,
        target_symbols: Optional[List[str]] = None,
        asset_class: str = "",
    ) -> None:
        self.strategy_code = strategy_code
        self.config = config
        # Canonical asset class of the run, threaded to the engine dispatcher so
        # ``_compute_qty`` sizes crypto/forex fractionally and equities
        # whole-share. ``BacktestConfig`` carries no asset_class, so callers pass
        # it explicitly (from ``StrategySpec.asset_class`` / the paper config).
        self._asset_class = asset_class or ""
        # #450: opt-in coverage-probe mode. Off by default so all
        # existing callers keep the zero-overhead path.
        self._coverage_probe_mode = coverage_probe_mode
        # Phase 3: StrategySpec.risk_limits is now a validated RiskLimits
        # instance; keep accepting raw dicts for callers that haven't
        # migrated (the backtest API still carries a ``Dict[str, Any]`` at
        # the request boundary).
        if isinstance(risk_limits, RiskLimits):
            limits = risk_limits
        else:
            limits = RiskLimits.from_legacy_dict(risk_limits or {})
        self._risk = RiskFilter(limits)
        self._default_unfilled_policy = default_unfilled_policy
        # Issue #527 — structured exit rules the parent engine enforces after
        # each bar's strategy response. Empty list (or None) preserves the
        # legacy behaviour where strategy code is the only source of exits.
        self._exit_rules: List[ExitRule] = list(exit_rules or [])
        # Short-safety floor: a short can lose more than 100% of the deployed
        # capital (price can more than double), so the deployed-size cap
        # (``max_position_pct``) is only a true per-trade loss bound for a short
        # that has a stop. When a short can be opened and the spec declares no
        # stop the executor can fire for it, auto-inject a 100%-adverse-move stop
        # (``basis="entry_price"``, ``pct=1.0``) so the short exits at 2x entry —
        # bounding its modeled worst-case loss at the full deployed amount, like a
        # long. The readiness gate relies on this contract to pass uncovered
        # shorts. The "sides unknown, might short" signal is ``entry_rules is None``
        # — the custom-code path, where the mode layers pass ``entry_rules=None``
        # (``requires_custom_code``) and the subprocess may open shorts. A populated
        # list is enumerated for an explicit short side; an empty list (a no-trade
        # engine spec, or a strategy-code-driven spec that did not mark itself
        # custom-code) does NOT trigger injection, so its ``_exit_rules`` stays
        # empty and the chunked-bar fast path is not needlessly disabled. The rule
        # is a no-op for longs (entry_price/1.0 → long floor = 0, never fires), and
        # ``self._exit_rules`` is a fresh copy so this never mutates the caller's
        # list.
        shorts_possible = entry_rules is None or any(
            getattr(rule, "side", "long") == "short" for rule in entry_rules
        )
        # An ``oco_bracket`` only protects ENGINE-managed entries: the entry
        # dispatcher attaches its legs to engine-emitted orders, and on the
        # custom-code path (``entry_rules is None``) it never fires — the strategy
        # subprocess submits its own entries with no attachment, and the exit
        # evaluator also skips the bracket. So a bracket's stop leg must NOT
        # suppress the short-safety auto-stop on that path, or a custom-code short
        # under a bracket spec would run with no engine-enforced loss cap. Drop
        # brackets from the stop check when entries are not engine-managed; a real
        # ``StopLossRule`` still counts on either path.
        stop_check_rules = (
            self._exit_rules
            if entry_rules is not None
            else [r for r in self._exit_rules if not is_bracket_exit(r)]
        )
        if shorts_possible and first_side_stop_factor(stop_check_rules, "short") is None:
            self._exit_rules.append(StopLossRule(pct=1.0, basis="entry_price"))
        self._entry_rules: List[EntryRule] = list(entry_rules or [])
        self._sizing = sizing
        self._target_symbols: frozenset[str] = frozenset(target_symbols or ())
        # Issue #377: when set, overrides ``BAR_CHUNK_SIZE`` env. Paper-trade
        # mode pins this to 1 so live-bar handling never buffers. Reject
        # zero/negative or non-int explicitly so a future caller passing
        # garbage doesn't silently fall back to per-bar mode.
        if bar_chunk_size is not None:
            if isinstance(bar_chunk_size, bool) or not isinstance(bar_chunk_size, int):
                raise TypeError(
                    f"bar_chunk_size must be a positive int or None, "
                    f"got {type(bar_chunk_size).__name__} {bar_chunk_size!r}"
                )
            if bar_chunk_size < 1:
                raise ValueError(f"bar_chunk_size must be >= 1, got {bar_chunk_size!r}")
        self._chunk_size_override = bar_chunk_size

    # ------------------------------------------------------------------

    def run(
        self,
        stream: Iterable[StreamEvent],
        *,
        on_trade: Optional[Callable[[TradeRecord], None]] = None,
    ) -> TradingServiceResult:
        """Run the strategy against ``stream``.

        ``on_trade`` is invoked once per closed trade as they happen —
        used by paper-trade mode to read the running fill count inside
        its termination-check closure without peeking into service
        internals.
        """
        portfolio = Portfolio(initial_capital=self.config.initial_capital)
        order_book = OrderBook()
        # Issue #527 — per-position state the engine uses to evaluate
        # structured exit rules. Keyed by symbol; populated after each bar's
        # fills are processed. No effect when ``self._exit_rules`` is empty.
        position_tracker: Dict[str, _TrackedPosition] = {}
        # Issue #527 — owns engine-side exit-rule enforcement for this
        # run. Encapsulates the ``client_order_id → entry_order_id``
        # binding map (consumed by the submit step below), the sequence
        # counter, and the per-bar dispatch logic split across
        # :meth:`_EngineExitDispatcher.maybe_emit` sub-steps. No-op
        # when ``self._exit_rules`` is empty.
        streaming_views: Dict[str, StreamingHistoryView] = {}
        # Read once so both dispatchers below agree for the whole run — see
        # ``_resting_stop_loss_enabled`` for the default and
        # ``_first_resting_stop_loss_index`` for why the two dispatchers,
        # scanning the same ``self._exit_rules``, always pick the same rule.
        # When disabled (the default), ``exclude_rule_idx`` stays ``None`` so
        # ``_EngineExitDispatcher`` evaluates every rule exactly as it does
        # today.
        resting_stop_loss_enabled = _resting_stop_loss_enabled()
        exclude_rule_idx = (
            _first_resting_stop_loss_index(self._exit_rules) if resting_stop_loss_enabled else None
        )
        engine_exits = _EngineExitDispatcher(
            exit_rules=self._exit_rules,
            views=streaming_views,
            exclude_rule_index=exclude_rule_idx,
        )
        engine_entries = _EngineEntryDispatcher(
            entry_rules=self._entry_rules,
            sizing=self._sizing,
            exit_rules=self._exit_rules,
            target_symbols=self._target_symbols,
            risk_limits=self._risk.limits,
            asset_class=self._asset_class,
            resting_stop_loss_enabled=resting_stop_loss_enabled,
        )
        execution_model = build_execution_model(
            self.config.execution_model,
            participation_cap=self.config.fill_participation_cap,
        )
        fill_sim = FillSimulator(
            portfolio=portfolio,
            order_book=order_book,
            risk_filter=self._risk,
            config=FillSimulatorConfig(
                slippage_bps=self.config.slippage_bps,
                transaction_cost_bps=self.config.transaction_cost_bps,
            ),
            execution_model=execution_model,
            # Reconcile strategy-initiated closes that comply with a structured
            # exit rule back to ``engine_exit:<kind>`` attribution. ``None`` when
            # the run has no ``exit_rules`` (e.g. compiled strategies), leaving
            # the fill simulator's default behaviour unchanged. ``position_tracker``
            # is threaded so reconciliation mirrors the dispatcher's entry-bar skip.
            exit_reconciler=_build_exit_reconciler(
                self._exit_rules, streaming_views, position_tracker
            ),
        )

        result = TradingServiceResult()
        # #430/#378: per-trading-day EOD MTM equity, stamped from the run
        # loop's existing ``portfolio.mark_to_market()`` calls. The buffer
        # preallocates a NumPy slot for every weekday in
        # ``[start_date, end_date]`` so every return path materializes the
        # same date set; an overflow dict catches paper-trade runs that
        # extend past ``end_date``. Empty curve stays ``None``.
        eod_buffer = _StreamingEquityBuffer(
            weekday_range(
                date_cls.fromisoformat(self.config.start_date),
                date_cls.fromisoformat(self.config.end_date),
            ),
            self.config.initial_capital,
        )

        chunk_size = self._chunk_size_override
        if chunk_size is None:
            chunk_size = _resolve_bar_chunk_size()

        with StreamingHarness(
            self.strategy_code,
            coverage_probe_mode=self._coverage_probe_mode,
        ) as harness:
            try:
                harness.send_start(
                    config={
                        "initial_capital": self.config.initial_capital,
                        "transaction_cost_bps": self.config.transaction_cost_bps,
                        "slippage_bps": self.config.slippage_bps,
                    }
                )
            except StrategyRuntimeError as exc:
                result.error = str(exc)
                result.lookahead_violation = exc.etype == "lookahead_violation"
                result.streaming_equity_curve = eod_buffer.materialize()
                result.probe_events = harness.probe_events
                return _finalize_diagnostics(result)

            # Issue #377: chunked-bar protocol. Only opt in when the env var
            # asked for a chunk size > 1 *and* the child negotiated
            # ``chunked_bars`` in its first ready. Falling back to per-bar
            # silently keeps older child builds correct; a single warning
            # tells operators the chunked path was requested but skipped.
            use_chunked = chunk_size > 1 and harness.supports_chunked_bars
            if chunk_size > 1 and not harness.supports_chunked_bars:
                logger.warning(
                    "BAR_CHUNK_SIZE=%d requested but strategy subprocess did not "
                    "advertise chunked_bars; falling back to per-bar protocol",
                    chunk_size,
                )

            # Issue #527 — engine-side exit-rule enforcement is wired
            # into the per-bar path only. The chunked path delivers
            # multiple bars per strategy round-trip; emitting synthetic
            # closes mid-chunk would require restructuring the rule
            # evaluator to run inside the chunk replay, which is out of
            # scope for the MVP. Rather than crashing
            # ``run_backtest`` for any spec with exit rules whenever
            # ``BAR_CHUNK_SIZE`` is set globally, fall back to per-bar
            # mode for this run with a single ``logger.warning``: the
            # caller asked for chunking, but enforcement is the more
            # important guarantee.
            if use_chunked and self._exit_rules:
                logger.warning(
                    "BAR_CHUNK_SIZE=%d requested but TradingService.exit_rules "
                    "is non-empty; engine-side rule enforcement requires the "
                    "per-bar protocol — falling back to BAR_CHUNK_SIZE=1 for "
                    "this run. Set bar_chunk_size=1 explicitly to suppress "
                    "this warning.",
                    chunk_size,
                )
                use_chunked = False

            if use_chunked:
                return self._run_chunked(
                    stream=stream,
                    harness=harness,
                    portfolio=portfolio,
                    order_book=order_book,
                    fill_sim=fill_sim,
                    result=result,
                    chunk_size=chunk_size,
                    on_trade=on_trade,
                    eod_buffer=eod_buffer,
                    position_tracker=position_tracker,
                    engine_exits=engine_exits,
                    engine_entries=engine_entries,
                    streaming_views=streaming_views,
                )

            # We need one-bar lookahead in the fill simulator, so we buffer
            # the next bar. The strategy sees bar N; the fill simulator uses
            # bar N+1 to decide fills for orders submitted after bar N.
            #
            # Issue #248: the realistic execution model also wants a
            # one-bar **forward** view (bar N+2) to compute the
            # adverse-selection haircut on limit fills. We get that by
            # peeking one event ahead via ``_peeked``.
            prev_bar = None  # the bar the strategy most recently saw
            pending_for_prev: List[OrderRequest] = []
            event_iter = iter(stream)
            peeked: Optional[StreamEvent] = None

            try:
                while True:
                    if peeked is not None:
                        event = peeked
                        peeked = None
                    else:
                        event = next(event_iter, None)
                    if event is None or isinstance(event, EndOfStreamEvent):
                        break
                    if not isinstance(event, BarEvent):
                        continue
                    cur_bar = event.bar
                    is_warmup = event.is_warmup

                    # Peek the next bar event for the fill simulator's
                    # lookahead (used by realistic execution model). In
                    # multi-symbol streams the very next ``BarEvent`` may
                    # belong to a different symbol — ``HistoricalReplayStream``
                    # interleaves bars chronologically — so we only set
                    # ``next_bar`` when the peeked bar is the same symbol.
                    # Otherwise the realistic model would compute symbol A's
                    # adverse-selection haircut against symbol B's price
                    # move, corrupting fills. The peeked event is preserved
                    # for the next loop iteration regardless.
                    next_bar = None
                    while True:
                        peeked = next(event_iter, None)
                        if peeked is None or isinstance(peeked, EndOfStreamEvent):
                            break
                        if isinstance(peeked, BarEvent):
                            if peeked.bar.symbol == cur_bar.symbol:
                                next_bar = peeked.bar
                            break
                        # Skip non-bar events but keep looking.

                    # Per-bar mode: deliver this bar to the strategy inline,
                    # using post-fill portfolio state, and apply its response.
                    def _fetch_response() -> Tuple[List[Dict], List[Dict]]:
                        resp = harness.send_bar(
                            bar=cur_bar.model_dump(mode="json"),
                            state=self._state(portfolio),
                            is_warmup=is_warmup,
                        )
                        return resp.orders, resp.cancels

                    pending_for_prev = self._process_one_bar(
                        cur_bar=cur_bar,
                        next_bar=next_bar,
                        prev_bar=prev_bar,
                        is_warmup=is_warmup,
                        fetch_response=_fetch_response,
                        pending_for_prev=pending_for_prev,
                        portfolio=portfolio,
                        order_book=order_book,
                        fill_sim=fill_sim,
                        harness=harness,
                        on_trade=on_trade,
                        result=result,
                        eod_buffer=eod_buffer,
                        position_tracker=position_tracker,
                        engine_exits=engine_exits,
                        engine_entries=engine_entries,
                        streaming_views=streaming_views,
                    )

                    prev_bar = cur_bar

                # End-of-stream: any orders still queued for "next bar" are
                # dropped with a log note — matches the legacy engine's
                # behavior of not fabricating a terminal fill bar.
                self._drain_unfilled_at_eos(pending_for_prev, prev_bar, result)

                harness.send_end()
            except LookAheadError as exc:
                # Parent-side look-ahead guard fired inside the fill simulator:
                # classified the same way as a subprocess-side violation so
                # operators see a single error category.
                return self._abort_result(result, exc, eod_buffer, harness)
            except StrategyRuntimeError as exc:
                return self._abort_result(result, exc, eod_buffer, harness)

        return self._finalize_result(result, eod_buffer, harness, fill_sim=fill_sim)

    # ------------------------------------------------------------------
    # Issue #377: chunked-bar protocol path. Buffers up to ``chunk_size``
    # bars and sends them in a single ``send_bars`` round-trip; the
    # subprocess returns orders/cancels tagged with ``bar_index`` so each
    # one is routed back to the originating bar's timestamp — preserving
    # ``BarSafetyAssertion`` semantics. Tradeoff: every bar in a chunk
    # sees the same chunk-start state snapshot (capital/equity/positions).
    # Strategies that depend on intra-chunk fill state should run with
    # ``BAR_CHUNK_SIZE=1``; paper trading pins this in __init__.
    # ------------------------------------------------------------------

    def _run_chunked(
        self,
        *,
        stream: Iterable[StreamEvent],
        harness: StreamingHarness,
        portfolio: Portfolio,
        order_book: OrderBook,
        fill_sim: FillSimulator,
        result: TradingServiceResult,
        chunk_size: int,
        on_trade: Optional[Callable[[TradeRecord], None]],
        eod_buffer: "_StreamingEquityBuffer",
        position_tracker: Dict[str, _TrackedPosition],
        engine_exits: _EngineExitDispatcher,
        engine_entries: _EngineEntryDispatcher,
        streaming_views: Dict[str, StreamingHistoryView],
    ) -> TradingServiceResult:
        prev_bar = None
        pending_for_prev: List[OrderRequest] = []
        event_iter = iter(stream)
        peeked: Optional[StreamEvent] = None
        chunk_buffer: List[tuple] = []  # (cur_bar, is_warmup, next_bar)
        terminated = False

        def _flush_chunk() -> bool:
            """Send the buffered chunk, then replay per-bar pre/post logic
            in order using the strategy's bar_index-tagged response.
            Returns False if the run should terminate (drawdown breach).
            """
            nonlocal prev_bar, pending_for_prev
            if not chunk_buffer:
                return True
            chunk_state = self._state(portfolio)
            payload = [
                {
                    "bar": cb.model_dump(mode="json"),
                    "state": chunk_state,
                    "is_warmup": iw,
                }
                for (cb, iw, _) in chunk_buffer
            ]
            chunk_resp = harness.send_bars(bars=payload)

            # Group orders/cancels by bar_index. Validate the index is
            # in [0, len(chunk)) before bucketing — without this, a
            # strategy bug (or a hand-set ``ctx._current_bar_index``
            # outside the harness-managed range) would silently route
            # the order to a phantom bar that the replay loop never
            # consumes, dropping the emission with no diagnostic.
            # Untagged records (None) likewise fail the range check;
            # the chunked child always tags, so a missing tag is a
            # protocol violation.
            chunk_len = len(chunk_buffer)

            def _validated(
                records: List[Dict], indices: List[Optional[int]], kind: str
            ) -> Dict[int, List[Dict]]:
                grouped: Dict[int, List[Dict]] = {}
                for rec, idx in zip(records, indices):
                    # ``bool`` is a subclass of ``int`` in Python, so a
                    # forged ``True``/``False`` would pass the range
                    # check and route to bar 1 / bar 0. Reject it
                    # explicitly to match the same defense in
                    # ``OrderBook.requeue``'s numeric input checks.
                    if (
                        isinstance(idx, bool)
                        or not isinstance(idx, int)
                        or not (0 <= idx < chunk_len)
                    ):
                        raise StrategyRuntimeError(
                            f"strategy emitted {kind} with out-of-range bar_index="
                            f"{idx!r} for chunk of size {chunk_len} (payload={rec!r})",
                            etype="protocol_error",
                        )
                    grouped.setdefault(idx, []).append(rec)
                return grouped

            orders_by_bar = _validated(chunk_resp.orders, chunk_resp.order_bar_indices, "order")
            cancels_by_bar = _validated(chunk_resp.cancels, chunk_resp.cancel_bar_indices, "cancel")

            for i, (cur_bar, is_warmup, next_bar) in enumerate(chunk_buffer):
                bar_orders = orders_by_bar.get(i, [])
                bar_cancels = cancels_by_bar.get(i, [])

                # Per-bar replay: the strategy response was already collected
                # by the batched ``send_bars`` above, so the thunk just hands
                # back this bar's ``bar_index``-tagged orders/cancels (bound as
                # defaults so the closure captures this iteration's values).
                try:
                    pending_for_prev = self._process_one_bar(
                        cur_bar=cur_bar,
                        next_bar=next_bar,
                        prev_bar=prev_bar,
                        is_warmup=is_warmup,
                        fetch_response=lambda o=bar_orders, c=bar_cancels: (o, c),
                        pending_for_prev=pending_for_prev,
                        portfolio=portfolio,
                        order_book=order_book,
                        fill_sim=fill_sim,
                        harness=harness,
                        on_trade=on_trade,
                        result=result,
                        eod_buffer=eod_buffer,
                        position_tracker=position_tracker,
                        engine_exits=engine_exits,
                        engine_entries=engine_entries,
                        streaming_views=streaming_views,
                    )
                except StrategyRuntimeError:
                    # ``_process_bar_strategy_response`` (inside
                    # ``_process_one_bar``) raises on an
                    # ``UnsupportedOrderFeatureError`` from the strategy. The
                    # per-bar path lets it propagate; the chunked path must
                    # clear the buffer first so the outer loop's recovery path
                    # doesn't replay any buffered bars.
                    chunk_buffer.clear()
                    raise

                prev_bar = cur_bar

            chunk_buffer.clear()
            return True

        try:
            while True:
                if peeked is not None:
                    event = peeked
                    peeked = None
                else:
                    event = next(event_iter, None)
                if event is None or isinstance(event, EndOfStreamEvent):
                    break
                if not isinstance(event, BarEvent):
                    continue
                cur_bar = event.bar
                is_warmup = event.is_warmup

                next_bar = None
                while True:
                    peeked = next(event_iter, None)
                    if peeked is None or isinstance(peeked, EndOfStreamEvent):
                        break
                    if isinstance(peeked, BarEvent):
                        if peeked.bar.symbol == cur_bar.symbol:
                            next_bar = peeked.bar
                        break

                chunk_buffer.append((cur_bar, is_warmup, next_bar))
                if len(chunk_buffer) >= chunk_size:
                    if not _flush_chunk():
                        terminated = True
                        break

            if not terminated:
                _flush_chunk()

            self._drain_unfilled_at_eos(pending_for_prev, prev_bar, result)

            harness.send_end()
        except LookAheadError as exc:
            return self._abort_result(result, exc, eod_buffer, harness)
        except StrategyRuntimeError as exc:
            return self._abort_result(result, exc, eod_buffer, harness)

        return self._finalize_result(result, eod_buffer, harness, fill_sim=fill_sim)

    # ------------------------------------------------------------------
    # Shared per-bar loop body + stream-teardown tail.
    #
    # Extracted so the per-bar (``run``) and chunked (``_run_chunked``)
    # paths cannot drift: the two used to carry ~120 lines of near-identical
    # logic and a clone-class divergence had already caused a bug. The only
    # genuine difference between the paths — per-bar ``send_bar`` vs a single
    # batched ``send_bars`` per chunk — is threaded in via the
    # ``fetch_response`` thunk, invoked at the same point in both paths.
    # ------------------------------------------------------------------

    def _process_one_bar(
        self,
        *,
        cur_bar: Bar,
        next_bar: Optional[Bar],
        prev_bar: Optional[Bar],
        is_warmup: bool,
        fetch_response: Callable[[], Tuple[List[Dict], List[Dict]]],
        pending_for_prev: List[OrderRequest],
        portfolio: Portfolio,
        order_book: OrderBook,
        fill_sim: FillSimulator,
        harness: "StreamingHarness",
        on_trade: Optional[Callable[[TradeRecord], None]],
        result: TradingServiceResult,
        eod_buffer: "_StreamingEquityBuffer",
        position_tracker: Dict[str, _TrackedPosition],
        engine_exits: _EngineExitDispatcher,
        engine_entries: _EngineEntryDispatcher,
        streaming_views: Dict[str, StreamingHistoryView],
    ) -> List[OrderRequest]:
        """Run the per-bar event loop for a single bar.

        Steps, in execution order:
        1. Expire day orders on date change.
        2. Submit the orders queued against the previous bar, pinning any
           engine-emitted exit to its target entry.
        3. Process fills, mark to market, stamp the equity curve, increment
           ``bars_processed``, and refresh the engine position tracker.
        4. Append the current bar to the streaming views.
        5. Fetch the strategy response via ``fetch_response``.
        6. Apply the strategy response (orders/cancels) and return the updated
           pending queue.

        Warm-up bars skip steps 1-3 (and the count) but still run steps 4-6.

        Preconditions: ``fetch_response`` returns ``(bar_orders, bar_cancels)``
        for ``cur_bar`` using post-fill portfolio state; the ``process_bar`` →
        tracker → append ordering must not be reordered (the exit reconciler
        relies on it).
        Postconditions: returns the new ``pending_for_prev`` queue (the orders
        the strategy emitted this bar, to be submitted against the next bar).
        """
        if not is_warmup:
            # 1) Expire day orders on date change. Routes through
            #    ``FillSimulator.expire_day_orders`` so partially-filled
            #    bracket parents get protective legs before the parent is
            #    dropped. ``timestamp`` is an ISO-8601 string
            #    (``YYYY-MM-DD[THH:MM:SS]``), so ``[:10]`` is the calendar date.
            if prev_bar is not None and (cur_bar.timestamp[:10] != prev_bar.timestamp[:10]):
                expired = fill_sim.expire_day_orders(cur_bar)
                if expired:
                    result.execution_diagnostics.orders_unfilled += len(expired)
                    for ex in expired:
                        _record_event(
                            result.execution_diagnostics,
                            "unfilled",
                            timestamp=cur_bar.timestamp,
                            symbol=ex.request.symbol,
                            side=ex.request.side.value,
                            order_type=ex.request.order_type.value,
                            reason="day_expired",
                        )

            # 2) Fill any orders from the previous iteration against *this*
            #    (current) bar. These were submitted by the strategy after
            #    seeing ``prev_bar``.
            if pending_for_prev:
                # Invariant: the queue is only populated after a prior bar was
                # delivered to the strategy, so ``prev_bar`` is necessarily set
                # here. Make it explicit (stripped under ``-O``) so a future
                # caller that violates it fails loudly rather than with a bare
                # AttributeError on ``prev_bar.timestamp``.
                assert prev_bar is not None, "pending_for_prev implies a prior bar was seen"
                # Apply the mode-level default unfilled policy parent-side
                # (after the request has left the strategy process), so
                # strategy bytes stay identical regardless of the flag.
                apply_default = _partial_fill_defaults_enabled()
                for req in pending_for_prev:
                    if apply_default and req.unfilled_policy is None:
                        req.unfilled_policy = self._default_unfilled_policy
                    equity = portfolio.mark_to_market()
                    submitted_po = order_book.submit(
                        req,
                        submitted_at=prev_bar.timestamp,
                        submitted_equity=equity,
                        # Register the parent as eligible to carry bracket
                        # children when the strategy attached protective legs;
                        # non-bracket entries pay zero overhead (flag is False).
                        # One predicate, not a hand-rolled OR: an entry whose
                        # exit legs live only in ``attached_exits`` must register
                        # as bracket-eligible too, or materializing its children
                        # on fill raises "not a known top-level order id".
                        expect_brackets=req.has_attached_exits,
                    )
                    # Pin engine-emitted exits to the Position they target so
                    # the fill simulator's stale-continuation guard drops them
                    # when a prior strategy exit closes the position first.
                    # No-op on the chunked path (engine exits require the
                    # per-bar protocol, so the bindings map is empty there).
                    bound_entry = engine_exits.engine_exit_bindings.pop(req.client_order_id, None)
                    if bound_entry is not None:
                        submitted_po.working_against_entry_order_id = bound_entry
                    result.execution_diagnostics.orders_accepted += 1
                    _record_event(
                        result.execution_diagnostics,
                        "accepted",
                        timestamp=prev_bar.timestamp,
                        symbol=req.symbol,
                        side=req.side.value,
                        order_type=req.order_type.value,
                    )
                pending_for_prev = []

            # Ordering invariant (relied on by the exit reconciler in
            # ``_build_exit_reconciler``): ``process_bar`` runs BEFORE
            # ``_update_position_tracker`` and ``_append_streaming_bar`` below.
            # So when the fill simulator stamps an exit during this call,
            # ``streaming_views``' latest bar is still the signal bar (cur_bar
            # not yet appended) and ``position_tracker[sym].just_opened`` still
            # reflects it. Do not move the tracker/view updates above this call.
            outcome = fill_sim.process_bar(cur_bar, next_bar=next_bar)
            _apply_fill_outcome_events(result.execution_diagnostics, outcome)
            for fill in outcome.entry_fills + outcome.exit_fills:
                harness.send_fill(
                    fill=fill.model_dump(mode="json"),
                    state=self._state(portfolio),
                )
            result.trades.extend(outcome.closed_trades)
            if on_trade is not None:
                for trade in outcome.closed_trades:
                    on_trade(trade)

            # 3) Mark-to-market and stamp the equity curve. There is no
            # drawdown circuit-breaker — a Strategy Lab run is an experiment
            # and must be free to lose up to 100% so its true downside is
            # observed, not truncated by a limit.
            portfolio.update_last_price(cur_bar.symbol, cur_bar.close)
            equity = portfolio.mark_to_market()
            # Stamp EOD equity for the streaming curve. Sub-daily bars
            # overwrite the same calendar-day key, so the last MTM of each
            # trading day wins.
            eod_buffer.record(cur_bar.timestamp, equity)

            # Refresh engine-side per-position state for ``cur_bar.symbol``
            # based on the post-fill portfolio. No-op when exit_rules is empty.
            if self._exit_rules:
                self._update_position_tracker(
                    tracker=position_tracker,
                    cur_bar=cur_bar,
                    portfolio=portfolio,
                )

        # Append every bar (including warm-up) to the streaming view so
        # indicators have full history for predicate evaluation once warm-up
        # ends.
        self._append_streaming_bar(streaming_views, cur_bar)

        # 4) Deliver the current bar to the strategy and apply the orders it
        #    submits in response. Warm-up bars set ``ctx.is_warmup = True`` in
        #    the subprocess so the strategy can short-circuit order emission;
        #    any orders it emits anyway are dropped as a safety net inside
        #    ``_process_bar_strategy_response``.
        bar_orders, bar_cancels = fetch_response()

        # Count only post-warmup bars, and only after the strategy has been
        # consulted for this bar — Phase 4's signals_per_bar diagnostic divides
        # trades by bars the strategy could actually have signaled on. Placing
        # the increment after ``fetch_response`` (the per-bar path's
        # ``send_bar``) preserves the error-path behaviour: a strategy that
        # raises before returning a response leaves ``bars_processed`` unchanged
        # for that bar, so a first-bar crash does not fabricate a misleading
        # ``signals_per_bar`` / ``low_signals_per_bar`` diagnostic. (No-op
        # difference on the chunked path, where ``fetch_response`` cannot fail.)
        if not is_warmup:
            result.bars_processed += 1

        self._process_bar_strategy_response(
            cur_bar=cur_bar,
            bar_orders=bar_orders,
            bar_cancels=bar_cancels,
            is_warmup=is_warmup,
            portfolio=portfolio,
            order_book=order_book,
            pending_for_prev=pending_for_prev,
            position_tracker=position_tracker,
            engine_exits=engine_exits,
            engine_entries=engine_entries,
            streaming_views=streaming_views,
            result=result,
        )
        return pending_for_prev

    def _drain_unfilled_at_eos(
        self,
        pending_for_prev: List[OrderRequest],
        prev_bar: Optional[Bar],
        result: TradingServiceResult,
    ) -> None:
        """Drop orders still queued for a next bar that never arrives.

        Matches the legacy engine's behaviour of not fabricating a terminal
        fill bar.

        Args:
            pending_for_prev: orders the strategy queued against a next bar that
                never arrived; dropped (not filled).
            prev_bar: the last processed bar, used to timestamp the diagnostics
                (``None`` if the stream produced no bars).
            result: the in-progress service result whose execution diagnostics
                are updated in place.

        Postconditions: each still-pending order is recorded as an
        ``end_of_stream`` unfilled diagnostic.
        """
        if not pending_for_prev:
            return
        logger.info(
            "%d orders queued at end-of-stream with no next bar; dropped",
            len(pending_for_prev),
        )
        result.execution_diagnostics.orders_unfilled += len(pending_for_prev)
        last_ts = prev_bar.timestamp if prev_bar is not None else None
        for req in pending_for_prev:
            _record_event(
                result.execution_diagnostics,
                "unfilled",
                timestamp=last_ts,
                symbol=req.symbol,
                side=req.side.value,
                order_type=req.order_type.value,
                reason="end_of_stream",
            )

    def _finalize_result(
        self,
        result: TradingServiceResult,
        eod_buffer: "_StreamingEquityBuffer",
        harness: "StreamingHarness",
        *,
        fill_sim: Optional[FillSimulator] = None,
    ) -> TradingServiceResult:
        """Materialize the streaming equity curve + probe events and finalize.

        When ``fill_sim`` is supplied (the success path), also records the entry
        reasons of any still-open positions.

        Args:
            result: the in-progress service result, mutated in place.
            eod_buffer: end-of-day equity buffer materialized into
                ``result.streaming_equity_curve``.
            harness: streaming harness whose ``probe_events`` are copied onto
                the result.
            fill_sim: supplied only on the success path; its open positions'
                entry reasons are recorded. ``None`` on the abort path.
        """
        result.streaming_equity_curve = eod_buffer.materialize()
        result.probe_events = harness.probe_events
        if fill_sim is not None:
            result.open_position_entry_reasons = [
                pos.entry_reason
                for pos in fill_sim.portfolio.positions.values()
                if pos.entry_reason
            ]
        return _finalize_diagnostics(result)

    def _abort_result(
        self,
        result: TradingServiceResult,
        exc: Exception,
        eod_buffer: "_StreamingEquityBuffer",
        harness: "StreamingHarness",
    ) -> TradingServiceResult:
        """Shared ``except`` tail for both event loops.

        Classifies look-ahead violations the same way whether they surface as a
        parent-side ``LookAheadError`` or a subprocess-side ``StrategyRuntimeError``
        with ``etype == "lookahead_violation"``, then materializes and finalizes.

        Args:
            result: the in-progress service result, mutated in place.
            exc: the exception that aborted the run; its message and type drive
                ``result.error`` / ``result.lookahead_violation``.
            eod_buffer: end-of-day equity buffer (see :meth:`_finalize_result`).
            harness: streaming harness (see :meth:`_finalize_result`).
        """
        result.error = str(exc)
        result.lookahead_violation = isinstance(exc, LookAheadError) or (
            isinstance(exc, StrategyRuntimeError) and exc.etype == "lookahead_violation"
        )
        return self._finalize_result(result, eod_buffer, harness)

    # ------------------------------------------------------------------
    # Issue #527 — engine-side enforcement of structured ``exit_rules``.
    # ------------------------------------------------------------------

    def _process_bar_strategy_response(
        self,
        *,
        cur_bar,
        bar_orders: List[Dict],
        bar_cancels: List[Dict],
        is_warmup: bool,
        portfolio: Portfolio,
        order_book: OrderBook,
        pending_for_prev: List[OrderRequest],
        position_tracker: Dict[str, _TrackedPosition],
        engine_exits: _EngineExitDispatcher,
        engine_entries: _EngineEntryDispatcher,
        streaming_views: Dict[str, StreamingHistoryView],
        result: TradingServiceResult,
    ) -> None:
        """Apply one bar's strategy response (cancels + orders) to the
        order book and pending-submit queue, then run the engine's
        structured entry- and exit-rule enforcement steps.

        Shared between the per-bar (``run``) and chunked
        (``_run_chunked``) paths — extracted because earlier the engine-
        exit enforcement step lived only in the per-bar copy, so any
        run with ``BAR_CHUNK_SIZE>1`` silently skipped ``exit_rules``
        evaluation entirely. The dedup makes that gap structurally
        impossible.

        Warm-up bars short-circuit: orders submitted during warm-up
        are dropped with a ``warmup_dropped`` lifecycle event (the
        strategy is expected to honour ``ctx.is_warmup``; we drop
        anyway as a safety net), cancels are no-ops (no live book),
        and the engine-exit step is skipped (no positions exist
        during warm-up that could trip a rule).

        Orders queued here are look-ahead-safe: the bar-loop caller
        submits them against the NEXT bar.

        Raises :class:`StrategyRuntimeError` on
        ``UnsupportedOrderFeatureError`` from
        ``OrderRequest.validate_prices`` — the chunked caller must
        ``chunk_buffer.clear()`` before letting it propagate.
        """
        if is_warmup:
            if bar_orders:
                result.warmup_orders_dropped += len(bar_orders)
                logger.info(
                    "dropped %d order(s) submitted during warm-up bar",
                    len(bar_orders),
                )
                for o in bar_orders:
                    _record_event(
                        result.execution_diagnostics,
                        "warmup_dropped",
                        timestamp=cur_bar.timestamp,
                        symbol=o.get("symbol"),
                        side=o.get("side"),
                        order_type=o.get("order_type"),
                    )
            # Cancels during warm-up are no-ops (no live order book).
            return

        for c in bar_cancels:
            oid = c.get("order_id")
            if oid:
                order_book.cancel(oid)

        # Orders submitted now are evaluated against the *next* bar
        # (look-ahead-safe).
        for o in bar_orders:
            result.execution_diagnostics.orders_emitted += 1
            _record_event(
                result.execution_diagnostics,
                "emitted",
                timestamp=cur_bar.timestamp,
                symbol=o.get("symbol"),
                side=o.get("side"),
                order_type=o.get("order_type"),
            )
            try:
                req = OrderRequest(**o)
                req.validate_prices()
                pending_for_prev.append(req)
                # An opposite-side order against an existing open
                # position is the strategy's exit intent. Counted
                # here (parent-side, before fill) so the diagnostic
                # reflects emission, not execution; #410 owns the
                # fill-side ``exit_filled`` event.
                held = portfolio.positions.get(req.symbol)
                if held is not None and held.side != req.side:
                    result.execution_diagnostics.exits_emitted += 1
            except UnsupportedOrderFeatureError as exc:
                # Runtime-support gates from validate_prices ("feature
                # ships in a later step of #379") must terminate the
                # run, not be silently dropped. Convert to a
                # StrategyRuntimeError so the outer loop returns a
                # structured ``TradingServiceResult.error`` instead of
                # crashing ``TradingService.run()``. The narrow subclass
                # keeps unrelated ``NotImplementedError``s from strategy
                # code in the generic catch below. See #383.
                _increment_rejection(result.execution_diagnostics, "unsupported_feature")
                _record_event(
                    result.execution_diagnostics,
                    "rejected",
                    timestamp=cur_bar.timestamp,
                    symbol=o.get("symbol"),
                    side=o.get("side"),
                    order_type=o.get("order_type"),
                    reason="unsupported_feature",
                    detail=str(exc),
                )
                raise StrategyRuntimeError(
                    f"strategy emitted an unsupported order: {exc}",
                    etype="unsupported_feature",
                ) from exc
            except Exception as exc:  # malformed request from strategy
                logger.warning("dropping malformed order from strategy: %s", exc)
                _increment_rejection(result.execution_diagnostics, "malformed_request")
                _record_event(
                    result.execution_diagnostics,
                    "rejected",
                    timestamp=cur_bar.timestamp,
                    symbol=o.get("symbol"),
                    side=o.get("side"),
                    order_type=o.get("order_type"),
                    reason="malformed_request",
                    detail=str(exc),
                )

        # Issue #527 — engine-side enforcement of structured
        # ``exit_rules``. Runs after the strategy's orders are queued
        # so we can dedupe against strategy-emitted closes and any
        # in-flight engine exit on the order book. No-op when the
        # spec has no exit rules.
        engine_exits.maybe_emit(
            cur_bar=cur_bar,
            position_tracker=position_tracker,
            portfolio=portfolio,
            pending_for_prev=pending_for_prev,
            order_book=order_book,
            result=result,
        )

        # Issue #527 — extend trailing watermarks AFTER rule evaluation
        # so the next bar's eval has cur_bar's extreme baked in, but
        # THIS bar's eval did not see ``cur_bar.high`` raise the
        # trailing floor and then trigger off ``cur_bar.low`` (intrabar
        # lookahead). No-op when the spec has no exit rules.
        if self._exit_rules:
            self._extend_watermarks(tracker=position_tracker, cur_bar=cur_bar)

        engine_entries.maybe_emit(
            cur_bar=cur_bar,
            portfolio=portfolio,
            pending_for_prev=pending_for_prev,
            views=streaming_views,
            result=result,
        )

    @staticmethod
    def _append_streaming_bar(
        views: Dict[str, StreamingHistoryView],
        cur_bar,
    ) -> None:
        """Append a bar to the per-symbol streaming view."""
        sym = cur_bar.symbol
        if sym not in views:
            views[sym] = StreamingHistoryView()
        views[sym].append(
            BarRecord(
                timestamp=cur_bar.timestamp,
                open=cur_bar.open,
                high=cur_bar.high,
                low=cur_bar.low,
                close=cur_bar.close,
                volume=getattr(cur_bar, "volume", 0.0),
                symbol=sym,
            )
        )

    @staticmethod
    def _update_position_tracker(
        *,
        tracker: Dict[str, _TrackedPosition],
        cur_bar,
        portfolio: Portfolio,
    ) -> None:
        """Reconcile ``tracker`` against ``portfolio.positions`` for one symbol.

        Called BEFORE rule evaluation each bar. Handles:

        * Fresh-entry tracker creation (with watermarks initialised at
          ``entry_price`` regardless of market vs limit entry — see the
          ``_extend_watermarks`` docstring for why the entry bar's
          high/low is NOT included here).
        * Identity reset on same-bar exit + re-entry (different
          ``entry_order_id``).
        * ``entry_price`` refresh on scale-ins (partial-fill
          continuation where ``Portfolio.extend`` updates the weighted-
          average entry).
        * ``just_opened`` flip from ``True`` to ``False`` on the first
          carry-over bar.

        Watermark extension is deliberately split off into
        :meth:`_extend_watermarks` so trailing-stop rules don't see
        the current bar's high/low while evaluating against the same
        bar's high/low (would be intrabar lookahead — a long could
        use ``cur_bar.high`` to raise the trailing floor and then
        trigger off ``cur_bar.low`` even if the low printed first).
        """
        sym = cur_bar.symbol
        pos = portfolio.positions.get(sym)
        if pos is None:
            # Position closed this bar (or never opened) — drop tracker entry.
            tracker.pop(sym, None)
            return
        existing = tracker.get(sym)
        if existing is not None and existing.entry_order_id == pos.entry_order_id:
            # Scale-in refresh: ``Portfolio.extend`` updates
            # ``pos.entry_price`` to the new weighted-average entry on
            # ``REQUEUE_NEXT_BAR`` / ``TWAP_N`` continuations. Mirror
            # that here so ``StopLossRule(basis="entry_price")`` and
            # ``TakeProfitRule`` evaluate against the position's current
            # basis rather than the first slice's price.
            existing.entry_price = pos.entry_price
            # The high/low watermarks are deliberately NOT rebased here. They are
            # ABSOLUTE since-entry price extremes (an actual high/low print), not
            # offsets off ``entry_price``, so a shift in the weighted-average entry
            # does not invalidate them. Scaled-take-profit rung eligibility
            # (``_next_scaled_rung``) and trailing stops both compare the absolute
            # watermark against a target recomputed off the CURRENT ``entry_price``
            # (e.g. ``entry_price * (1 + level.pct)``), so the post-scale-in basis is
            # already folded into the target — rebasing the watermark too would
            # double-count the entry shift. (Watermark extension itself still happens
            # in ``_extend_watermarks`` after evaluation.)
            # First carry-over bar — the position has now seen a full
            # bar of post-entry price action. Rule evaluation may use
            # whatever watermark extension the prior bar's
            # ``_extend_watermarks`` step produced.
            existing.just_opened = False
        else:
            # Fresh entry this bar — either truly first entry, or a
            # same-bar exit + re-entry replaced the prior position
            # (different ``entry_order_id``).
            #
            # Watermarks initialise at ``entry_price`` for BOTH market
            # and non-market fills. Including the entry bar's high/low
            # here would create intrabar lookahead for trailing stops
            # (today's high raises the floor, today's low triggers it,
            # regardless of which printed first). Non-market entries
            # additionally set ``just_opened=True`` so rule evaluation
            # is skipped entirely on the entry bar (the bar's pre-fill
            # price action is ambiguous). Market entries leave
            # ``just_opened=False`` so an entry_price stop-loss or
            # take-profit can fire same-bar from ``bar.high`` / ``bar.low``
            # against ``entry_price`` (the watermark isn't consulted
            # for those rule kinds).
            just_opened = pos.entry_order_type != "market"
            tracker[sym] = _TrackedPosition(
                side=pos.side,
                entry_price=pos.entry_price,
                entry_order_id=pos.entry_order_id,
                just_opened=just_opened,
                high_since_entry=pos.entry_price,
                low_since_entry=pos.entry_price,
            )

    @staticmethod
    def _extend_watermarks(
        *,
        tracker: Dict[str, _TrackedPosition],
        cur_bar,
    ) -> None:
        """Extend ``high_since_entry`` / ``low_since_entry`` for the
        current bar's symbol AFTER rule evaluation.

        Why a separate call: ``_update_position_tracker`` runs before
        :meth:`_EngineExitDispatcher.maybe_emit` so the tracker
        reflects the current bar's fills + ``entry_price`` /
        ``just_opened`` state. Watermark extension is deferred to
        after rule evaluation so trailing-stop rules see only the
        watermark "as of the prior bar" — they can't fire from a
        floor that just moved up on the same bar's high.

        The next bar's evaluation reads the now-extended watermark,
        which is the intended trailing-stop semantics (track every
        prior bar's extreme since entry).

        ``just_opened=True`` (non-market entry) skips extension on
        the entry bar: a limit / stop fill that landed mid-bar shares
        OHLC with pre-entry price action — including the entry bar's
        high / low here would let a pre-entry intrabar spike define
        the trailing watermark and trigger a trailing-high stop on
        the next bar from price action that happened before the
        position existed. The tradeoff is losing the entry bar's
        post-fill range; that's the safer side of the unknowable
        intrabar fill location.
        """
        sym = cur_bar.symbol
        state = tracker.get(sym)
        if state is None:
            return
        if state.just_opened:
            return
        if cur_bar.high > state.high_since_entry:
            state.high_since_entry = cur_bar.high
        if cur_bar.low < state.low_since_entry:
            state.low_since_entry = cur_bar.low

    # ------------------------------------------------------------------

    @staticmethod
    def _state(portfolio: Portfolio) -> Dict:
        equity = portfolio.mark_to_market()
        return {
            "capital": portfolio.capital,
            "equity": equity,
            "positions": portfolio.position_snapshots(),
        }


# Re-export the OrderSide enum for convenience of callers that need to
# construct synthetic orders (e.g. tests).
__all__ = ["OrderSide", "TradingService", "TradingServiceResult"]
