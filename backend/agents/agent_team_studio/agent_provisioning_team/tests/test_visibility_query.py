"""Unit tests for shared/visibility_query.py.

Covers query construction, cutoff parsing, agent_id resolution from a fake
history event, and the async filtering/sync-bridge wiring — all against a
fake/mocked Temporal client. No live Temporal server is used.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeAsyncIterator:
    """Minimal async iterator standing in for a Temporal SDK page iterator."""

    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


def _execution(workflow_id, run_id, workflow_type, start_time=None):
    return SimpleNamespace(
        id=workflow_id,
        run_id=run_id,
        workflow_type=workflow_type,
        start_time=start_time or datetime(2026, 7, 1, tzinfo=timezone.utc),
    )


def _started_event(has_started_attrs, payloads=()):
    # `payloads` is kept as the exact object passed in (not copied/rewrapped) so
    # `_fake_client`'s `_decode` can look it up by identity/equality in `decode_table`.
    attrs = SimpleNamespace(input=SimpleNamespace(payloads=payloads))
    event = SimpleNamespace(workflow_execution_started_event_attributes=attrs)
    event.HasField = lambda name: has_started_attrs
    return event


def _fake_client(executions, history_by_run, decode_table):
    client = MagicMock(name="fake-temporal-client")
    client.list_workflows = MagicMock(return_value=_FakeAsyncIterator(executions))

    def _get_workflow_handle(workflow_id, run_id=None):
        events = history_by_run.get(run_id, [])
        handle = MagicMock(name=f"handle-{workflow_id}")
        handle.fetch_history_events = MagicMock(return_value=_FakeAsyncIterator(events))
        return handle

    client.get_workflow_handle = MagicMock(side_effect=_get_workflow_handle)

    async def _decode(payloads):
        return decode_table[payloads]

    client.data_converter = SimpleNamespace(decode=AsyncMock(side_effect=_decode))
    return client


# ---------------------------------------------------------------------------
# _build_query
# ---------------------------------------------------------------------------


def test_build_query_without_cutoff() -> None:
    from agent_team_studio.agent_provisioning_team.shared.visibility_query import _build_query

    query = _build_query(None)
    assert "WorkflowType IN ('AgentProvisioningWorkflow', 'AgentDeprovisioningWorkflow')" in query
    assert "ExecutionStatus = 'Running'" in query
    assert "StartTime" not in query


def test_build_query_with_cutoff() -> None:
    from agent_team_studio.agent_provisioning_team.shared.visibility_query import _build_query

    cutoff = datetime(2026, 7, 17, 12, 30, 0, tzinfo=timezone.utc)
    query = _build_query(cutoff)
    assert "StartTime < '2026-07-17T12:30:00Z'" in query


def test_build_query_converts_non_utc_cutoff() -> None:
    from datetime import timedelta

    from agent_team_studio.agent_provisioning_team.shared.visibility_query import _build_query

    cutoff = datetime(2026, 7, 17, 8, 30, 0, tzinfo=timezone(timedelta(hours=-4)))
    query = _build_query(cutoff)
    assert "StartTime < '2026-07-17T12:30:00Z'" in query


# ---------------------------------------------------------------------------
# _lock_patch_cutoff
# ---------------------------------------------------------------------------


def test_lock_patch_cutoff_unset(monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.shared import visibility_query as vq

    monkeypatch.delenv(vq.LOCK_PATCH_CUTOFF_ENV_VAR, raising=False)
    assert vq._lock_patch_cutoff() is None


def test_lock_patch_cutoff_valid_iso_z(monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.shared import visibility_query as vq

    monkeypatch.setenv(vq.LOCK_PATCH_CUTOFF_ENV_VAR, "2026-07-17T00:00:00Z")
    assert vq._lock_patch_cutoff() == datetime(2026, 7, 17, tzinfo=timezone.utc)


def test_lock_patch_cutoff_naive_treated_as_utc(monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.shared import visibility_query as vq

    monkeypatch.setenv(vq.LOCK_PATCH_CUTOFF_ENV_VAR, "2026-07-17T00:00:00")
    assert vq._lock_patch_cutoff() == datetime(2026, 7, 17, tzinfo=timezone.utc)


def test_lock_patch_cutoff_invalid_falls_back_to_none(monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.shared import visibility_query as vq

    monkeypatch.setenv(vq.LOCK_PATCH_CUTOFF_ENV_VAR, "not-a-timestamp")
    assert vq._lock_patch_cutoff() is None


# ---------------------------------------------------------------------------
# _resolve_agent_id
# ---------------------------------------------------------------------------


def test_resolve_agent_id_unknown_workflow_type_skips_history_fetch() -> None:
    from agent_team_studio.agent_provisioning_team.shared.visibility_query import _resolve_agent_id

    client = _fake_client([], {}, {})
    execution = _execution("wf-1", "run-1", "SomeOtherWorkflow")

    result = asyncio.run(_resolve_agent_id(client, execution))

    assert result is None
    client.get_workflow_handle.assert_not_called()


def test_resolve_agent_id_provisioning_decodes_second_positional_arg() -> None:
    from agent_team_studio.agent_provisioning_team.shared.visibility_query import _resolve_agent_id

    payloads = ("payloads-token",)
    history = {"run-1": [_started_event(True, payloads)]}
    decode_table = {payloads: ["job-1", "agent-42", "/manifest.yaml"]}
    client = _fake_client([], history, decode_table)
    execution = _execution("agent-provisioning-job-1", "run-1", "AgentProvisioningWorkflow")

    result = asyncio.run(_resolve_agent_id(client, execution))

    assert result == "agent-42"


def test_resolve_agent_id_deprovisioning_decodes_first_positional_arg() -> None:
    from agent_team_studio.agent_provisioning_team.shared.visibility_query import _resolve_agent_id

    payloads = ("payloads-token",)
    history = {"run-2": [_started_event(True, payloads)]}
    decode_table = {payloads: ["agent-99", False]}
    client = _fake_client([], history, decode_table)
    execution = _execution(
        "agent-provisioning-deprovision-agent-99-abcd1234", "run-2", "AgentDeprovisioningWorkflow"
    )

    result = asyncio.run(_resolve_agent_id(client, execution))

    assert result == "agent-99"


def test_resolve_agent_id_missing_started_event_returns_none() -> None:
    from agent_team_studio.agent_provisioning_team.shared.visibility_query import _resolve_agent_id

    history = {"run-3": [_started_event(False)]}
    client = _fake_client([], history, {})
    execution = _execution("wf-3", "run-3", "AgentProvisioningWorkflow")

    assert asyncio.run(_resolve_agent_id(client, execution)) is None


def test_resolve_agent_id_short_payload_returns_none() -> None:
    from agent_team_studio.agent_provisioning_team.shared.visibility_query import _resolve_agent_id

    payloads = ("payloads-token",)
    history = {"run-4": [_started_event(True, payloads)]}
    decode_table = {payloads: ["only-one-arg"]}
    client = _fake_client([], history, decode_table)
    execution = _execution("wf-4", "run-4", "AgentProvisioningWorkflow")

    assert asyncio.run(_resolve_agent_id(client, execution)) is None


def test_resolve_agent_id_non_string_value_returns_none() -> None:
    from agent_team_studio.agent_provisioning_team.shared.visibility_query import _resolve_agent_id

    payloads = ("payloads-token",)
    history = {"run-5": [_started_event(True, payloads)]}
    decode_table = {payloads: ["job-1", 12345, "/manifest.yaml"]}
    client = _fake_client([], history, decode_table)
    execution = _execution("wf-5", "run-5", "AgentProvisioningWorkflow")

    assert asyncio.run(_resolve_agent_id(client, execution)) is None


def test_resolve_agent_id_no_history_events_returns_none() -> None:
    from agent_team_studio.agent_provisioning_team.shared.visibility_query import _resolve_agent_id

    client = _fake_client([], {"run-6": []}, {})
    execution = _execution("wf-6", "run-6", "AgentProvisioningWorkflow")

    assert asyncio.run(_resolve_agent_id(client, execution)) is None


def test_resolve_agent_id_swallows_history_fetch_errors() -> None:
    from agent_team_studio.agent_provisioning_team.shared.visibility_query import _resolve_agent_id

    client = _fake_client([], {}, {})
    client.get_workflow_handle.side_effect = RuntimeError("boom")
    execution = _execution("wf-7", "run-7", "AgentProvisioningWorkflow")

    assert asyncio.run(_resolve_agent_id(client, execution)) is None


# ---------------------------------------------------------------------------
# _find_open_pre_patch_executions_async
# ---------------------------------------------------------------------------


def test_find_open_pre_patch_executions_async_returns_all_with_resolved_agent_ids() -> None:
    from agent_team_studio.agent_provisioning_team.shared.visibility_query import (
        _find_open_pre_patch_executions_async,
    )

    prov_payloads = ("prov-token",)
    deprov_payloads = ("deprov-token",)
    executions = [
        _execution("agent-provisioning-job-1", "run-1", "AgentProvisioningWorkflow"),
        _execution(
            "agent-provisioning-deprovision-agent-9-ab12cd34",
            "run-2",
            "AgentDeprovisioningWorkflow",
        ),
    ]
    history = {
        "run-1": [_started_event(True, prov_payloads)],
        "run-2": [_started_event(True, deprov_payloads)],
    }
    decode_table = {
        prov_payloads: ["job-1", "agent-1", "/manifest.yaml"],
        deprov_payloads: ["agent-9", False],
    }
    client = _fake_client(executions, history, decode_table)

    results = asyncio.run(_find_open_pre_patch_executions_async(client, agent_id=None, cutoff=None))

    assert {r.agent_id for r in results} == {"agent-1", "agent-9"}
    assert {r.workflow_type for r in results} == {
        "AgentProvisioningWorkflow",
        "AgentDeprovisioningWorkflow",
    }


def test_find_open_pre_patch_executions_async_filters_by_agent_id() -> None:
    from agent_team_studio.agent_provisioning_team.shared.visibility_query import (
        _find_open_pre_patch_executions_async,
    )

    match_payloads = ("match-token",)
    other_payloads = ("other-token",)
    executions = [
        _execution("wf-match", "run-match", "AgentProvisioningWorkflow"),
        _execution("wf-other", "run-other", "AgentProvisioningWorkflow"),
    ]
    history = {
        "run-match": [_started_event(True, match_payloads)],
        "run-other": [_started_event(True, other_payloads)],
    }
    decode_table = {
        match_payloads: ["job-1", "agent-wanted", "/manifest.yaml"],
        other_payloads: ["job-2", "agent-unwanted", "/manifest.yaml"],
    }
    client = _fake_client(executions, history, decode_table)

    results = asyncio.run(
        _find_open_pre_patch_executions_async(client, agent_id="agent-wanted", cutoff=None)
    )

    assert len(results) == 1
    assert results[0].workflow_id == "wf-match"


def test_find_open_pre_patch_executions_async_keeps_unresolved_agent_id_when_filtering() -> None:
    from agent_team_studio.agent_provisioning_team.shared.visibility_query import (
        _find_open_pre_patch_executions_async,
    )

    executions = [_execution("wf-unresolved", "run-unresolved", "AgentProvisioningWorkflow")]
    client = _fake_client(executions, {"run-unresolved": []}, {})

    results = asyncio.run(
        _find_open_pre_patch_executions_async(client, agent_id="agent-wanted", cutoff=None)
    )

    assert len(results) == 1
    assert results[0].agent_id is None


def test_find_open_pre_patch_executions_async_passes_cutoff_into_query() -> None:
    from agent_team_studio.agent_provisioning_team.shared.visibility_query import (
        _find_open_pre_patch_executions_async,
    )

    client = _fake_client([], {}, {})
    cutoff = datetime(2026, 7, 17, tzinfo=timezone.utc)

    asyncio.run(_find_open_pre_patch_executions_async(client, agent_id=None, cutoff=cutoff))

    called_query = client.list_workflows.call_args.kwargs["query"]
    assert "StartTime < '2026-07-17T00:00:00Z'" in called_query


# ---------------------------------------------------------------------------
# find_open_pre_patch_executions (sync bridge)
# ---------------------------------------------------------------------------


def test_find_open_pre_patch_executions_rejects_empty_agent_id() -> None:
    from agent_team_studio.agent_provisioning_team.shared.visibility_query import (
        find_open_pre_patch_executions,
    )

    with pytest.raises(AssertionError):
        find_open_pre_patch_executions(agent_id="")


def test_find_open_pre_patch_executions_propagates_client_unavailable() -> None:
    from agent_team_studio.agent_provisioning_team.shared import visibility_query as vq

    with patch.object(vq, "await_client", side_effect=RuntimeError("client not ready")):
        with pytest.raises(RuntimeError, match="client not ready"):
            vq.find_open_pre_patch_executions()


def test_find_open_pre_patch_executions_wires_sync_bridge_and_returns_result() -> None:
    from agent_team_studio.agent_provisioning_team.shared import visibility_query as vq

    sentinel_result = [
        vq.PrePatchExecution(
            workflow_id="wf-1",
            run_id="run-1",
            workflow_type="AgentProvisioningWorkflow",
            start_time=datetime(2026, 7, 1, tzinfo=timezone.utc),
            agent_id="agent-1",
        )
    ]
    future = MagicMock()
    future.result.return_value = sentinel_result

    def _submit(coro, _loop):
        coro.close()
        return future

    with (
        patch.object(vq, "await_client", return_value=(MagicMock(), MagicMock())),
        patch.object(asyncio, "run_coroutine_threadsafe", side_effect=_submit),
    ):
        result = vq.find_open_pre_patch_executions(query_timeout_s=5.0)

    assert result == sentinel_result
    future.result.assert_called_once_with(timeout=5.0)
