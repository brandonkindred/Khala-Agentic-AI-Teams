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

import pytest
from pydantic import ValidationError

from investment_team.api.main import (
    RunPaperTradingRequest,
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
    """Unparseable rows are skipped with a debug log; valid orphans still recover."""
    _paper_trading_sessions["pt-bad"] = {"not": "a-paper-trading-session"}
    valid = _make_session("pt-good", PaperTradingStatus.OPENING)
    _install_session(valid)
    try:
        with caplog.at_level(logging.DEBUG, logger="investment_team.api.main"):
            _recover_orphaned_paper_trading_sessions()
        skip_records = [
            r
            for r in caplog.records
            if "Paper-trade recovery: skipping unparseable session record" in r.getMessage()
        ]
        assert len(skip_records) == 1
        assert skip_records[0].exc_info is not None
        assert _fetch_session("pt-good").status == PaperTradingStatus.FAILED
        # Corrupt row remains present and is not rewritten into a valid session.
        assert "pt-bad" in _paper_trading_sessions
    finally:
        _paper_trading_sessions.pop("pt-bad", None)
        _paper_trading_sessions.pop("pt-good", None)
