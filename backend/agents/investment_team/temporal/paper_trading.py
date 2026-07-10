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
            - Returns the activity result (``{"session_id", "status"}``), or
              ``{"session_id", "status": "stopped"}`` if the ``stop`` signal
              cancelled the activity before it returned.
        """
        max_hours = float(payload.get("max_hours") or _DEFAULT_MAX_HOURS)
        self._handle = workflow.start_activity(
            run_paper_trading_activity,
            args=[payload],
            start_to_close_timeout=timedelta(hours=max_hours + _TIMEOUT_BUFFER_HOURS),
            heartbeat_timeout=_HEARTBEAT_TIMEOUT,
            retry_policy=_PT_RETRY,
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        try:
            result = await self._handle
        except asyncio.CancelledError:
            self._status = "stopped"
            return {"session_id": payload.get("session_id"), "status": "stopped"}
        self._status = str(result.get("status", "completed"))
        return result

    @workflow.signal
    def stop(self) -> None:
        """Request cancellation of the running session (idempotent)."""
        self._stop_requested = True
        if self._handle is not None:
            self._handle.cancel()

    @workflow.query
    def status(self) -> str:
        """Return the last-known session status."""
        return self._status
