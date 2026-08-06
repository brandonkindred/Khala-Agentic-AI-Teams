"""Regression tests for Codex-flagged paper-trading request/response issues.

Covers:
* ``_resolve_fee_overrides`` preserves explicit zero overrides (``0.0`` is
  a valid user intent for zero-fee / zero-slip experiments).
* ``_recover_orphaned_paper_trading_sessions`` marks sessions in any of
  the PR 2 active states (OPENING / WARMING_UP / LIVE), not just the
  legacy RUNNING state, so SIGKILL orphans cannot block the new
  per-strategy concurrency guard.
* ``RunPaperTradingRequest.timeframe`` rejects values outside its
  documented allowed set at the API boundary instead of only failing later.

The recovery tests run against an in-memory ``FakeJobServiceClient`` swapped
into the module-level ``_paper_trading_sessions`` ``_PersistentDict`` so the
suite no longer requires Postgres or a live job-service.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest
from pydantic import ValidationError

from investment_team.api.main import (
    RunPaperTradingRequest,
    _lock,
    _paper_trading_sessions,
    _recover_orphaned_paper_trading_sessions,
    _resolve_fee_overrides,
)
from investment_team.models import (
    PaperTradingSession,
    PaperTradingStatus,
    StrategySpec,
)


@pytest.fixture(autouse=True)
def _patched_paper_trading_client(monkeypatch, fake_job_client):
    """Redirect the paper-trading ``_PersistentDict`` at the in-memory fake.

    Preconditions: ``_paper_trading_sessions`` exposes a ``_client`` attribute
    matching the ``JobServiceClient`` interface (``get_job``, ``create_job``,
    ``update_job``, ``delete_job``, ``list_jobs``).
    Postconditions: every ``_paper_trading_sessions`` read/write inside this
    test routes through ``fake_job_client``; the original client is restored
    after the test by ``monkeypatch``.
    """
    monkeypatch.setattr(_paper_trading_sessions, "_client", fake_job_client)
    return fake_job_client


# ---------------------------------------------------------------------------
# Fee-override resolution
# ---------------------------------------------------------------------------


def test_explicit_zero_tx_cost_is_preserved() -> None:
    req = RunPaperTradingRequest(
        lab_record_id="x",
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )
    tx, slip = _resolve_fee_overrides(req)
    assert tx == 0.0
    assert slip == 0.0


def test_missing_overrides_fall_back_to_defaults() -> None:
    req = RunPaperTradingRequest(lab_record_id="x")
    tx, slip = _resolve_fee_overrides(req)
    assert tx == 5.0
    assert slip == 2.0


def test_mixed_overrides_default_only_what_is_missing() -> None:
    req = RunPaperTradingRequest(lab_record_id="x", transaction_cost_bps=0.0)
    tx, slip = _resolve_fee_overrides(req)
    assert tx == 0.0  # explicit zero preserved
    assert slip == 2.0  # default applied


def test_nonzero_override_is_preserved() -> None:
    req = RunPaperTradingRequest(
        lab_record_id="x",
        transaction_cost_bps=1.5,
        slippage_bps=3.0,
    )
    tx, slip = _resolve_fee_overrides(req)
    assert tx == 1.5
    assert slip == 3.0


# ---------------------------------------------------------------------------
# timeframe validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "timeframe", ["1s", "15s", "30s", "1m", "5m", "15m", "30m", "1h", "4h", "1d", None]
)
def test_timeframe_accepts_documented_values(timeframe) -> None:
    req = RunPaperTradingRequest(lab_record_id="x", timeframe=timeframe)
    assert req.timeframe == timeframe


def test_timeframe_rejects_undocumented_value() -> None:
    with pytest.raises(ValidationError):
        RunPaperTradingRequest(lab_record_id="x", timeframe="2m")


# ---------------------------------------------------------------------------
# Orphan recovery for PR 2 live statuses
# ---------------------------------------------------------------------------


def _make_session(session_id: str, status: PaperTradingStatus) -> PaperTradingSession:
    return PaperTradingSession(
        session_id=session_id,
        lab_record_id="lr-1",
        strategy=StrategySpec(
            strategy_id=f"strat-{session_id}",
            authored_by="test",
            asset_class="crypto",
            hypothesis="h",
            signal_definition="s",
            timeframe="1d",
        ),
        status=status,
        initial_capital=100_000.0,
        current_capital=100_000.0,
    )


def _install_session(session: PaperTradingSession) -> None:
    _paper_trading_sessions[session.session_id] = session


def _fetch_session(session_id: str) -> PaperTradingSession:
    raw = _paper_trading_sessions[session_id]
    return PaperTradingSession(**raw) if isinstance(raw, dict) else raw


def test_recovery_fails_opening_session() -> None:
    session = _make_session("pt-opening", PaperTradingStatus.OPENING)
    _install_session(session)
    try:
        _recover_orphaned_paper_trading_sessions()
        recovered = _fetch_session("pt-opening")
        assert recovered.status == PaperTradingStatus.FAILED
        assert recovered.terminated_reason == "process_exit"
        assert recovered.error is not None
        assert "did not complete" in recovered.error
    finally:
        _paper_trading_sessions.pop("pt-opening", None)


def test_recovery_fails_warming_up_session() -> None:
    session = _make_session("pt-warm", PaperTradingStatus.WARMING_UP)
    _install_session(session)
    try:
        _recover_orphaned_paper_trading_sessions()
        assert _fetch_session("pt-warm").status == PaperTradingStatus.FAILED
    finally:
        _paper_trading_sessions.pop("pt-warm", None)


def test_recovery_fails_live_session() -> None:
    session = _make_session("pt-live", PaperTradingStatus.LIVE)
    _install_session(session)
    try:
        _recover_orphaned_paper_trading_sessions()
        assert _fetch_session("pt-live").status == PaperTradingStatus.FAILED
    finally:
        _paper_trading_sessions.pop("pt-live", None)


def test_recovery_still_fails_legacy_running_session() -> None:
    """The PR 1 behavior must remain intact after PR 2's extension."""
    session = _make_session("pt-legacy", PaperTradingStatus.RUNNING)
    _install_session(session)
    try:
        _recover_orphaned_paper_trading_sessions()
        assert _fetch_session("pt-legacy").status == PaperTradingStatus.FAILED
    finally:
        _paper_trading_sessions.pop("pt-legacy", None)


