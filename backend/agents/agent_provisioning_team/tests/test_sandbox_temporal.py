"""Unit tests for the sandbox lifecycle Temporal surface.

Covers the async sandbox activities, the three sandbox workflows, and the
dispatch helpers (enablement gate, Temporal-vs-direct branch, error unwrapping,
single-instance reaper start). No live Temporal server is needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_provisioning_team.sandbox.state import SandboxHandle, SandboxStatus


def _handle(agent_id: str = "blog.writer") -> SandboxHandle:
    return SandboxHandle(
        agent_id=agent_id,
        team="blogging",
        status=SandboxStatus.WARM,
        container_name=f"sbx-{agent_id}",
        host_port=55123,
    )


# ---------------------------------------------------------------------------
# async activities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_acquire_activity_returns_dump() -> None:
    from agent_provisioning_team.temporal import sandbox_activities as sa

    fake_lc = MagicMock()
    fake_lc.acquire = AsyncMock(return_value=_handle())

    with (
        patch("agent_provisioning_team.sandbox.get_lifecycle", return_value=fake_lc),
        patch("temporalio.activity.heartbeat"),
    ):
        dump = await sa.sandbox_acquire_activity("blog.writer")

    fake_lc.acquire.assert_awaited_once_with("blog.writer")
    assert dump["agent_id"] == "blog.writer"
    assert dump["status"] == "warm"


@pytest.mark.asyncio
async def test_sandbox_acquire_activity_rejects_blank() -> None:
    from agent_provisioning_team.temporal import sandbox_activities as sa

    with patch("temporalio.activity.heartbeat"):
        with pytest.raises(AssertionError):
            await sa.sandbox_acquire_activity("")


@pytest.mark.asyncio
async def test_sandbox_teardown_activity_calls_lifecycle() -> None:
    from agent_provisioning_team.temporal import sandbox_activities as sa

    fake_lc = MagicMock()
    fake_lc.teardown = AsyncMock()

    with (
        patch("agent_provisioning_team.sandbox.get_lifecycle", return_value=fake_lc),
        patch("temporalio.activity.heartbeat"),
    ):
        await sa.sandbox_teardown_activity("blog.writer")

    fake_lc.teardown.assert_awaited_once_with("blog.writer")


@pytest.mark.asyncio
async def test_sandbox_reap_activity_reads_threshold_from_env() -> None:
    from agent_provisioning_team.temporal import sandbox_activities as sa

    fake_lc = MagicMock()
    fake_lc.reap_once = AsyncMock(return_value=["blog.writer"])

    with (
        patch("agent_provisioning_team.sandbox.get_lifecycle", return_value=fake_lc),
        patch("agent_provisioning_team.sandbox.state.idle_teardown_seconds", return_value=123),
        patch("temporalio.activity.heartbeat"),
    ):
        out = await sa.sandbox_reap_activity()

    # Threshold is read inside the activity, never inside the workflow.
    fake_lc.reap_once.assert_awaited_once_with(threshold=123)
    assert out == ["blog.writer"]


# ---------------------------------------------------------------------------
# workflows — direct .run() with stubbed workflow primitives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_acquire_workflow_returns_dump() -> None:
    from agent_provisioning_team.temporal import sandbox_workflows as sw

    captured: dict = {}

    async def fake_exec(activity_fn, *args, **kwargs):
        captured["name"] = getattr(activity_fn, "__name__", str(activity_fn))
        captured["args"] = kwargs.get("args")
        return _handle().model_dump(mode="json")

    with patch.object(sw.workflow, "execute_activity", new=fake_exec):
        out = await sw.SandboxAcquireWorkflow().run("blog.writer")

    assert captured["name"] == "sandbox_acquire_activity"
    assert captured["args"] == ["blog.writer"]
    assert out["agent_id"] == "blog.writer"


@pytest.mark.asyncio
async def test_sandbox_teardown_workflow_calls_activity() -> None:
    from agent_provisioning_team.temporal import sandbox_workflows as sw

    captured: dict = {}

    async def fake_exec(activity_fn, *args, **kwargs):
        captured["name"] = getattr(activity_fn, "__name__", str(activity_fn))
        captured["args"] = kwargs.get("args")
        return None

    with patch.object(sw.workflow, "execute_activity", new=fake_exec):
        await sw.SandboxTeardownWorkflow().run("blog.writer")

    assert captured["name"] == "sandbox_teardown_activity"
    assert captured["args"] == ["blog.writer"]


@pytest.mark.asyncio
async def test_sandbox_reaper_workflow_one_tick() -> None:
    from agent_provisioning_team.temporal import sandbox_workflows as sw

    calls: dict = {}

    async def fake_sleep(delay):
        calls["slept"] = delay

    async def fake_exec(activity_fn, *args, **kwargs):
        calls["activity"] = getattr(activity_fn, "__name__", str(activity_fn))
        return []

    def fake_can(*args):
        calls["continue_as_new"] = args

    with (
        patch.object(sw.workflow, "sleep", new=fake_sleep),
        patch.object(sw.workflow, "execute_activity", new=fake_exec),
        patch.object(sw.workflow, "continue_as_new", new=fake_can),
    ):
        await sw.SandboxReaperWorkflow().run(45)

    assert calls["slept"].total_seconds() == 45
    assert calls["activity"] == "sandbox_reap_activity"
    # Restarts itself with the same interval so history stays bounded.
    assert calls["continue_as_new"] == (45,)


# ---------------------------------------------------------------------------
# dispatch — enablement gate + Temporal-vs-direct branch
# ---------------------------------------------------------------------------


def test_sandbox_temporal_enabled_gates_on_env(monkeypatch) -> None:
    from agent_provisioning_team.temporal import sandbox_dispatch as sd

    monkeypatch.setenv("PROVISION_THREAD_FALLBACK", "1")
    assert sd.sandbox_temporal_enabled() is False

    monkeypatch.delenv("PROVISION_THREAD_FALLBACK", raising=False)
    with patch("agent_provisioning_team.temporal.client.is_temporal_enabled", return_value=True):
        assert sd.sandbox_temporal_enabled() is True
    with patch("agent_provisioning_team.temporal.client.is_temporal_enabled", return_value=False):
        assert sd.sandbox_temporal_enabled() is False


@pytest.mark.asyncio
async def test_acquire_sandbox_falls_back_to_direct() -> None:
    from agent_provisioning_team.temporal import sandbox_dispatch as sd

    direct = AsyncMock(return_value=_handle())
    with (
        patch.object(sd, "sandbox_temporal_enabled", return_value=False),
        patch("agent_provisioning_team.sandbox.acquire", new=direct),
    ):
        out = await sd.acquire_sandbox("blog.writer")

    direct.assert_awaited_once_with("blog.writer")
    assert out.agent_id == "blog.writer"


@pytest.mark.asyncio
async def test_acquire_sandbox_uses_temporal_when_enabled() -> None:
    from agent_provisioning_team.temporal import sandbox_dispatch as sd

    with (
        patch.object(sd, "sandbox_temporal_enabled", return_value=True),
        patch.object(
            sd,
            "execute_workflow_async",
            new=AsyncMock(return_value=_handle().model_dump(mode="json")),
        ),
    ):
        out = await sd.acquire_sandbox("blog.writer")

    assert isinstance(out, SandboxHandle)
    assert out.agent_id == "blog.writer"


@pytest.mark.asyncio
async def test_teardown_sandbox_falls_back_to_direct() -> None:
    from agent_provisioning_team.temporal import sandbox_dispatch as sd

    direct = AsyncMock()
    with (
        patch.object(sd, "sandbox_temporal_enabled", return_value=False),
        patch("agent_provisioning_team.sandbox.teardown", new=direct),
    ):
        await sd.teardown_sandbox("blog.writer")

    direct.assert_awaited_once_with("blog.writer")


@pytest.mark.asyncio
async def test_teardown_sandbox_uses_temporal_when_enabled() -> None:
    from agent_provisioning_team.temporal import sandbox_dispatch as sd

    tmp = AsyncMock()
    with (
        patch.object(sd, "sandbox_temporal_enabled", return_value=True),
        patch.object(sd, "teardown_sandbox_via_temporal", new=tmp),
    ):
        await sd.teardown_sandbox("blog.writer")

    tmp.assert_awaited_once_with("blog.writer")


# ---------------------------------------------------------------------------
# dispatch — error unwrapping (HTTP-status parity with the in-process path)
# ---------------------------------------------------------------------------


class _FakeApplicationError(Exception):
    def __init__(self, type_name: str, message: str) -> None:
        self.type = type_name
        self.message = message
        self.cause = None
        super().__init__(message)


class _FakeWorkflowFailure(Exception):
    def __init__(self, app: _FakeApplicationError) -> None:
        self.cause = app
        super().__init__("workflow failed")


def test_reraise_unwraps_unknown_agent() -> None:
    from agent_provisioning_team.sandbox import UnknownAgentError
    from agent_provisioning_team.temporal import sandbox_dispatch as sd

    exc = _FakeWorkflowFailure(_FakeApplicationError("UnknownAgentError", "no agent"))
    with pytest.raises(UnknownAgentError, match="no agent"):
        sd._reraise_sandbox_error(exc)


def test_reraise_unwraps_docker_unavailable() -> None:
    from agent_provisioning_team.sandbox import DockerUnavailableError
    from agent_provisioning_team.temporal import sandbox_dispatch as sd

    exc = _FakeWorkflowFailure(_FakeApplicationError("DockerUnavailableError", "no docker"))
    with pytest.raises(DockerUnavailableError, match="no docker"):
        sd._reraise_sandbox_error(exc)


def test_reraise_is_noop_for_unknown_type() -> None:
    from agent_provisioning_team.temporal import sandbox_dispatch as sd

    # No recognizable ApplicationError type → returns without raising, caller re-raises.
    sd._reraise_sandbox_error(RuntimeError("plain"))


@pytest.mark.asyncio
async def test_acquire_via_temporal_unwraps_error() -> None:
    from agent_provisioning_team.sandbox import UnknownAgentError
    from agent_provisioning_team.temporal import sandbox_dispatch as sd

    exc = _FakeWorkflowFailure(_FakeApplicationError("UnknownAgentError", "ghost"))
    with (
        patch.object(sd, "execute_workflow_async", new=AsyncMock(side_effect=exc)),
        pytest.raises(UnknownAgentError, match="ghost"),
    ):
        await sd.acquire_sandbox_via_temporal("ghost.agent")


@pytest.mark.asyncio
async def test_acquire_via_temporal_reraises_unrecognized_error() -> None:
    from agent_provisioning_team.temporal import sandbox_dispatch as sd

    # An error with no recognizable ApplicationError type propagates unchanged.
    with (
        patch.object(sd, "execute_workflow_async", new=AsyncMock(side_effect=RuntimeError("boom"))),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await sd.acquire_sandbox_via_temporal("blog.writer")


@pytest.mark.asyncio
async def test_teardown_via_temporal_dispatches_workflow() -> None:
    from agent_provisioning_team.temporal import sandbox_dispatch as sd

    exec_mock = AsyncMock(return_value=None)
    with patch.object(sd, "execute_workflow_async", new=exec_mock):
        await sd.teardown_sandbox_via_temporal("blog.writer")

    exec_mock.assert_awaited_once()
    call = exec_mock.await_args
    assert call.args[0] is sd.SandboxTeardownWorkflow.run
    assert call.args[1] == "blog.writer"
    assert call.kwargs["workflow_id"].startswith("agent-provisioning-sandbox-teardown-blog.writer-")


# ---------------------------------------------------------------------------
# dispatch — single-instance reaper start
# ---------------------------------------------------------------------------


def test_start_reaper_workflow_starts_with_fixed_id() -> None:
    from agent_provisioning_team.temporal import sandbox_dispatch as sd
    from agent_provisioning_team.temporal.constants import SANDBOX_REAPER_WORKFLOW_ID

    captured: dict = {}

    def fake_start(workflow_run, *args, workflow_id, task_queue, **kwargs):
        captured["workflow_id"] = workflow_id
        captured["args"] = args

    with patch.object(sd, "start_workflow_sync", side_effect=fake_start):
        sd.start_sandbox_reaper_workflow(30)

    assert captured["workflow_id"] == SANDBOX_REAPER_WORKFLOW_ID
    assert captured["args"] == (30,)


def test_start_reaper_workflow_swallows_already_started() -> None:
    from agent_provisioning_team.temporal import sandbox_dispatch as sd

    class WorkflowAlreadyStartedError(Exception):
        pass

    with patch.object(sd, "start_workflow_sync", side_effect=WorkflowAlreadyStartedError("dup")):
        # Must not raise — a running reaper IS the desired single-instance state.
        sd.start_sandbox_reaper_workflow()


def test_start_reaper_workflow_reraises_other_errors() -> None:
    from agent_provisioning_team.temporal import sandbox_dispatch as sd

    with patch.object(sd, "start_workflow_sync", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            sd.start_sandbox_reaper_workflow()
