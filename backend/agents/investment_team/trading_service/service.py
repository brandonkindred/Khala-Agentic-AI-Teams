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
from ..models import BacktestConfig, TradeRecord
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


def _partial_fill_defaults_enabled() -> bool:
    """Whether parent-side application of ``default_unfilled_policy`` is on.

    Off by default — Step 3 of #379 ships the wiring; Steps 4+ make the
    downstream engine actually act on the policy. With the flag off, the
    service ignores ``default_unfilled_policy`` entirely (acts as DROP
    regardless), preserving today's byte-identical behavior.
    """
    return os.environ.get("TRADING_PARTIAL_FILL_DEFAULTS_ENABLED", "false").lower() in {
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
                return result

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
                            order_book.expire_day_orders(cur_bar.timestamp)

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
                        try:
                            req = OrderRequest(**o)
                            req.validate_prices()
                            pending_for_prev.append(req)
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
                            raise StrategyRuntimeError(
                                f"strategy emitted an unsupported order: {exc}",
                                etype="unsupported_feature",
                            ) from exc
                        except Exception as exc:  # malformed request from strategy
                            logger.warning("dropping malformed order from strategy: %s", exc)

                    prev_bar = cur_bar

                # End-of-stream: any orders still queued for "next bar" are
                # dropped with a log note — matches the legacy engine's
                # behavior of not fabricating a terminal fill bar.
                if pending_for_prev:
                    logger.info(
                        "%d orders queued at end-of-stream with no next bar; dropped",
                        len(pending_for_prev),
                    )

                harness.send_end()
            except LookAheadError as exc:
                # Parent-side look-ahead guard fired inside the fill
                # simulator: classify the same way as a subprocess-side
                # violation so operators see a single error category.
                result.error = str(exc)
                result.lookahead_violation = True
                return result
            except StrategyRuntimeError as exc:
                result.error = str(exc)
                result.lookahead_violation = exc.etype == "lookahead_violation"
                return result

        return result

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
