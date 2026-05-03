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
        bar_chunk_size: Optional[int] = None,
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

        chunk_size = self._chunk_size_override
        if chunk_size is None:
            chunk_size = _resolve_bar_chunk_size()

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
                    # 1) Expire day orders on date change.
                    if prev_bar is not None and (cur_bar.timestamp[:10] != prev_bar.timestamp[:10]):
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
                        # send_fill is per-fill; happens between chunks too.
                        # The strategy sees fills from the *previous* chunk
                        # before its next chunk arrives.
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
                        chunk_buffer.clear()
                        return False

                    result.bars_processed += 1

                # 4) Process the strategy's response for this bar.
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
                    prev_bar = cur_bar
                    continue

                for c in bar_cancels:
                    oid = c.get("order_id")
                    if oid:
                        order_book.cancel(oid)

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
                        held = portfolio.positions.get(req.symbol)
                        if held is not None and held.side != req.side:
                            result.execution_diagnostics.exits_emitted += 1
                    except UnsupportedOrderFeatureError as exc:
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
                        chunk_buffer.clear()
                        raise StrategyRuntimeError(
                            f"strategy emitted an unsupported order: {exc}",
                            etype="unsupported_feature",
                        ) from exc
                    except Exception as exc:
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