def test_recovery_leaves_terminal_sessions_untouched() -> None:
    completed = _make_session("pt-done", PaperTradingStatus.COMPLETED)
    failed = _make_session("pt-err", PaperTradingStatus.FAILED)
    _install_session(completed)
    _install_session(failed)
    try:
        _recover_orphaned_paper_trading_sessions()
        assert _fetch_session("pt-done").status == PaperTradingStatus.COMPLETED
        assert _fetch_session("pt-err").status == PaperTradingStatus.FAILED
    finally:
        _paper_trading_sessions.pop("pt-done", None)
        _paper_trading_sessions.pop("pt-err", None)


def test_recovery_logs_and_skips_unparseable_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unparseable rows are skipped with a warning log (not debug, which is
    typically disabled in production and would let corrupted records go
    unnoticed) that includes the session id when the raw record still has a
    readable one; valid orphans still recover.

    Written directly via the underlying job-service client's ``data`` field
    (bypassing ``_PersistentDict.__setitem__``, which would wrap a plain dict
    as ``{"value": ...}`` instead of storing it flat the way a real
    ``PaperTradingSession.model_dump()`` -- corrupted by, e.g., a schema
    migration dropping a required field -- would be).
    """
    _paper_trading_sessions._client.create_job(
        "pt-bad", status="stored", data={"not": "a-paper-trading-session"}
    )
    _paper_trading_sessions._client.create_job(
        "pt-bad-with-id", status="stored", data={"session_id": "pt-bad-with-id"}
    )
    valid = _make_session("pt-good", PaperTradingStatus.OPENING)
    _install_session(valid)
    try:
        with caplog.at_level(logging.WARNING, logger="investment_team.api.main"):
            _recover_orphaned_paper_trading_sessions()
        skip_records = [
            r
            for r in caplog.records
            if "Paper-trade recovery: skipping unparseable session record" in r.getMessage()
        ]
        assert len(skip_records) == 2
        assert all(r.levelno == logging.WARNING for r in skip_records)
        assert all(r.exc_info is not None for r in skip_records)
        messages = [r.getMessage() for r in skip_records]
        assert any("session_id=unknown" in m for m in messages)
        assert any("session_id=pt-bad-with-id" in m for m in messages)
        assert _fetch_session("pt-good").status == PaperTradingStatus.FAILED
        # Corrupt rows remain present and are not rewritten into valid sessions.
        assert "pt-bad" in _paper_trading_sessions
        assert "pt-bad-with-id" in _paper_trading_sessions
    finally:
        _paper_trading_sessions.pop("pt-bad", None)
        _paper_trading_sessions.pop("pt-bad-with-id", None)
        _paper_trading_sessions.pop("pt-good", None)


def test_recovery_logs_enumeration_failure_at_error_level(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failure enumerating the persisted-session store itself (e.g. the
    store being unreachable/misconfigured) is a non-recoverable
    infrastructure failure, not a single malformed record — the outer
    catch-all around the whole enumerate/parse/mutate/write pass must log it
    at ERROR level (with a traceback), not debug, which is typically
    disabled in production and would leave orphaned sessions unrecovered
    with no operator-visible signal."""

    def _boom():
        raise RuntimeError("job service unavailable")

    monkeypatch.setattr(_paper_trading_sessions, "values", _boom)

    with caplog.at_level(logging.DEBUG, logger="investment_team.api.main"):
        _recover_orphaned_paper_trading_sessions()  # must not raise

    matches = [r for r in caplog.records if "could not enumerate sessions" in r.getMessage()]
    assert len(matches) == 1
    assert matches[0].levelno == logging.ERROR
    assert matches[0].exc_info is not None


