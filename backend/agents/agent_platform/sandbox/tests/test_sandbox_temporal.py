"""Unit tests for the sandbox lifecycle Temporal surface.

Covers the async sandbox activities, the three sandbox workflows, and the
dispatch helpers (enablement gate, Temporal-vs-direct branch, error unwrapping,
single-instance reaper start). No live Temporal server is needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_platform.sandbox.state import SandboxHandle, SandboxStatus


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
    from agent_platform.sandbox.temporal import activities as sa

    fake_lc = MagicMock()
    fake_lc.acquire = AsyncMock(return_value=_handle())

    with (
        patch("agent_platform.sandbox.get_lifecycle", return_value=fake_lc),
        patch("temporalio.activity.heartbeat"),
    ):
        dump = await sa.sandbox_acquire_activity("blog.writer")

    fake_lc.acquire.assert_awaited_once_with("blog.writer")
    assert dump["agent_id"] == "blog.writer"
    assert dump["status"] == "warm"


@pytest.mark.asyncio
async def test_sandbox_acquire_activity_rejects_blank() -> None:
    from agent_platform.sandbox.temporal import activities as sa

    with patch("temporalio.activity.heartbeat"):
        with pytest.raises(AssertionError):
            await sa.sandbox_acquire_activity("")


@pytest.mark.asyncio
async def test_sandbox_acquire_activity_raises_on_error_status() -> None:
    """Lifecycle.acquire() never raises for a transient failure — it returns a
    non-raising ERROR-status handle so its direct/thread-mode callers always
    get a handle back. The activity must re-raise on that status so
    SANDBOX_ACQUIRE_RETRY_POLICY actually retries; otherwise Temporal sees the
    activity as a success and the retry policy is dead code. Raises the
    dedicated SandboxAcquireFailedError (not a bare RuntimeError) so
    _reraise_sandbox_error can recognize and translate it once retries are
    exhausted, instead of leaking an opaque WorkflowFailureError."""
    from agent_platform.sandbox import SandboxAcquireFailedError
    from agent_platform.sandbox.temporal import activities as sa

    error_handle = _handle()
    error_handle.status = SandboxStatus.ERROR
    error_handle.error = "docker run failed: transient"
    fake_lc = MagicMock()
    fake_lc.acquire = AsyncMock(return_value=error_handle)

    with (
        patch("agent_platform.sandbox.get_lifecycle", return_value=fake_lc),
        patch("temporalio.activity.heartbeat"),
    ):
        with pytest.raises(SandboxAcquireFailedError, match="docker run failed: transient"):
            await sa.sandbox_acquire_activity("blog.writer")


@pytest.mark.asyncio
async def test_sandbox_teardown_activity_calls_lifecycle() -> None:
    from agent_platform.sandbox.temporal import activities as sa

    fake_lc = MagicMock()
    fake_lc.teardown = AsyncMock()

    with (
        patch("agent_platform.sandbox.get_lifecycle", return_value=fake_lc),
        patch("temporalio.activity.heartbeat"),
    ):
        await sa.sandbox_teardown_activity("blog.writer")

    fake_lc.teardown.assert_awaited_once_with("blog.writer")


@pytest.mark.asyncio
async def test_sandbox_reap_activity_reads_threshold_from_env() -> None:
    from agent_platform.sandbox.temporal import activities as sa

    fake_lc = MagicMock()
    fake_lc.reap_once = AsyncMock(return_value=["blog.writer"])

    with (
        patch("agent_platform.sandbox.get_lifecycle", return_value=fake_lc),
        patch(
            "agent_platform.sandbox.state.idle_teardown_seconds",
            return_value=123,
        ),
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
    from agent_platform.sandbox.temporal import workflows as sw

    captured: dict = {}

    async def fake_exec(activity_fn, *args, **kwargs):
        captured["name"] = getattr(activity_fn, "__name__", str(activity_fn))
        captured["args"] = kwargs.get("args")
        captured["task_queue"] = kwargs.get("task_queue")
        return _handle().model_dump(mode="json")

    with patch.object(sw.workflow, "execute_activity", new=fake_exec):
        out = await sw.SandboxAcquireWorkflow().run("blog.writer")

    assert captured["name"] == "sandbox_acquire_activity"
    assert captured["args"] == ["blog.writer"]
    assert out["agent_id"] == "blog.writer"
    # P1 regression: the activity must be scheduled on SANDBOX_TASK_QUEUE.
    assert captured["task_queue"] == sw.SANDBOX_TASK_QUEUE


@pytest.mark.asyncio
async def test_sandbox_acquire_workflow_rejects_blank_agent_id() -> None:
    from agent_platform.sandbox.temporal import workflows as sw

    with pytest.raises(AssertionError):
        await sw.SandboxAcquireWorkflow().run("")


@pytest.mark.asyncio
async def test_sandbox_teardown_workflow_calls_activity() -> None:
    from agent_platform.sandbox.temporal import workflows as sw

    captured: dict = {}

    async def fake_exec(activity_fn, *args, **kwargs):
        captured["name"] = getattr(activity_fn, "__name__", str(activity_fn))
        captured["args"] = kwargs.get("args")
        captured["task_queue"] = kwargs.get("task_queue")
        return None

    with patch.object(sw.workflow, "execute_activity", new=fake_exec):
        await sw.SandboxTeardownWorkflow().run("blog.writer")

    assert captured["name"] == "sandbox_teardown_activity"
    assert captured["args"] == ["blog.writer"]
    assert captured["task_queue"] == sw.SANDBOX_TASK_QUEUE


@pytest.mark.asyncio
async def test_sandbox_teardown_workflow_rejects_blank_agent_id() -> None:
    from agent_platform.sandbox.temporal import workflows as sw

    with pytest.raises(AssertionError):
        await sw.SandboxTeardownWorkflow().run("")


@pytest.mark.asyncio
async def test_sandbox_reaper_workflow_one_tick() -> None:
    from agent_platform.sandbox.temporal import workflows as sw

    calls: dict = {}

    async def fake_sleep(delay):
        calls["slept"] = delay

    async def fake_exec(activity_fn, *args, **kwargs):
        calls["activity"] = getattr(activity_fn, "__name__", str(activity_fn))
        calls["task_queue"] = kwargs.get("task_queue")
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
    assert calls["task_queue"] == sw.SANDBOX_TASK_QUEUE
    # Restarts itself with the same interval so history stays bounded.
    assert calls["continue_as_new"] == (45,)


@pytest.mark.asyncio
async def test_sandbox_reaper_workflow_run_rejects_non_positive_interval() -> None:
    from agent_platform.sandbox.temporal import workflows as sw

    with pytest.raises(AssertionError):
        await sw.SandboxReaperWorkflow().run(0)


@pytest.mark.asyncio
async def test_sandbox_reaper_workflow_survives_activity_failure() -> None:
    """A reap tick that fails even after SANDBOX_RETRY_POLICY's retries is
    caught and logged; continue_as_new must still run, so a single bad tick
    (e.g. Docker briefly unreachable) can never permanently kill this
    single-instance durable workflow."""
    from agent_platform.sandbox.temporal import workflows as sw

    calls: dict = {}

    async def fake_sleep(delay):
        calls["slept"] = delay

    async def fake_exec(activity_fn, *args, **kwargs):
        raise RuntimeError("docker daemon unreachable")

    def fake_can(*args):
        calls["continue_as_new"] = args

    with (
        patch.object(sw.workflow, "sleep", new=fake_sleep),
        patch.object(sw.workflow, "execute_activity", new=fake_exec),
        patch.object(sw.workflow, "continue_as_new", new=fake_can),
        patch.object(sw.workflow, "logger") as mock_logger,
    ):
        # Must NOT raise — the workflow catches, logs, and keeps going.
        await sw.SandboxReaperWorkflow().run(45)

    assert calls["continue_as_new"] == (45,)
    # The failure must actually be logged, not just silently swallowed.
    mock_logger.exception.assert_called_once()


# ---------------------------------------------------------------------------
# dispatch — enablement gate + Temporal-vs-direct branch
# ---------------------------------------------------------------------------


def test_sandbox_temporal_enabled_follows_is_temporal_enabled(monkeypatch) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "localhost:7233")
    from agent_platform.sandbox.temporal import dispatch as sd

    assert sd.sandbox_temporal_enabled() is True


def test_sandbox_temporal_disabled_without_address(monkeypatch) -> None:
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from agent_platform.sandbox.temporal import dispatch as sd

    assert sd.sandbox_temporal_enabled() is False


@pytest.mark.asyncio
async def test_acquire_sandbox_falls_back_to_direct() -> None:
    from agent_platform.sandbox.temporal import dispatch as sd

    direct = AsyncMock(return_value=_handle())
    with (
        patch.object(sd, "sandbox_temporal_enabled", return_value=False),
        patch.object(sd, "_acquire_sandbox_inprocess", new=direct),
    ):
        out = await sd.acquire_sandbox("blog.writer")

    direct.assert_awaited_once_with("blog.writer")
    assert out.agent_id == "blog.writer"


@pytest.mark.asyncio
async def test_acquire_sandbox_uses_temporal_when_enabled() -> None:
    from agent_platform.sandbox.temporal import dispatch as sd

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
    from agent_platform.sandbox.temporal import dispatch as sd

    direct = AsyncMock()
    with (
        patch.object(sd, "sandbox_temporal_enabled", return_value=False),
        patch.object(sd, "_teardown_sandbox_inprocess", new=direct),
    ):
        await sd.teardown_sandbox("blog.writer")

    direct.assert_awaited_once_with("blog.writer")


@pytest.mark.asyncio
async def test_teardown_sandbox_uses_temporal_when_enabled() -> None:
    from agent_platform.sandbox.temporal import dispatch as sd

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
        super().__init__(message)


def _fake_workflow_failure(app: _FakeApplicationError) -> Exception:
    """Build a WorkflowFailureError-shaped exception using the standard
    __cause__ chaining attribute (matching real Temporal — FailureError.cause
    is documented as an alias of __cause__ — and the shared
    shared.temporal.translate_workflow_failure walk, which reads
    __cause__/__context__, not a nonstandard '.cause' attribute)."""
    failure = Exception("workflow failed")
    failure.__cause__ = app
    return failure


def test_reraise_unwraps_unknown_agent() -> None:
    from agent_platform.sandbox import UnknownAgentError
    from agent_platform.sandbox.temporal import dispatch as sd

    exc = _fake_workflow_failure(_FakeApplicationError("UnknownAgentError", "no agent"))
    with pytest.raises(UnknownAgentError, match="no agent"):
        sd._reraise_sandbox_error(exc)


def test_reraise_unwraps_docker_unavailable() -> None:
    from agent_platform.sandbox import DockerUnavailableError
    from agent_platform.sandbox.temporal import dispatch as sd

    exc = _fake_workflow_failure(_FakeApplicationError("DockerUnavailableError", "no docker"))
    with pytest.raises(DockerUnavailableError, match="no docker"):
        sd._reraise_sandbox_error(exc)


def test_reraise_unwraps_docker_error() -> None:
    """DockerError (e.g. from teardown's stop_container) must round-trip as
    itself through Temporal, matching what reap_once()'s own
    `except provisioner_mod.DockerError:` handler expects around the
    in-process teardown() call."""
    from agent_platform.sandbox.provisioner import DockerError
    from agent_platform.sandbox.temporal import dispatch as sd

    exc = _fake_workflow_failure(_FakeApplicationError("DockerError", "daemon unreachable"))
    with pytest.raises(DockerError, match="daemon unreachable"):
        sd._reraise_sandbox_error(exc)


def test_reraise_unwraps_sandbox_acquire_failed() -> None:
    """Once SANDBOX_ACQUIRE_RETRY_POLICY's retries are exhausted, the
    SandboxAcquireFailedError marker must round-trip back to itself so
    warm_sandbox can map it to a clean 503 instead of an opaque
    WorkflowFailureError."""
    from agent_platform.sandbox import SandboxAcquireFailedError
    from agent_platform.sandbox.temporal import dispatch as sd

    exc = _fake_workflow_failure(
        _FakeApplicationError("SandboxAcquireFailedError", "docker daemon unreachable")
    )
    with pytest.raises(SandboxAcquireFailedError, match="docker daemon unreachable"):
        sd._reraise_sandbox_error(exc)


@pytest.mark.asyncio
async def test_acquire_via_temporal_unwraps_sandbox_acquire_failed() -> None:
    """End-to-end: after SandboxAcquireWorkflow's retries exhaust, the caller
    (e.g. warm_sandbox) gets back SandboxAcquireFailedError, not a raw
    WorkflowFailureError."""
    from agent_platform.sandbox import SandboxAcquireFailedError
    from agent_platform.sandbox.temporal import dispatch as sd

    exc = _fake_workflow_failure(
        _FakeApplicationError("SandboxAcquireFailedError", "retries exhausted")
    )
    with (
        patch.object(sd, "execute_workflow_async", new=AsyncMock(side_effect=exc)),
        pytest.raises(SandboxAcquireFailedError, match="retries exhausted"),
    ):
        await sd.acquire_sandbox_via_temporal("blog.writer")


def test_reraise_is_noop_for_unknown_type() -> None:
    from agent_platform.sandbox.temporal import dispatch as sd

    # No recognizable ApplicationError type → returns without raising, caller re-raises.
    sd._reraise_sandbox_error(RuntimeError("plain"))


def test_reraise_mapping_covers_every_sandbox_exception_type() -> None:
    """Regression guard: _reraise_sandbox_error's type mapping must cover
    every exception type agent_platform.sandbox exports (plus
    DockerError from provisioner), so a new sandbox exception type added
    without updating this mapping fails this test instead of silently
    leaking as an opaque WorkflowFailureError (a raw 500) to callers."""
    import agent_platform.sandbox as sandbox_pkg
    from agent_platform.sandbox.provisioner import DockerError
    from agent_platform.sandbox.temporal import dispatch as sd

    expected = {
        name
        for name in sandbox_pkg.__all__
        if isinstance(getattr(sandbox_pkg, name), type)
        and issubclass(getattr(sandbox_pkg, name), Exception)
    }
    expected.add(DockerError.__name__)

    captured: dict = {}

    def fake_translate(exc, mapping):
        captured["mapping"] = mapping

    with patch.object(sd, "translate_workflow_failure", new=fake_translate):
        sd._reraise_sandbox_error(RuntimeError("dummy"))

    assert set(captured["mapping"]) == expected


@pytest.mark.asyncio
async def test_acquire_via_temporal_unwraps_error() -> None:
    from agent_platform.sandbox import UnknownAgentError
    from agent_platform.sandbox.temporal import dispatch as sd

    exc = _fake_workflow_failure(_FakeApplicationError("UnknownAgentError", "ghost"))
    with (
        patch.object(sd, "execute_workflow_async", new=AsyncMock(side_effect=exc)),
        pytest.raises(UnknownAgentError, match="ghost"),
    ):
        await sd.acquire_sandbox_via_temporal("ghost.agent")


@pytest.mark.asyncio
async def test_acquire_via_temporal_reraises_unrecognized_error() -> None:
    from agent_platform.sandbox.temporal import dispatch as sd

    # An error with no recognizable ApplicationError type propagates unchanged.
    with (
        patch.object(sd, "execute_workflow_async", new=AsyncMock(side_effect=RuntimeError("boom"))),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await sd.acquire_sandbox_via_temporal("blog.writer")


@pytest.mark.asyncio
async def test_acquire_via_temporal_uses_client_timeout_that_covers_retries() -> None:
    """The client wait must exceed the workflow's own worst-case retry budget
    (SANDBOX_ACQUIRE_TIMEOUT_S per attempt x up to 3 attempts), or a
    legitimately-retrying-but-eventually-successful acquire is mistaken for a
    hung one and surfaces as an unhandled client-side timeout."""
    from agent_platform.sandbox.temporal import dispatch as sd
    from agent_platform.sandbox.temporal.constants import (
        SANDBOX_ACQUIRE_CLIENT_TIMEOUT_S,
        SANDBOX_ACQUIRE_TIMEOUT_S,
    )

    exec_mock = AsyncMock(return_value=_handle().model_dump(mode="json"))
    with patch.object(sd, "execute_workflow_async", new=exec_mock):
        await sd.acquire_sandbox_via_temporal("blog.writer")

    call = exec_mock.await_args
    assert call.kwargs["execute_timeout_s"] == SANDBOX_ACQUIRE_CLIENT_TIMEOUT_S
    # Must cover at least a full single-attempt timeout with room for retries.
    assert SANDBOX_ACQUIRE_CLIENT_TIMEOUT_S > SANDBOX_ACQUIRE_TIMEOUT_S


@pytest.mark.asyncio
async def test_acquire_via_temporal_dispatches_on_sandbox_task_queue() -> None:
    """P1 regression: must dispatch on SANDBOX_TASK_QUEUE, never the shared
    TASK_QUEUE the standalone agent-provisioning-service team container also
    polls — otherwise Temporal could run this activity in that other
    process's own (separate, process-local) Lifecycle singleton."""
    from agent_platform.sandbox.temporal import dispatch as sd
    from agent_platform.sandbox.temporal.constants import SANDBOX_TASK_QUEUE
    from agent_team_studio.agent_provisioning_team.temporal.constants import TASK_QUEUE

    exec_mock = AsyncMock(return_value=_handle().model_dump(mode="json"))
    with patch.object(sd, "execute_workflow_async", new=exec_mock):
        await sd.acquire_sandbox_via_temporal("blog.writer")

    call = exec_mock.await_args
    assert call.kwargs["task_queue"] == SANDBOX_TASK_QUEUE
    assert call.kwargs["task_queue"] != TASK_QUEUE


@pytest.mark.asyncio
async def test_teardown_via_temporal_dispatches_workflow() -> None:
    from agent_platform.sandbox.temporal import dispatch as sd

    exec_mock = AsyncMock(return_value=None)
    with patch.object(sd, "execute_workflow_async", new=exec_mock):
        await sd.teardown_sandbox_via_temporal("blog.writer")

    exec_mock.assert_awaited_once()
    call = exec_mock.await_args
    assert call.args[0] is sd.SandboxTeardownWorkflow.run
    assert call.args[1] == "blog.writer"
    assert call.kwargs["workflow_id"].startswith("agent-provisioning-sandbox-teardown-blog.writer-")


@pytest.mark.asyncio
async def test_teardown_via_temporal_dispatches_on_sandbox_task_queue() -> None:
    """P1 regression, teardown side of the same fix."""
    from agent_platform.sandbox.temporal import dispatch as sd
    from agent_platform.sandbox.temporal.constants import SANDBOX_TASK_QUEUE
    from agent_team_studio.agent_provisioning_team.temporal.constants import TASK_QUEUE

    exec_mock = AsyncMock(return_value=None)
    with patch.object(sd, "execute_workflow_async", new=exec_mock):
        await sd.teardown_sandbox_via_temporal("blog.writer")

    call = exec_mock.await_args
    assert call.kwargs["task_queue"] == SANDBOX_TASK_QUEUE
    assert call.kwargs["task_queue"] != TASK_QUEUE


@pytest.mark.asyncio
async def test_teardown_via_temporal_unwraps_docker_error() -> None:
    """Parity fix: teardown must translate WorkflowFailureError back to
    DockerError the same way acquire_sandbox_via_temporal does, instead of
    leaking an opaque WorkflowFailureError that breaks any caller written to
    catch DockerError around teardown (mirroring the acquire-side contract)."""
    from agent_platform.sandbox.provisioner import DockerError
    from agent_platform.sandbox.temporal import dispatch as sd

    exc = _fake_workflow_failure(_FakeApplicationError("DockerError", "stop_container failed"))
    with (
        patch.object(sd, "execute_workflow_async", new=AsyncMock(side_effect=exc)),
        pytest.raises(DockerError, match="stop_container failed"),
    ):
        await sd.teardown_sandbox_via_temporal("blog.writer")


@pytest.mark.asyncio
async def test_teardown_via_temporal_reraises_unrecognized_error() -> None:
    """Mirrors ``test_acquire_via_temporal_reraises_unrecognized_error``: an
    error with no recognizable ApplicationError type (e.g. a client-side
    timeout) propagates unchanged rather than being swallowed."""
    from agent_platform.sandbox.temporal import dispatch as sd

    with (
        patch.object(sd, "execute_workflow_async", new=AsyncMock(side_effect=RuntimeError("boom"))),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await sd.teardown_sandbox_via_temporal("blog.writer")


# ---------------------------------------------------------------------------
# dispatch — single-instance reaper start
# ---------------------------------------------------------------------------


def test_start_reaper_workflow_starts_with_fixed_id() -> None:
    from agent_platform.sandbox.temporal import dispatch as sd
    from agent_platform.sandbox.temporal.constants import (
        SANDBOX_REAPER_WORKFLOW_ID,
    )

    captured: dict = {}

    def fake_start(workflow_run, *args, workflow_id, task_queue, **kwargs):
        captured["workflow_id"] = workflow_id
        captured["args"] = args
        captured["task_queue"] = task_queue

    with patch.object(sd, "start_workflow_sync", side_effect=fake_start):
        sd.start_sandbox_reaper_workflow(30)

    assert captured["workflow_id"] == SANDBOX_REAPER_WORKFLOW_ID
    assert captured["args"] == (30,)
    # P1 regression: must run on SANDBOX_TASK_QUEUE, never the shared
    # TASK_QUEUE the standalone agent-provisioning-service team container
    # also polls (see SANDBOX_TASK_QUEUE's docstring in agent_platform.sandbox.temporal.constants).
    from agent_platform.sandbox.temporal.constants import SANDBOX_TASK_QUEUE
    from agent_team_studio.agent_provisioning_team.temporal.constants import TASK_QUEUE

    assert captured["task_queue"] == SANDBOX_TASK_QUEUE
    assert captured["task_queue"] != TASK_QUEUE


def test_start_reaper_workflow_swallows_already_started() -> None:
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from agent_platform.sandbox.temporal import dispatch as sd

    err = WorkflowAlreadyStartedError(workflow_id="wf-1", workflow_type="SandboxReaperWorkflow")
    with patch.object(sd, "start_workflow_sync", side_effect=err):
        # Must not raise — a running reaper IS the desired single-instance state.
        # Uses isinstance (not a name-substring match) against the real
        # temporalio type, matching the codebase's established convention.
        sd.start_sandbox_reaper_workflow()


def test_start_reaper_workflow_does_not_swallow_lookalike_exception() -> None:
    """A different exception whose name merely contains "AlreadyStarted" must
    NOT be swallowed — regression guard against the old fragile string match."""
    from agent_platform.sandbox.temporal import dispatch as sd

    class SomeOtherAlreadyStartedError(RuntimeError):
        pass

    with patch.object(
        sd, "start_workflow_sync", side_effect=SomeOtherAlreadyStartedError("unrelated")
    ):
        with pytest.raises(SomeOtherAlreadyStartedError):
            sd.start_sandbox_reaper_workflow()


def test_start_reaper_workflow_reraises_other_errors() -> None:
    from agent_platform.sandbox.temporal import dispatch as sd

    with patch.object(sd, "start_workflow_sync", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            sd.start_sandbox_reaper_workflow()
