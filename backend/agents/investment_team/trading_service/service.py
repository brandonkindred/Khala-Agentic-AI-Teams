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
import os
from dataclasses import dataclass, field
from datetime import date as date_cls
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

import numpy as np

from ..execution.bar_safety import LookAheadError
from ..execution.metrics import EquityCurve, weekday_range
from ..execution.risk_filter import RiskFilter, RiskLimits
from ..models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    OrderLifecycleEvent,
    TradeRecord,
)
from ..strategy_lab.executor.predicate_evaluator import (
    BarRecord,
    StreamingHistoryView,
)
from ..strategy_lab.executor.predicate_evaluator import (
    evaluate_entry_rules as _evaluate_entry_rules_pred,
)
from ..strategy_lab.executor.rule_compiler import (
    BarSnapshot,
    ExitIntent,
    PositionState,
    evaluate_exit_rules,
)
from ..strategy_lab.spec_dsl import (
    EntryRule,
    ExitRule,
    FixedFractionSizing,
    FixedNotionalSizing,
    StopLossRule,
    VolatilityTargetSizing,
    first_side_stop_factor,
)
from ..strategy_lab_context import is_fractional_asset_class
from .data_stream.protocol import BarEvent, EndOfStreamEvent, StreamEvent
from .engine.execution_model import build_execution_model
from .engine.fill_simulator import FillOutcome, FillSimulator, FillSimulatorConfig
from .engine.order_book import OrderBook
from .engine.portfolio import Portfolio, Position
from .strategy.contract import (
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
    UnfilledPolicy,
    UnsupportedOrderFeatureError,
)
from .strategy.streaming_harness import StrategyRuntimeError, StreamingHarness

logger = logging.getLogger(__name__)

_MAX_ORDER_EVENTS = 20

