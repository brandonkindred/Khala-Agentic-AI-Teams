"""Temporal workflow + activity for investment paper-trading sessions.

Paper trading is a long-running background job — the legacy recent-OHLCV replay
finishes in seconds, but the live path (``INVESTMENT_LIVE_PAPER_ENABLED=true``)
streams market data and can run for hours until a fill target / wall-clock guard
/ user stop. That shape — one long unit of work with a cooperative stop — is a
natural Temporal workflow:

* :class:`PaperTradingWorkflow` starts exactly one
  :func:`run_paper_trading_activity` and exposes a ``stop`` **signal** and a
  ``status`` **query**. The signal cancels the running activity, which is how the
  old in-process ``StopController`` poke (``POST …/stop``) now travels durably.
* :func:`run_paper_trading_activity` reuses the team's existing background
  workers verbatim (``_run_live_paper_trading_background`` /
  ``_run_paper_trading_background``) — it never re-implements the trading loop.
  It runs a background heartbeat that both keeps the activity alive and, on
  Temporal cancellation, trips the session's ``StopController`` so the live loop
  ends cleanly at the next bar.

Sandbox-safety: the temporalio workflow sandbox re-imports this module to
register :class:`PaperTradingWorkflow`, so nothing here may touch restricted
builtins (``os.getenv``, time/random, threading) at module top level. Every
heavy import lives inside the activity body, which runs outside the sandbox.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

logger = logging.getLogger(__name__)

# The live session already owns its own termination guards (fill target /
# ``max_hours`` wall clock / user stop) and writes a FAILED session on any
# internal error, so a Temporal-level retry would only re-run a non-idempotent
# session. Bound retries to one attempt — a crash surfaces as a FAILED session
# (recovered by the orphan sweep), not a silent replay.
_PT_RETRY = RetryPolicy(maximum_attempts=1)

# How often the activity heartbeats (and re-checks for Temporal cancellation).
# Short enough that a ``stop`` signal reaches the live loop within one bar, well
# under the heartbeat timeout.
_HEARTBEAT_INTERVAL_S = 10.0
_HEARTBEAT_TIMEOUT = timedelta(seconds=60)

# ``start_to_close_timeout`` = the request's ``max_hours`` safety guard plus a
# buffer for warm-up + provider resolution + final persistence, so Temporal does
# not time the activity out before the session's own wall-clock guard fires.
_TIMEOUT_BUFFER_HOURS = 1.0
_DEFAULT_MAX_HOURS = 72.0


@activity.defn(name="investment_run_paper_trading")
def run_paper_trading_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one paper-trading session to completion inside a Temporal activity.

    Reuses the team's existing background workers unchanged: the live path
    (``_run_live_paper_trading_background``) or the legacy recent-OHLCV path
    (``_run_paper_trading_background``), selected by ``payload["use_live"]``
    exactly as the ``POST /strategy-lab/paper-trade`` route did before. A
    background heartbeat keeps the activity live and, on Temporal cancellation
    (the ``stop`` signal), trips the session's ``StopController`` so the live
    loop terminates cleanly with ``user_stop``.

    Preconditions:
        - ``payload`` carries ``session_id`` (an already-created "running"
          session), ``lab_record_id`` (a winning ``StrategyLabRecord`` with
          executable ``strategy_code``), ``use_live`` and the JSON dump of the
          ``RunPaperTradingRequest`` under ``request``.

    Postconditions:
        - The session under ``session_id`` has been driven to a terminal state
          and persisted by the reused background worker (which owns all status
          transitions and swallows its own errors into a FAILED session).
        - Returns ``{"session_id": str, "status": str}`` read back from the
          persisted session. Raises ``ApplicationError`` only when the lab
          record is missing (a caller precondition violation).
    """
    from investment_team.api.main import (
        RunPaperTradingRequest,
        _fail_paper_trading_session,
        _live_paper_stop_controllers,
        _lock,
        _paper_trading_sessions,
        _run_live_paper_trading_background,
        _run_paper_trading_background,
        _strategy_lab_records,
    )
    from investment_team.models import PaperTradingSession, StrategyLabRecord
    from shared_concurrency import BackgroundHeartbeat

    session_id = payload["session_id"]
    lab_record_id = payload["lab_record_id"]
    use_live = bool(payload.get("use_live"))
    req_data = dict(payload.get("request") or {})

    # Fire-and-forget dispatch + maximum_attempts=1 means nothing else will ever
    # mark this session terminal if this preamble fails (e.g. a concurrent
    # delete of the lab record racing the dispatch, or a malformed persisted
    # record) — without this guard the session would sit stuck non-terminal
    # until the next process-restart orphan sweep.
    try:
        raw_record = _strategy_lab_records.get(lab_record_id)
        if raw_record is None:
            raise ApplicationError(
                f"Strategy lab record '{lab_record_id}' not found",
                type="NotFound",
                non_retryable=True,
            )
        lab_record = StrategyLabRecord.parse_persisted(raw_record)
        strategy = lab_record.strategy
        backtest_record = lab_record.backtest
        strategy_code = lab_record.strategy_code or getattr(strategy, "strategy_code", None)
    except Exception as exc:
        _fail_paper_trading_session(
            session_id, f"Failed to prepare the paper-trading session: {exc}"
        )
        if isinstance(exc, ApplicationError):
            raise
        raise ApplicationError(
            f"Failed to prepare paper-trading session '{session_id}': {exc}",
            type=type(exc).__name__,
            non_retryable=True,
        ) from exc

    def _beat() -> None:
        # Best-effort: outside a real activity context (e.g. a direct-call unit
        # test) these raise and BackgroundHeartbeat swallows them.
        activity.heartbeat()
        if activity.is_cancelled():
            with _lock:
                controller = _live_paper_stop_controllers.get(session_id)
            if controller is not None:
                controller.request_stop()

    with BackgroundHeartbeat(
        _beat,
        _HEARTBEAT_INTERVAL_S,
        copy_context=True,
        name=f"paper-trade-hb-{session_id}",
    ):
        if use_live:
            _run_live_paper_trading_background(
                session_id,
                lab_record_id,
                strategy,
                RunPaperTradingRequest(**req_data),
            )
        else:
            _run_paper_trading_background(
                session_id,
                lab_record_id,
                strategy,
                strategy_code,
                backtest_record,
                req_data.get("lookback_days", 365),
                req_data.get("initial_capital", 100000.0),
                req_data.get("transaction_cost_bps"),
                req_data.get("slippage_bps"),
            )

    with _lock:
        raw = _paper_trading_sessions.get(session_id)
    status = "unknown"
    if raw is not None:
        status = PaperTradingSession.parse_persisted(raw).status.value
    return {"session_id": session_id, "status": status}


