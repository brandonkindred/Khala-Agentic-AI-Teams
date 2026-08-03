"""Unit tests for shared.fencing's check_fencing_token (the promoted, generic primitive).

Covers the bootstrap (no prior token), equal-token (concurrent fan-out),
higher-token, and strictly-lower-token (stale write) cases in isolation
from any store. Mirrors agent_provisioning_team/tests/test_fencing.py,
which now exercises the same behavior through the re-export shim.
"""

from __future__ import annotations

import pytest

from shared.fencing import StaleFencingTokenError, check_fencing_token


def test_accepts_when_no_current_token() -> None:
    """Bootstrap: a resource that has never recorded a token accepts any write."""
    check_fencing_token(
        agent_id="run-1", resource="strategy_lab_run", provided_token=1, current_token=None
    )


def test_accepts_equal_token() -> None:
    """Concurrent fan-out: N activities present the SAME token; all must be accepted."""
    check_fencing_token(
        agent_id="run-1", resource="strategy_lab_run", provided_token=2, current_token=2
    )


def test_accepts_higher_token() -> None:
    check_fencing_token(
        agent_id="run-1", resource="strategy_lab_run", provided_token=3, current_token=2
    )


def test_rejects_lower_token() -> None:
    with pytest.raises(StaleFencingTokenError) as exc_info:
        check_fencing_token(
            agent_id="run-1", resource="strategy_lab_run", provided_token=1, current_token=2
        )

    assert exc_info.value.agent_id == "run-1"
    assert exc_info.value.resource == "strategy_lab_run"
    assert exc_info.value.provided_token == 1
    assert exc_info.value.current_token == 2


def test_rejects_lower_token_error_message_includes_context() -> None:
    with pytest.raises(StaleFencingTokenError) as exc_info:
        check_fencing_token(
            agent_id="run-2", resource="strategy_lab_run", provided_token=0, current_token=1
        )

    message = str(exc_info.value)
    assert "run-2" in message
    assert "strategy_lab_run" in message
    assert "provided=0" in message
    assert "current=1" in message


def test_provided_token_must_be_int() -> None:
    """Regression: enforced with an explicit TypeError, not assert -- assertions
    are stripped under Python's -O flag, which would otherwise silently
    disable this precondition check in an optimized deployment."""
    with pytest.raises(TypeError, match="provided_token must be an int"):
        check_fencing_token(
            agent_id="run-1", resource="strategy_lab_run", provided_token="1", current_token=1
        )
