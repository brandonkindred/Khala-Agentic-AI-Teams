"""Regression test: the exact scenario issue #1640 exists to close — a
workflow that resumes after its lease was reclaimed during a worker outage
must have its stale-token mutation attempts rejected, and that rejection
must be diagnosable from logs alone.

Unlike ``test_temporal_unit.py``'s per-activity stale-fencing-token tests
(which substitute a ``_FakeStore`` whose ``check_fencing_token`` always
raises), this module drives a *real* ``AgentLockStore`` end to end: acquire
-> simulate TTL expiry -> a second owner reclaims -> the first owner's
mutation attempt, presenting its now-stale token, is rejected by the real
persisted fencing-token high-water mark, not a scripted mock.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest


def test_stale_owner_mutation_rejected_after_reclaim_by_second_owner(
    tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """acquire (token N) -> expire -> reclaim by a second owner (token N+1)
    -> the first owner's compensate_activity call, presenting its stale
    token N, is rejected with StaleFencingTokenError -- and the rejection is
    logged with agent_id, the presented token, and the current token."""
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import (
        AgentLockStore,
        StaleFencingTokenError,
    )
    from agent_team_studio.agent_provisioning_team.temporal import activities

    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    monkeypatch.setattr(
        "agent_team_studio.agent_provisioning_team.temporal.constants.LOCK_TTL_S", 100
    )

    agent_id = "agent-fencing-regression"

    with patch("temporalio.activity.heartbeat"):
        first_token = activities.acquire_agent_lock_activity("job-1", agent_id)
        assert first_token == 1

        # Simulate the first owner's worker being unavailable past
        # LOCK_TTL_S: a real reclaim happens for a different owner once the
        # lease has aged out, exactly as AgentLockStore.acquire's own
        # expiry check would.
        store = AgentLockStore(ttl_seconds=100)
        record = store._read_record(agent_id)
        record["expires_at"] -= 200  # force expiry without sleeping in the test
        store._write_record(agent_id, record)

        second_token = activities.acquire_agent_lock_activity("job-2", agent_id)
        assert second_token == first_token + 1

    # The first owner resumes, unaware its lease was reclaimed, and attempts
    # a resource-mutating call presenting its now-stale token.
    with caplog.at_level(
        logging.ERROR, logger="agent_team_studio.agent_provisioning_team.shared.agent_lock"
    ):
        with pytest.raises(StaleFencingTokenError) as exc_info:
            activities.compensate_activity(
                agent_id,
                [],
                job_id="job-1",
                fencing_token=first_token,
            )

    assert exc_info.value.agent_id == agent_id
    assert exc_info.value.provided_token == first_token
    assert exc_info.value.current_token == second_token

    [record] = [r for r in caplog.records if r.levelname == "ERROR"]
    message = record.getMessage()
    assert agent_id in message
    assert str(first_token) in message
    assert str(second_token) in message


def test_stale_owner_deprovision_mutation_also_rejected(tmp_path, monkeypatch) -> None:
    """The same stale-token rejection applies to deprovision_activity, not
    just compensate_activity -- both are resource-mutating call sites the
    fencing token must guard per #1640's acceptance criteria."""
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import (
        AgentLockStore,
        StaleFencingTokenError,
    )
    from agent_team_studio.agent_provisioning_team.temporal import activities

    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    monkeypatch.setattr(
        "agent_team_studio.agent_provisioning_team.temporal.constants.LOCK_TTL_S", 100
    )

    agent_id = "agent-fencing-regression-deprovision"

    with patch("temporalio.activity.heartbeat"):
        first_token = activities.acquire_agent_lock_activity("job-1", agent_id)

        store = AgentLockStore(ttl_seconds=100)
        record = store._read_record(agent_id)
        record["expires_at"] -= 200
        store._write_record(agent_id, record)

        activities.acquire_agent_lock_activity("job-2", agent_id)

        with pytest.raises(StaleFencingTokenError):
            activities.deprovision_activity(agent_id, fencing_token=first_token)


def test_reclaiming_owners_own_subsequent_mutation_is_accepted(tmp_path, monkeypatch) -> None:
    """Sanity check on the positive path: the *second* (current) owner's
    mutation attempt, presenting its own valid token, is not rejected by the
    fencing check -- only a stale token triggers it."""
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import AgentLockStore
    from agent_team_studio.agent_provisioning_team.temporal import activities

    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    monkeypatch.setattr(
        "agent_team_studio.agent_provisioning_team.temporal.constants.LOCK_TTL_S", 100
    )

    agent_id = "agent-fencing-regression-accept"

    with patch("temporalio.activity.heartbeat"):
        activities.acquire_agent_lock_activity("job-1", agent_id)

        store = AgentLockStore(ttl_seconds=100)
        record = store._read_record(agent_id)
        record["expires_at"] -= 200
        store._write_record(agent_id, record)

        second_token = activities.acquire_agent_lock_activity("job-2", agent_id)

    # check_fencing_token is a lock-free read against the high-water mark
    # only -- calling it directly with the current owner's own token must
    # not raise.
    store.check_fencing_token(agent_id, second_token)