@activity.defn(name="investment_mark_paper_trading_stopped")
def mark_paper_trading_stopped_activity(session_id: str) -> dict[str, Any]:
    """Persist a terminal state for a session stopped before its run began.

    Compensation for the race where ``stop`` is signalled while
    :func:`run_paper_trading_activity` is still *scheduled* — that activity is
    cancelled before it can create its StopController / write the session, so
    without this the session would sit ``running``/``opening`` forever (blocking
    future live starts). Idempotent: leaves an already-terminal session as-is.

    Preconditions:
        - ``session_id`` may or may not exist in ``_paper_trading_sessions``.
    Postconditions:
        - When the session exists and is not already ``COMPLETED``/``FAILED``, it
          is marked ``FAILED`` with ``terminated_reason="user_stop"`` and a
          ``completed_at`` stamp. Returns ``{"session_id", "status"}``.
    """
    from investment_team.api.main import _lock, _paper_trading_sessions
    from investment_team.models import PaperTradingSession, PaperTradingStatus

    with _lock:
        raw = _paper_trading_sessions.get(session_id)
        if raw is None:
            return {"session_id": session_id, "status": "unknown"}
        session = PaperTradingSession.parse_persisted(raw)
        if session.status in (PaperTradingStatus.COMPLETED, PaperTradingStatus.FAILED):
            return {"session_id": session_id, "status": session.status.value}
        from datetime import datetime, timezone

        session.status = PaperTradingStatus.FAILED
        session.terminated_reason = "user_stop"
        session.error = "Paper trading stopped before the session started."
        session.completed_at = datetime.now(tz=timezone.utc).isoformat()
        _paper_trading_sessions[session_id] = session
    return {"session_id": session_id, "status": session.status.value}