# Issue #527 — reserved order ``reason`` prefix the engine stamps on every
# rule-triggered close order it emits on the strategy's behalf. The conformance
# quality gate reads this prefix off ``OrderLifecycleEvent`` records to count
# wrapper-emitted exits and verify each trade obeyed the structured rules.
ENGINE_EXIT_REASON_PREFIX = "engine_exit:"


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
    """

    side: OrderSide
    entry_price: float
    entry_order_id: str
    just_opened: bool
    high_since_entry: float
    low_since_entry: float

    def snapshot(self, symbol: str, qty: float) -> PositionState:
        # ``PositionState`` (the public evaluator input) carries the
        # side as a ``"long" | "short"`` Literal; convert at the
        # boundary so internal dispatcher logic stays enum-typed.
        return PositionState(
            symbol=symbol,
            side="long" if self.side == OrderSide.LONG else "short",
            qty=qty,
            entry_price=self.entry_price,
            high_since_entry=self.high_since_entry,
            low_since_entry=self.low_since_entry,
        )


@dataclass
class _EngineExitDispatcher:
    """Per-run owner of engine-side ``exit_rules`` enforcement.

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

    Empty ``exit_rules`` makes :meth:`maybe_emit` a no-op.
    """

    exit_rules: Sequence[ExitRule]
    engine_exit_bindings: Dict[str, str] = field(default_factory=dict)
    _next_seq: int = 0
    views: Optional[Dict[str, StreamingHistoryView]] = None

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
        gating = self._should_evaluate(sym, position_tracker, portfolio, order_book)
        if gating is None:
            return
        tracked, pos = gating

        intent = self._evaluate(sym, tracked, pos, cur_bar)
        if intent is None:
            return

        # Issue #527 — size the close to cover any same-side scale-in
        # that could grow ``pos.qty`` past the snapshot the engine saw
        # at emission. Two sources, both BEFORE the engine close on the
        # next bar:
        #   * Same-bar queued in ``pending_for_prev`` — strategy order
        #     submitted on this bar; submits first next bar.
        #   * Already resting on the order book — a GTC/limit scale-in
        #     from a prior bar that's still working; could fill
        #     alongside ``pending_for_prev`` next bar.
        # ``_fill_exit`` clips the engine close at
        # ``min(req.qty, existing_pos.qty)``, so without this oversize
        # the residual exposure stays open even though the structured
        # exit rule already fired. If any scale-in is rejected /
        # clipped at fill time the engine close clips back down to the
        # actual ``existing_pos.qty`` — no over-close risk.
        scale_in_qty = self._sum_same_side_queued(
            sym, tracked.side, pending_for_prev
        ) + self._sum_same_side_resting(sym, tracked.side, order_book)

        req = self._build_close_order(intent, tracked, pos, scale_in_qty)
        if req is None:
            return

        pending_for_prev.append(req)
        self._register_binding(req, pos)
        self._retire_competing_resting_orders(sym, tracked.side, pos, order_book)
        self._bind_same_bar_queued_exits(sym, tracked.side, pos, pending_for_prev)
        self._cancel_pending_entry_continuations(sym, pos, order_book)
        self._record_emission(req, intent, cur_bar, result)

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
    ) -> Optional[tuple[_TrackedPosition, Position]]:
        """Return ``(tracked, pos)`` if rule evaluation should run for
        this symbol on this bar, else ``None``.

        Gates:
        * Tracker has the symbol (a position is open).
        * Portfolio agrees and has positive qty.
        * ``tracked.just_opened`` is False — skip the entry bar for
          non-market fills (see ``_update_position_tracker``).
        * No in-flight engine market exit already pending on the
          order book (avoid stacking redundant engine markets across
          bars while the rule keeps re-triggering).
        """
        if sym not in position_tracker:
            return None
        pos = portfolio.positions.get(sym)
        if pos is None or pos.qty <= 0:
            return None
        tracked = position_tracker[sym]
        if tracked.just_opened:
            return None
        for po in order_book.pending_for_symbol(sym):
            po_req = po.request
            if po_req.order_type != OrderType.MARKET:
                continue
            if po_req.side != tracked.side and (po_req.reason or "").startswith(
                ENGINE_EXIT_REASON_PREFIX
            ):
                return None
        return tracked, pos

    def _evaluate(
        self,
        sym: str,
        tracked: _TrackedPosition,
        pos: Position,
        cur_bar,
    ) -> Optional[ExitIntent]:
        """Run the pure rule evaluator. Returns at most one intent per
        symbol — :func:`evaluate_exit_rules` stops at the first
        triggered rule per position.
        """
        snapshot = tracked.snapshot(sym, pos.qty)
        bar_snap = BarSnapshot(high=cur_bar.high, low=cur_bar.low, close=cur_bar.close)
        intents = evaluate_exit_rules(
            self.exit_rules,
            {sym: snapshot},
            {sym: bar_snap},
            views=self.views,
        )
        return intents[0] if intents else None

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
        sym: str,
        tracked_side: OrderSide,
        order_book: OrderBook,
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
        for po in order_book.pending_for_symbol(sym):
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
        """
        self._next_seq += 1
        close_side = OrderSide.SHORT if tracked.side == OrderSide.LONG else OrderSide.LONG
        req = OrderRequest(
            client_order_id=f"e{self._next_seq}",
            symbol=intent.symbol,
            side=close_side,
            qty=pos.qty + scale_in_qty,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            reason=(
                f"{ENGINE_EXIT_REASON_PREFIX}{intent.rule_kind}[{intent.rule_index}]"
                if intent.rule_kind == "signal_exit"
                else f"{ENGINE_EXIT_REASON_PREFIX}{intent.rule_kind}"
            ),
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

    def _register_binding(self, req: OrderRequest, pos: Position) -> None:
        """Record the binding so the bar-loop submit step can set
        ``working_against_entry_order_id`` on the resulting
        ``PendingOrder``.
        """
        self.engine_exit_bindings[req.client_order_id] = pos.entry_order_id

    def _retire_competing_resting_orders(
        self,
        sym: str,
        tracked_side: OrderSide,
        pos: Position,
        order_book: OrderBook,
    ) -> None:
        """Bind any unbound opposite-side resting orders to the position
        so they retire when the engine close removes the position.

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
        """
        for resting in order_book.pending_for_symbol(sym):
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
        sym: str,
        pos: Position,
        order_book: OrderBook,
    ) -> None:
        """Cancel any in-flight continuation of the position's entry
        order. A partial-fill remainder (``REQUEUE_NEXT_BAR`` or
        ``TWAP_N``) still on the book would fill on the next bar
        before the engine's close, growing the position past what the
        engine sized for and leaving residual exposure after
        ``_fill_exit`` clips at ``min(req.qty, existing_pos.qty)``.

        Continuations are identified by ``po.order_id ==
        pos.entry_order_id`` (exact — the strategy can't reuse an
        engine-issued order_id, and same-side new strategy entries
        have different order_ids).
        """
        for po in order_book.pending_for_symbol(sym):
            if po.order_id != pos.entry_order_id:
                continue
            if po.cumulative_filled_qty <= 0:
                continue
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
        basis_label = f"{intent.rule_kind}:{intent.basis}" if intent.basis else intent.rule_kind
        diag.exit_rule_firings_by_basis[basis_label] = (
            diag.exit_rule_firings_by_basis.get(basis_label, 0) + 1
        )
        _record_event(
            diag,
            "emitted",
            timestamp=cur_bar.timestamp,
            symbol=intent.symbol,
            side=req.side.value,
            order_type=OrderType.MARKET.value,
            reason=req.reason,
        )


