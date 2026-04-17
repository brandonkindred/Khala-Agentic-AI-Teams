"""Regression tests for the three Codex-flagged PR 2 issues.

Covers:
* ``_resolve_fee_overrides`` preserves explicit zero overrides (``0.0`` is
  a valid user intent for zero-fee / zero-slip experiments).
* ``_recover_orphaned_paper_trading_sessions`` marks sessions in any of
  the PR 2 active states (OPENING / WARMING_UP / LIVE), not just the
  legacy RUNNING state, so SIGKILL orphans cannot block the new
  per-strategy concurrency guard.
"""

from __future__ import annotations

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
