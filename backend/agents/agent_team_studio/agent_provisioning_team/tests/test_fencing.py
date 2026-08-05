"""Unit tests for shared/fencing.py's check_fencing_token.

Covers the bootstrap (no prior token), equal-token (concurrent fan-out),
and strictly-lower-token (stale write) cases in isolation from any store.
"""

from __future__ import annotations

import pytest

from agent_team_studio.agent_provisioning_team.shared.fencing import (
    StaleFencingTokenError,
    check_fencing_token,
)


def test_accepts_when_no_current_token() -> None:
    """Bootstrap: a resource that has never recorded a token accepts any write."""
    check_fencing_token(
        agent_id="agent-1", resource="environment_store", provided_token=1, current_token=None
    )


def test_accepts_equal_token() -> None:
    """Concurrent fan-out: N activities present the SAME token; all must be accepted."""
    check_fencing_token(
        agent_id="agent-1", resource="environment_store", provided_token=5, current_token=5
    )


def test_accepts_higher_token() -> None:
    check_fencing_token(
        agent_id="agent-1", resource="environment_store", provided_token=6, current_token=5
    )


def test_rejects_lower_token() -> None:
    with pytest.raises(StaleFencingTokenError) as exc_info:
        check_fencing_token(
            agent_id="agent-1", resource="environment_store", provided_token=4, current_token=5
        )

    assert exc_info.value.agent_id == "agent-1"
    assert exc_info.value.resource == "environment_store"
    assert exc_info.value.provided_token == 4
    assert exc_info.value.current_token == 5


def test_rejects_lower_token_for_provisioner_state_resource_label() -> None:
    with pytest.raises(StaleFencingTokenError) as exc_info:
        check_fencing_token(
            agent_id="agent-2",
            resource="provisioner_state:docker_provisioner",
            provided_token=1,
            current_token=2,
        )

    assert exc_info.value.resource == "provisioner_state:docker_provisioner"
