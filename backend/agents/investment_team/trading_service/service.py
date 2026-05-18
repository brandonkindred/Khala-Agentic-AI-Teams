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
from typing import Any, Callable, Dict, Iterable, List, Optional

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
from ..strategy_lab.executor.rule_compiler import (
    BarSnapshot,
    PositionState,
    evaluate_exit_rules,
)
from ..strategy_lab.spec_dsl import ExitRule
from .data_stream.protocol import BarEvent, EndOfStreamEvent, StreamEvent
from .engine.execution_model import build_execution_model
from .engine.fill_simulator import FillOutcome, FillSimulator, FillSimulatorConfig
from .engine.order_book import OrderBook
from .engine.portfolio import Portfolio
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
    """

    side: str  # "long" | "short"
    entry_price: float
    entry_order_id: str
    high_since_entry: float
    low_since_entry: float

    def snapshot(self, symbol: str, qty: float) -> PositionState:
        return PositionState(
            symbol=symbol,
            side=self.side,  # type: ignore[arg-type]
            qty=qty,
            entry_price=self.entry_price,
            high_since_entry=self.high_since_entry,
            low_since_entry=self.low_since_entry,
        )


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
    ) -> None:
        self.strategy_code = strategy_code
        self.config = config
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
        # Issue #527 — engine-issued exit orders bind to the position they
        # close so the fill simulator's stale-continuation guard drops them
        # when a prior strategy exit (limit/stop/GTC) closes the position
        # first. Keyed by client_order_id; consumed when the order moves
        # from ``pending_for_prev`` to the order book.
        engine_exit_bindings: Dict[str, str] = {}
        # Monotonic counter for engine-issued client_order_ids so each
        # rule-triggered close has a distinct id. Strategy ids are emitted
        # client-side; engine ids must not collide with them, hence the
        # ``e`` prefix vs the strategy's ``c`` prefix.
        engine_order_seq = 0
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
                                bound_entry = engine_exit_bindings.pop(req.client_order_id, None)
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
                        # #430: stamp EOD equity for the streaming curve.
                        # Sub-daily bars overwrite the same calendar-day key,
                        # so the last MTM of each trading day wins.
                        eod_buffer.record(cur_bar.timestamp, equity)
                        dd = self._risk.check_drawdown(equity, portfolio.peak_equity)
                        if dd.breached:
                            result.terminated_reason = (
                                f"max_drawdown breached "
                                f"({dd.current_drawdown_pct:.1f}% >= {dd.limit_pct}%)"
                            )
                            break

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

                    # Issue #527 — engine-side enforcement of structured
                    # ``exit_rules``. Runs after the strategy's orders are
                    # queued so we can dedupe against strategy-emitted closes
                    # and any in-flight engine exit on the order book.
                    if self._exit_rules:
                        engine_order_seq = self._maybe_emit_engine_exits(
                            cur_bar=cur_bar,
                            position_tracker=position_tracker,
                            portfolio=portfolio,
                            pending_for_prev=pending_for_prev,
                            order_book=order_book,
                            result=result,
                            engine_order_seq=engine_order_seq,
                            engine_exit_bindings=engine_exit_bindings,
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
                    # #430: stamp EOD equity for the streaming curve.
                    eod_buffer.record(cur_bar.timestamp, equity)
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
        return _finalize_diagnostics(result)

    # ------------------------------------------------------------------
    # Issue #527 — engine-side enforcement of structured ``exit_rules``.
    # ------------------------------------------------------------------

    @staticmethod
    def _update_position_tracker(
        *,
        tracker: Dict[str, _TrackedPosition],
        cur_bar,
        portfolio: Portfolio,
    ) -> None:
        """Reconcile ``tracker`` against ``portfolio.positions`` for one symbol.

        Called after fills are processed each bar. Refreshes
        ``entry_price`` (for scaled-in positions) and extends trailing
        high/low watermarks against the bar. When the underlying
        ``Position`` is replaced (different ``entry_order_id``), the
        tracker resets so a stale trailing watermark can't fire a rule
        on a brand-new trade.
        """
        sym = cur_bar.symbol
        pos = portfolio.positions.get(sym)
        if pos is None:
            # Position closed this bar (or never opened) — drop tracker entry.
            tracker.pop(sym, None)
            return
        existing = tracker.get(sym)
        if existing is not None and existing.entry_order_id == pos.entry_order_id:
            # When a partial entry scales in (REQUEUE_NEXT_BAR / TWAP_N),
            # ``Portfolio.extend`` updates ``pos.entry_price`` to the new
            # weighted-average entry. Mirror that here so
            # ``StopLossRule(basis="entry_price")`` and ``TakeProfitRule``
            # evaluate against the position's current basis rather than
            # the first slice's price.
            existing.entry_price = pos.entry_price
            if cur_bar.high > existing.high_since_entry:
                existing.high_since_entry = cur_bar.high
            if cur_bar.low < existing.low_since_entry:
                existing.low_since_entry = cur_bar.low
        else:
            # Fresh entry this bar — either truly first entry, or a same-bar
            # exit + re-entry replaced the prior position (different
            # ``entry_order_id``). Reset watermarks so a stale trailing high
            # doesn't fire a rule against the new trade.
            tracker[sym] = _TrackedPosition(
                side=("long" if pos.side == OrderSide.LONG else "short"),
                entry_price=pos.entry_price,
                entry_order_id=pos.entry_order_id,
                high_since_entry=cur_bar.high,
                low_since_entry=cur_bar.low,
            )

    def _maybe_emit_engine_exits(
        self,
        *,
        cur_bar,
        position_tracker: Dict[str, _TrackedPosition],
        portfolio: Portfolio,
        pending_for_prev: List[OrderRequest],
        order_book: OrderBook,
        result: "TradingServiceResult",
        engine_order_seq: int,
        engine_exit_bindings: Dict[str, str],
    ) -> int:
        """Evaluate ``self._exit_rules`` against the current bar and emit
        synthetic close orders for any triggered rule.

        Dedup model: the engine always emits at the position's full open
        qty and lets the fill simulator + the position-identity binding
        handle the rest. Specifically:

        * Engine orders carry ``working_against_entry_order_id`` via
          ``engine_exit_bindings`` (see the bar-loop submit step). If a
          same-bar strategy order closes the position first on the next
          bar, the fill simulator's stale-continuation guard drops the
          engine close before it falls through to ``_fill_entry``.
        * If the strategy's same-bar order is partial / clipped (FOK
          rejection, IOC drop, participation cap, REQUEUE_NEXT_BAR
          residual), the engine close sits behind it in submission order
          and ``_fill_exit`` clips ``req.qty`` to ``existing_pos.qty`` —
          residual exposure gets closed on the same bar rather than
          waiting for the rule to fire again.
        * The one explicit guard is on in-flight engine markets: if a
          prior bar's engine exit is still pending (e.g. REQUEUE residual
          across bars), skip re-emission so the order book doesn't
          accumulate redundant engine markets across bars while the rule
          keeps re-triggering.

        Engine-emitted orders carry ``reason="engine_exit:<rule_kind>"`` so
        the conformance gate can count them off the order-lifecycle event
        stream. Returns the updated ``engine_order_seq``.
        """
        sym = cur_bar.symbol
        if sym not in position_tracker:
            return engine_order_seq
        pos = portfolio.positions.get(sym)
        if pos is None or pos.qty <= 0:
            return engine_order_seq

        tracked = position_tracker[sym]
        tracked_side: str = tracked.side

        # Skip if a prior bar's engine market exit is still pending on the
        # book — it will fire when the fill simulator can fill it, and we
        # don't want to stack redundant engine markets across bars while
        # the rule keeps re-triggering against the same open position.
        for po in order_book.pending_for_symbol(sym):
            po_req = po.request
            if po_req.order_type != OrderType.MARKET:
                continue
            po_side = "long" if po_req.side == OrderSide.LONG else "short"
            if po_side != tracked_side and (po_req.reason or "").startswith(
                ENGINE_EXIT_REASON_PREFIX
            ):
                return engine_order_seq

        snapshot = tracked.snapshot(sym, pos.qty)
        bar_snap = BarSnapshot(high=cur_bar.high, low=cur_bar.low, close=cur_bar.close)
        intents = evaluate_exit_rules(
            self._exit_rules,
            {sym: snapshot},
            {sym: bar_snap},
        )
        if not intents:
            return engine_order_seq

        # At most one intent per symbol — ``evaluate_exit_rules`` stops at the
        # first triggered rule per position. Defensive iteration regardless.
        for intent in intents:
            engine_order_seq += 1
            close_side = OrderSide.SHORT if tracked_side == "long" else OrderSide.LONG
            req = OrderRequest(
                client_order_id=f"e{engine_order_seq}",
                symbol=intent.symbol,
                side=close_side,
                qty=pos.qty,
                order_type=OrderType.MARKET,
                tif=TimeInForce.DAY,
                reason=f"{ENGINE_EXIT_REASON_PREFIX}{intent.rule_kind}",
            )
            try:
                req.validate_prices()
            except Exception as exc:  # pragma: no cover — engine-built request
                logger.error(
                    "engine-issued exit order failed validation (rule=%s symbol=%s): %s",
                    intent.rule_kind,
                    sym,
                    exc,
                )
                continue
            pending_for_prev.append(req)
            # Record the binding so the submit step can set
            # ``working_against_entry_order_id`` on the resulting PendingOrder
            # — see :meth:`_update_position_tracker` for why position
            # identity matters and ``fill_simulator.process_bar``'s
            # stale-continuation guard for what consumes the binding.
            engine_exit_bindings[req.client_order_id] = pos.entry_order_id
            result.execution_diagnostics.orders_emitted += 1
            result.execution_diagnostics.exits_emitted += 1
            firings = result.execution_diagnostics.exit_rule_firings
            firings[intent.rule_kind] = firings.get(intent.rule_kind, 0) + 1
            _record_event(
                result.execution_diagnostics,
                "emitted",
                timestamp=cur_bar.timestamp,
                symbol=intent.symbol,
                side=close_side.value,
                order_type=OrderType.MARKET.value,
                reason=req.reason,
            )
        return engine_order_seq

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
