"""Regression test for the ``PaperTradeTerminatedReason`` enum.

GitHub issue #5240 flagged that the API layer's FAILED-classification check
(``_run_live_paper_trading_background`` in ``investment_team.api.main``)
compared ``terminated_reason`` against an inline set of literal strings that
duplicated values already produced by ``paper_trade.py``. This pins the
enum's values so a future rename in the paper-trade module is caught here
instead of silently breaking the API layer's failure comparison.
"""

from __future__ import annotations

from investment_team.trading_service.modes.paper_trade import (
    PaperTradeTerminatedReason,
)


def test_failure_classifying_members_match_paper_trade_literals() -> None:
    """The four values ``main.py`` treats as session failures."""
    assert PaperTradeTerminatedReason.LOOKAHEAD_VIOLATION.value == "lookahead_violation"
    assert PaperTradeTerminatedReason.PROVIDER_ERROR.value == "provider_error"
    assert PaperTradeTerminatedReason.REGION_BLOCKED.value == "region_blocked"
    assert PaperTradeTerminatedReason.NO_PROVIDER.value == "no_provider"


def test_non_failure_members_match_paper_trade_literals() -> None:
    """The remaining terminal reasons that must NOT be classified as failures."""
    assert PaperTradeTerminatedReason.USER_STOP.value == "user_stop"
    assert PaperTradeTerminatedReason.FILL_TARGET_REACHED.value == "fill_target_reached"
    assert PaperTradeTerminatedReason.MAX_HOURS.value == "max_hours"
    assert PaperTradeTerminatedReason.PROVIDER_END.value == "provider_end"


def test_members_compare_equal_to_their_plain_string_values() -> None:
    """``str`` mixin: a plain string ``terminated_reason`` still matches the enum.

    ``PaperTradeRunResult.terminated_reason`` stays a plain ``str`` field
    (set via literals inside ``paper_trade.py``); the API layer's failure
    set is built from enum members. This must still compare equal via ``in``.
    """
    failures = {
        PaperTradeTerminatedReason.LOOKAHEAD_VIOLATION,
        PaperTradeTerminatedReason.PROVIDER_ERROR,
        PaperTradeTerminatedReason.REGION_BLOCKED,
        PaperTradeTerminatedReason.NO_PROVIDER,
    }
    assert "lookahead_violation" in failures
    assert "provider_error" in failures
    assert "region_blocked" in failures
    assert "no_provider" in failures
    assert "user_stop" not in failures