# Bound, retrying policy for the short compensation activity (it only writes the
# job store, so a transient store error is worth a couple of retries).
_STOP_COMPENSATION_RETRY = RetryPolicy(maximum_attempts=3)
_STOP_COMPENSATION_TIMEOUT = timedelta(seconds=30)


@workflow.defn(name="InvestmentPaperTradingWorkflow")
class PaperTradingWorkflow:
    """Durable wrapper around a single paper-trading session.

    Invariants:
        - Exactly one :func:`run_paper_trading_activity` is started per run.
        - A ``stop`` signal cancels that activity (idempotent); the resulting
          terminal status is surfaced via the ``status`` query and the return
          value.
    """

    def __init__(self) -> None:
        self._stop_requested = False
        self._status = "running"
        self._handle: Optional[Any] = None

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Start the paper-trading activity and await its terminal state.

        Preconditions:
            - ``payload`` satisfies :func:`run_paper_trading_activity`'s
              preconditions and may carry ``max_hours`` (the request's
              wall-clock guard) used to size the activity timeout.

        Postconditions:
            - Returns the activity result (``{"session_id", "status"}``). On a
              ``stop`` signal before the activity completes, cancels it and
              *waits for that cancellation to be reconciled* (the activity's own
              cooperative shutdown — via the heartbeat tripping the session's
              ``StopController`` — writes the session's real terminal state, same
              as a natural completion) before running
              :func:`mark_paper_trading_stopped_activity` as a backstop. Because
              the backstop runs only after the real activity has already had its
              chance to write a terminal record, it is a true no-op except in the
              narrow cancel-before-start race it exists for (the activity never
              ran at all). Returns ``{"session_id", "status": "stopped"}``.
        """
        session_id = payload.get("session_id")
        max_hours = float(payload.get("max_hours") or _DEFAULT_MAX_HOURS)
        self._handle = workflow.start_activity(
            run_paper_trading_activity,
            args=[payload],
            start_to_close_timeout=timedelta(hours=max_hours + _TIMEOUT_BUFFER_HOURS),
            heartbeat_timeout=_HEARTBEAT_TIMEOUT,
            retry_policy=_PT_RETRY,
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )

        # Race the activity against a stop signal. ``wait_condition`` wakes on the
        # signal or on the activity completing (both are workflow events).
        await workflow.wait_condition(lambda: self._stop_requested or self._handle.done())

        if self._stop_requested and not self._handle.done():
            self._status = "stopped"
            self._handle.cancel()
            # ``WAIT_CANCELLATION_COMPLETED`` means this await blocks until the
            # activity itself finishes (gracefully, via the heartbeat-driven
            # StopController, or because it never started at all) — i.e. until
            # any real terminal write it was going to make has already landed.
            try:
                await self._handle
            except asyncio.CancelledError:
                pass
            # Backstop, run only now that the real activity has been fully
            # reconciled above: idempotent no-op when it already wrote a
            # terminal session; only mutates state for the cancel-before-start
            # race (the activity was cancelled before it ever ran). Best-effort
            # — a persistent compensation failure must not fail the whole
            # workflow after the primary goal (stopping the activity) already
            # succeeded.
            try:
                await workflow.execute_activity(
                    mark_paper_trading_stopped_activity,
                    args=[session_id],
                    start_to_close_timeout=_STOP_COMPENSATION_TIMEOUT,
                    retry_policy=_STOP_COMPENSATION_RETRY,
                )
            except Exception:
                workflow.logger.warning(
                    "mark_paper_trading_stopped_activity failed for session %s; "
                    "session may remain non-terminal until the orphan sweep.",
                    session_id,
                )
            return {"session_id": session_id, "status": "stopped"}

        result = await self._handle
        self._status = str(result.get("status", "completed"))
        return result

    @workflow.signal
    def stop(self) -> None:
        """Request the session stop (idempotent). The run loop owns the cancel."""
        self._stop_requested = True

    @workflow.query
    def status(self) -> str:
        """Return the workflow's own coarse execution state.

        One of ``"running"``/``"stopped"``/the activity's terminal status
        string — the workflow's own state machine, not the trading session's
        finer-grained status (``OPENING``/``WARMING_UP``/``LIVE``/etc.).
        Clients that need that detail read the persisted session via
        ``GET /strategy-lab/paper-trade/{session_id}``, which is where it's
        actually surfaced today.
        """
        return self._status