ENGINE_ENTRY_REASON_PREFIX = "engine_entry:"


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
        req = OrderRequest(
            client_order_id=f"e_entry_{self._next_seq}",
            symbol=sym,
            side=side,
            qty=qty,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
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
        from ..strategy_lab.spec_dsl import IndicatorRef, SignalExitRule

        for rule in self.entry_rules:
            if not isinstance(rule, EntryRule):
                continue
            for side in (rule.when.lhs, rule.when.rhs):
                if isinstance(side, IndicatorRef) and side.name == "atr":
                    return side
        for rule in self.exit_rules:
            if not isinstance(rule, SignalExitRule):
                continue
            for side in (rule.when.lhs, rule.when.rhs):
                if isinstance(side, IndicatorRef) and side.name == "atr":
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
      ``same_side_order_ignored``).

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
        if shorts_possible and first_side_stop_factor(self._exit_rules, "short") is None:
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
        engine_exits = _EngineExitDispatcher(exit_rules=self._exit_rules, views=streaming_views)
        engine_entries = _EngineEntryDispatcher(
            entry_rules=self._entry_rules,
            sizing=self._sizing,
            exit_rules=self._exit_rules,
            target_symbols=self._target_symbols,
            risk_limits=self._risk.limits,
            asset_class=self._asset_class,
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

                    if not is_warmup:
                        # 1) Expire day orders on date change. Routes through
                        #    ``FillSimulator.expire_day_orders`` so partially-
                        #    filled bracket parents get protective legs before
                        #    the parent is dropped (#389).
                        if prev_bar is not None and (
                            cur_bar.timestamp[:10] != prev_bar.timestamp[:10]
                        ):
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

                        # 2) Fill any orders from the previous iteration against
                        #    *this* (current) bar. These were submitted by the
                        #    strategy after seeing `prev_bar`.
                        if pending_for_prev:
                            # #385 — apply the mode-level default unfilled
                            # policy parent-side (after the request has left
                            # the strategy process), so strategy bytes stay
                            # identical regardless of the flag. Step 3 only
                            # plumbs the value through; downstream consumers
                            # (order_book / fill_simulator) start acting on
                            # it in #386.
                            apply_default = _partial_fill_defaults_enabled()
                            for req in pending_for_prev:
                                if apply_default and req.unfilled_policy is None:
                                    req.unfilled_policy = self._default_unfilled_policy
                                equity = portfolio.mark_to_market()
                                submitted_po = order_book.submit(
                                    req,
                                    submitted_at=prev_bar.timestamp,
                                    submitted_equity=equity,
                                    # #389: register the parent as eligible to
                                    # carry bracket children when the strategy
                                    # attached protective legs. ``submit_attached``
                                    # rejects children whose parent isn't in the
                                    # eligible-parent set; non-bracket entries
                                    # pay zero overhead (flag is False).
                                    expect_brackets=(
                                        req.attached_stop_loss is not None
                                        or req.attached_take_profit is not None
                                    ),
                                )
                                # Issue #527 — pin engine-emitted exits to the
                                # Position they target so the fill simulator's
                                # stale-continuation guard drops them when a
                                # prior strategy exit closes the position first.
                                # Without this, an engine_exit submitted while a
                                # GTC/limit strategy exit is resting on the book
                                # could fall through to ``_fill_entry`` and open
                                # a new opposite-side position.
                                bound_entry = engine_exits.engine_exit_bindings.pop(
                                    req.client_order_id, None
                                )
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

                        # 3) Mark-to-market and stamp the equity curve. There is
                        # no drawdown circuit-breaker — a Strategy Lab run is an
                        # experiment and must be free to lose up to 100% so its
                        # true downside is observed, not truncated by a limit.
                        portfolio.update_last_price(cur_bar.symbol, cur_bar.close)
                        equity = portfolio.mark_to_market()
                        # #430: stamp EOD equity for the streaming curve.
                        # Sub-daily bars overwrite the same calendar-day key,
                        # so the last MTM of each trading day wins.
                        eod_buffer.record(cur_bar.timestamp, equity)

                        # Issue #527 — refresh engine-side per-position state
                        # for ``cur_bar.symbol`` based on the post-fill
                        # portfolio. No-op when exit_rules is empty (the
                        # tracker stays empty, the rule-eval block at the
                        # bottom of the loop short-circuits). Updating here
                        # — after fills but before send_bar — keeps trailing
                        # watermarks consistent with every bar the engine
                        # has actually seen, regardless of strategy behaviour.
                        if self._exit_rules:
                            self._update_position_tracker(
                                tracker=position_tracker,
                                cur_bar=cur_bar,
                                portfolio=portfolio,
                            )

                    # Append every bar (including warm-up) to the streaming
                    # view so indicators have full history for predicate
                    # evaluation once warm-up ends.
                    self._append_streaming_bar(streaming_views, cur_bar)

                    # 4) Deliver the current bar to the strategy and collect
                    #    any orders it submits in response. Warm-up bars set
                    #    ``ctx.is_warmup = True`` in the subprocess so the
                    #    strategy can short-circuit order emission; we also
                    #    drop any orders it emits anyway as a safety net
                    #    (handled inside ``_process_bar_strategy_response``).
                    resp = harness.send_bar(
                        bar=cur_bar.model_dump(mode="json"),
                        state=self._state(portfolio),
                        is_warmup=is_warmup,
                    )

                    if not is_warmup:
                        # Track only post-warmup bars — Phase 4's
                        # signals_per_bar diagnostic divides trades by
                        # bars the strategy could actually have signaled on.
                        result.bars_processed += 1

                    self._process_bar_strategy_response(
                        cur_bar=cur_bar,
                        bar_orders=resp.orders,
                        bar_cancels=resp.cancels,
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

                    prev_bar = cur_bar

                # End-of-stream: any orders still queued for "next bar" are
                # dropped with a log note — matches the legacy engine's
                # behavior of not fabricating a terminal fill bar.
                if pending_for_prev:
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

                harness.send_end()
            except LookAheadError as exc:
                # Parent-side look-ahead guard fired inside the fill
                # simulator: classify the same way as a subprocess-side
                # violation so operators see a single error category.
                result.error = str(exc)
                result.lookahead_violation = True
                result.streaming_equity_curve = eod_buffer.materialize()
                result.probe_events = harness.probe_events
                return _finalize_diagnostics(result)
            except StrategyRuntimeError as exc:
                result.error = str(exc)
                result.lookahead_violation = exc.etype == "lookahead_violation"
                result.streaming_equity_curve = eod_buffer.materialize()
                result.probe_events = harness.probe_events
                return _finalize_diagnostics(result)

        result.streaming_equity_curve = eod_buffer.materialize()
        result.probe_events = harness.probe_events
        result.open_position_entry_reasons = [
            pos.entry_reason for pos in fill_sim.portfolio.positions.values() if pos.entry_reason
        ]
        return _finalize_diagnostics(result)

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

                if not is_warmup:
                    # 1) Expire day orders on date change. See chunked path
                    #    above — routes through the simulator so brackets on
                    #    partially-filled parents survive expiry (#389).
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

                    # 2) Submit pending_for_prev against this (current) bar.
                    if pending_for_prev:
                        apply_default = _partial_fill_defaults_enabled()
                        for req in pending_for_prev:
                            if apply_default and req.unfilled_policy is None:
                                req.unfilled_policy = self._default_unfilled_policy
                            equity = portfolio.mark_to_market()
                            order_book.submit(
                                req,
                                submitted_at=prev_bar.timestamp,
                                submitted_equity=equity,
                                # #389: register the parent as eligible to
                                # carry bracket children when the strategy
                                # attached protective legs.
                                expect_brackets=(
                                    req.attached_stop_loss is not None
                                    or req.attached_take_profit is not None
                                ),
                            )
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

                    outcome = fill_sim.process_bar(cur_bar, next_bar=next_bar)
                    _apply_fill_outcome_events(result.execution_diagnostics, outcome)
                    for fill in outcome.entry_fills + outcome.exit_fills:
                        # send_fill is per-fill; happens between chunks too.
                        # The strategy sees fills from the *previous* chunk
                        # before its next chunk arrives.
                        harness.send_fill(
                            fill=fill.model_dump(mode="json"),
                            state=self._state(portfolio),
                        )
                    result.trades.extend(outcome.closed_trades)
                    if on_trade is not None:
                        for trade in outcome.closed_trades:
                            on_trade(trade)

                    # 3) Mark-to-market and stamp the equity curve. There is no
                    # drawdown circuit-breaker — a Strategy Lab run is an
                    # experiment and must be free to lose up to 100% so its true
                    # downside is observed, not truncated by a limit.
                    portfolio.update_last_price(cur_bar.symbol, cur_bar.close)
                    equity = portfolio.mark_to_market()
                    # #430: stamp EOD equity for the streaming curve.
                    eod_buffer.record(cur_bar.timestamp, equity)

                    result.bars_processed += 1

                    # Issue #527 — refresh engine-side per-position state
                    # for ``cur_bar.symbol`` based on the post-fill
                    # portfolio. Mirrors the per-bar (``run``) path's
                    # placement between fills+drawdown and the strategy-
                    # response processing step. No-op when exit_rules is
                    # empty.
                    if self._exit_rules:
                        self._update_position_tracker(
                            tracker=position_tracker,
                            cur_bar=cur_bar,
                            portfolio=portfolio,
                        )

                # Append every bar (including warm-up) to the streaming
                # view so indicators have full history for predicate
                # evaluation once warm-up ends.
                self._append_streaming_bar(streaming_views, cur_bar)

                # 4) Process the strategy's response for this bar.
                try:
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
                except StrategyRuntimeError:
                    # ``_process_bar_strategy_response`` raises on an
                    # ``UnsupportedOrderFeatureError`` from the strategy.
                    # The per-bar path just lets it propagate; the
                    # chunked path needs to clear the buffer first so
                    # the outer loop's recovery path doesn't replay
                    # any buffered bars.
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

            if pending_for_prev:
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

            harness.send_end()
        except LookAheadError as exc:
            result.error = str(exc)
            result.lookahead_violation = True
            result.streaming_equity_curve = eod_buffer.materialize()
            result.probe_events = harness.probe_events
            return _finalize_diagnostics(result)
        except StrategyRuntimeError as exc:
            result.error = str(exc)
            result.lookahead_violation = exc.etype == "lookahead_violation"
            result.streaming_equity_curve = eod_buffer.materialize()
            result.probe_events = harness.probe_events
            return _finalize_diagnostics(result)

        result.streaming_equity_curve = eod_buffer.materialize()
        result.probe_events = harness.probe_events
        result.open_position_entry_reasons = [
            pos.entry_reason for pos in fill_sim.portfolio.positions.values() if pos.entry_reason
        ]
        return _finalize_diagnostics(result)

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
