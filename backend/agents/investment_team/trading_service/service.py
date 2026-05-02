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
from typing import Callable, Dict, Iterable, List, Optional

from ..execution.bar_safety import LookAheadError
from ..execution.risk_filter import RiskFilter, RiskLimits
from ..models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    OrderLifecycleEvent,
    TradeRecord,
)
from .data_stream.protocol import BarEvent, EndOfStreamEvent, StreamEvent
from .engine.execution_model import build_execution_model
from .engine.fill_simulator import FillSimulator, FillSimulatorConfig
from .engine.order_book import OrderBook
from .engine.portfolio import Portfolio
from .strategy.contract import (
    OrderRequest,
    OrderSide,
    UnfilledPolicy,
    UnsupportedOrderFeatureError,
)
from .strategy.streaming_harness import StrategyRuntimeError, StreamingHarness

logger = logging.getLogger(__name__)

_MAX_ORDER_EVENTS = 20


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
    ) -> None:
        self.strategy_code = strategy_code
        self.config = config
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

        with StreamingHarness(self.strategy_code) as harness:
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
                return _finalize_diagnostics(result)

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
                        # 1) Expire day orders on date change.
                        if prev_bar is not None and (
                            cur_bar.timestamp[:10] != prev_bar.timestamp[:10]
                        ):
                            expired = order_book.expire_day_orders(cur_bar.timestamp)
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
                                order_book.submit(
                                    req,
                                    submitted_at=prev_bar.timestamp,
                                    submitted_equity=equity,
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
                        for fill in outcome.entry_fills + outcome.exit_fills:
                            harness.send_fill(
                                fill=fill.model_dump(mode="json", exclude_defaults=True),
                                state=self._state(portfolio),
                            )
                        result.trades.extend(outcome.closed_trades)
                        if on_trade is not None:
                            for trade in outcome.closed_trades:
                                on_trade(trade)

                        # 3) Drawdown circuit-breaker.
                        portfolio.update_last_price(cur_bar.symbol, cur_bar.close)
                        equity = portfolio.mark_to_market()
                        dd = self._risk.check_drawdown(equity, portfolio.peak_equity)
                        if dd.breached:
                            result.terminated_reason = (
                                f"max_drawdown breached "
                                f"({dd.current_drawdown_pct:.1f}% >= {dd.limit_pct}%)"
                            )
                            break

                    # 4) Deliver the current bar to the strategy and collect
                    #    any orders it submits in response. Warm-up bars set
                    #    ``ctx.is_warmup = True`` in the subprocess so the
                    #    strategy can short-circuit order emission; we also
                    #    drop any orders it emits anyway as a safety net.
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

                    if is_warmup:
                        if resp.orders:
                            result.warmup_orders_dropped += len(resp.orders)
                            logger.info(
                                "dropped %d order(s) submitted during warm-up bar",
                                len(resp.orders),
                            )
                            for o in resp.orders:
                                _record_event(
                                    result.execution_diagnostics,
                                    "warmup_dropped",
                                    timestamp=cur_bar.timestamp,
                                    symbol=o.get("symbol"),
                                    side=o.get("side"),
                                    order_type=o.get("order_type"),
                                )
                        # Cancels during warm-up are also no-ops (no live order book).
                        prev_bar = cur_bar
                        continue

                    # Map cancels.
                    for c in resp.cancels:
                        oid = c.get("order_id")
                        if oid:
                            order_book.cancel(oid)

                    # Orders submitted now are evaluated against the *next*
                    # bar (look-ahead-safe).
                    for o in resp.orders:
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
                            # structured ``TradingServiceResult.error`` instead
                            # of crashing ``TradingService.run()``. The narrow
                            # subclass keeps unrelated ``NotImplementedError``s
                            # from strategy code in the generic catch below.
                            # See #383.
                            _increment_rejection(
                                result.execution_diagnostics, "unsupported_feature"
                            )
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
                return _finalize_diagnostics(result)
            except StrategyRuntimeError as exc:
                result.error = str(exc)
                result.lookahead_violation = exc.etype == "lookahead_violation"
                return _finalize_diagnostics(result)

        return _finalize_diagnostics(result)

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