def test_recovery_holds_lock_for_entire_pass_against_concurrent_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent writer racing to update the same session must be fully
    serialized against the recovery pass (blocked until recovery releases
    the lock), not interleaved between recovery's snapshot read and its
    write-back. Otherwise recovery would silently clobber the writer's
    newer state with a stale FAILED status.
    """
    session = _make_session("pt-race", PaperTradingStatus.OPENING)
    _install_session(session)

    background_started = threading.Event()
    background_done = threading.Event()
    threads: list[threading.Thread] = []

    def _background_writer() -> None:
        background_started.set()
        with _lock:
            _install_session(_make_session("pt-race", PaperTradingStatus.COMPLETED))
        background_done.set()

    original_values = _paper_trading_sessions.values

    def _values_with_concurrent_writer() -> list:
        result = original_values()
        thread = threading.Thread(target=_background_writer)
        threads.append(thread)
        thread.start()
        assert background_started.wait(timeout=1), "background writer never started"
        # `values()` is only reached here while recovery still holds `_lock`
        # (per the fix). Give the background thread a moment to attempt its
        # own acquisition; if the lock is not held for the whole pass, it
        # will race ahead and finish before recovery writes back. Do NOT
        # join here — the writer can't unblock until recovery itself
        # releases `_lock`, which only happens after this call returns.
        time.sleep(0.05)
        assert not background_done.is_set(), (
            "concurrent writer completed while recovery held only a partial "
            "lock — the fix must hold `_lock` for the entire enumerate/"
            "mutate/write pass"
        )
        return result

    try:
        monkeypatch.setattr(_paper_trading_sessions, "values", _values_with_concurrent_writer)
        _recover_orphaned_paper_trading_sessions()
        for thread in threads:
            thread.join(timeout=1)
        # The writer's update happened after recovery released the lock, so
        # its terminal status is authoritative and must not be overwritten.
        assert _fetch_session("pt-race").status == PaperTradingStatus.COMPLETED
    finally:
        _paper_trading_sessions.pop("pt-race", None)
