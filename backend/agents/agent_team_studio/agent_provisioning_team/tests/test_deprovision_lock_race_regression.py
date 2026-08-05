"""Regression tests: a timed-out ``deprovision_activity`` must never race a
second workflow's ``agent_id`` lock acquire.

``AgentDeprovisioningWorkflow`` (``temporal/workflows.py``) only releases the
``agent_id`` lock after ``_await_deprovision`` confirms ``deprovision_activity``
has actually stopped (cancellation acknowledged, or the ack-grace window is
exhausted) — never merely because ``DEPROVISION_SOFT_TIMEOUT`` elapsed. The
existing suite (``test_workflows_unit.py``) proves this at the workflow
control-flow level with a fully mocked Temporal boundary and no real lock
contention. This module adds the missing piece: a *real* ``AgentLockStore``
(real file I/O, real contention) plus a genuinely concurrent second acquirer,
racing against a still-"mutating" fake teardown loop — the combination that
would actually catch a regression to the pre-fix "release on bare
await-timeout" behavior.

``workflow.execute_activity``/``start_activity``/``sleep``/``info``/``patched``
are stubbed exactly like ``test_workflows_unit.py`` (no live Temporal server
anywhere in this team's tests) — but the lock activities' *bodies* call the
real ``acquire_agent_lock_activity``/``release_agent_lock_activity`` functions
against a ``tmp_path``-backed ``AgentLockStore`` (via the real ``AGENT_CACHE``
env var), so contention between the two simulated workflow runs is genuine,
not scripted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _patched_true(monkeypatch):
    """Default to the post-lock-deploy replay branch, mirroring
    ``test_workflows_unit.py``'s identical fixture."""
    from agent_team_studio.agent_provisioning_team.temporal import workflows as wf

    monkeypatch.setattr(wf.workflow, "patched", lambda *a, **k: True)


def _sleep_sequence(*behaviors):
    """``workflow.sleep`` stand-in returning a distinct behavior per call, by
    call order (the last behavior repeats for any further calls).

    Copied from ``test_workflows_unit.py`` (this team's tests intentionally
    keep small harness helpers self-contained per file rather than
    cross-importing test internals). A behavior of ``None`` resolves
    immediately; ``"hang"`` never resolves on its own.
    """
    calls = {"n": 0}

    async def _sleep(_delta):
        idx = min(calls["n"], len(behaviors) - 1)
        calls["n"] += 1
        if behaviors[idx] == "hang":
            await asyncio.Future()
        return None

    return _sleep


def _make_fake_execute_activity(activities_module, event_log):
    """``workflow.execute_activity`` stand-in that dispatches the two lock
    activities to their real implementations (genuine ``AgentLockStore``
    contention) and no-ops anything else.

    ``event_log`` is a plain ``list[str]``; single-threaded ``asyncio``
    scheduling makes each entry's list position a deterministic
    happens-before ordering, so callers compare positions rather than
    wall-clock timestamps.
    """

    async def _fake_execute_activity(activity_fn, *args, **kwargs):
        name = getattr(activity_fn, "__name__", str(activity_fn))
        call_args = list(kwargs.get("args") or [])
        event_log.append(f"call:{name}")
        if name == "acquire_agent_lock_activity":
            return activities_module.acquire_agent_lock_activity(*call_args)
        if name == "release_agent_lock_activity":
            activities_module.release_agent_lock_activity(*call_args)
            event_log.append("release_called")
            return None
        return None

    return _fake_execute_activity


async def _second_acquirer_acquires_and_mutates(activities_module, agent_id, owner, event_log):
    """Model the "second workflow's acquire-and-mutate sequence" from the
    issue's acceptance criteria as a standalone coroutine racing the real
    ``acquire_agent_lock_activity`` against the first run's real lock record,
    rather than a full second workflow instance.

    Lock acquisition is the only contention-relevant step either
    ``AgentProvisioningWorkflow``/``AgentDeprovisioningWorkflow`` performs;
    reconstructing the full phase graph would add unrelated fragility without
    strengthening this regression's coverage.
    """
    while True:
        try:
            activities_module.acquire_agent_lock_activity(owner, agent_id)
            break
        except RuntimeError:
            event_log.append("run2_retry")
            await asyncio.sleep(0)
    event_log.append("run2_acquired")
    event_log.append("run2_mutate")


@pytest.mark.asyncio
async def test_second_workflow_acquire_cannot_overlap_first_runs_mutations(
    tmp_path, monkeypatch
) -> None:
    """A deprovision_activity that blocks past DEPROVISION_SOFT_TIMEOUT must
    stop making mutating teardown calls before a second workflow's acquire
    for the same agent_id can succeed — the core race issue #1641 closes.

    The fake activity's teardown loop is deliberately unbounded: it keeps
    "mutating" every tick until it actually observes cancellation. Against a
    pre-fix implementation that released the lock on the bare soft-timeout
    (without first cancelling the handle and awaiting its acknowledgement),
    the second run's already-racing acquire would win while this loop is
    still mutating on subsequent ticks — which is exactly what the
    "no mutate entry after cancel_observed" assertion below would catch.
    """
    from agent_team_studio.agent_provisioning_team.temporal import activities as _activities
    from agent_team_studio.agent_provisioning_team.temporal import workflows as wf

    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))

    agent_id = "agent-race-1"
    event_log: list[str] = []

    async def _fake_deprovision_loop():
        try:
            i = 0
            while True:
                event_log.append(f"mutate:{i}")
                i += 1
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            from agent_team_studio.agent_provisioning_team.models import DeprovisionCancelledError

            event_log.append("cancel_observed")
            raise DeprovisionCancelledError(agent_id, {"tools": {}}) from None

    def fake_start_activity(activity_fn, *args, **kwargs):
        return asyncio.ensure_future(_fake_deprovision_loop())

    fake_execute_activity = _make_fake_execute_activity(_activities, event_log)
    fake_info = SimpleNamespace(workflow_id="run1-owner")

    async def run1():
        with pytest.raises(TimeoutError, match="DEPROVISION_SOFT_TIMEOUT"):
            await wf.AgentDeprovisioningWorkflow().run(agent_id, False)

    async def run2():
        await _second_acquirer_acquires_and_mutates(_activities, agent_id, "run2-owner", event_log)

    with (
        patch("temporalio.activity.heartbeat"),
        patch.object(wf.workflow, "execute_activity", new=fake_execute_activity),
        patch.object(wf.workflow, "start_activity", new=fake_start_activity),
        # Soft timeout resolves immediately; ack-grace hangs so the second
        # race resolves via the handle itself raising DeprovisionCancelledError
        # (matching test_workflows_unit.py's identical cancelled-error test).
        patch.object(wf.workflow, "sleep", new=_sleep_sequence(None, "hang")),
        patch.object(wf.workflow, "info", return_value=fake_info),
        patch.object(wf.workflow, "logger", new=MagicMock()),
    ):
        await asyncio.gather(run1(), run2())

    # Non-vacuous contention: the second acquirer really had to retry against
    # the first run's still-held lock, not merely happen to run once, after
    # the fact, and trivially "succeed".
    assert "run2_retry" in event_log

    assert "cancel_observed" in event_log
    assert "run2_acquired" in event_log
    assert event_log.index("cancel_observed") < event_log.index("run2_acquired")

    # No mutating call happened after the first run's teardown loop actually
    # observed cancellation — transitively, none happened after the second
    # run's acquire succeeded either, which is the literal acceptance bar.
    mutate_indices = [i for i, entry in enumerate(event_log) if entry.startswith("mutate:")]
    assert mutate_indices, "the fake teardown loop never ran at all"
    assert max(mutate_indices) < event_log.index("cancel_observed")

    assert [e.removeprefix("call:") for e in event_log if e.startswith("call:")] == [
        "acquire_agent_lock_activity",
        "release_agent_lock_activity",
    ]


@pytest.mark.asyncio
async def test_second_workflow_acquire_only_succeeds_after_release_when_cancel_ack_never_arrives(
    tmp_path, monkeypatch
) -> None:
    """Leak-prevention edge case (issue #1787): even when
    deprovision_activity's worker never acknowledges its requested
    cancellation (crash, thread-pool starvation, ...) and the workflow gives
    up waiting past DEPROVISION_CANCEL_GRACE, the lock is not leaked — and,
    crucially, the second workflow's acquire still cannot succeed until
    release_agent_lock_activity has genuinely run against the real lock
    record.

    Unlike the test above, this scenario does *not* assert zero further
    mutation after release: an unresponsive worker's continued activity past
    the give-up point is an accepted, documented limit of this workflow-side
    gate (see ``_await_deprovision``'s own docstring) — the actual backstop
    for a truly dead worker is Temporal's own ``DEPROVISION_HEARTBEAT_TIMEOUT``
    liveness detection, not this lock-release ordering. What this test proves
    instead is the weaker, correctly-scoped invariant: the lock is never
    handed to a second acquirer before release genuinely executes, even on
    this worst-case give-up path.
    """
    from agent_team_studio.agent_provisioning_team.temporal import activities as _activities
    from agent_team_studio.agent_provisioning_team.temporal import workflows as wf

    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))

    agent_id = "agent-race-2"
    event_log: list[str] = []

    def fake_start_activity(activity_fn, *args, **kwargs):
        async def _never_acknowledges_cancellation():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                # Swallow the first cancellation — simulates an ack that
                # never arrives within this test's DEPROVISION_CANCEL_GRACE
                # window. Still cancellable a second time (the un-guarded
                # await below) so pytest-asyncio's own teardown can still
                # reap this task, mirroring test_workflows_unit.py's
                # identical "gives up" test.
                pass
            await asyncio.Future()

        return asyncio.ensure_future(_never_acknowledges_cancellation())

    fake_execute_activity = _make_fake_execute_activity(_activities, event_log)
    fake_info = SimpleNamespace(workflow_id="run1-owner")

    async def run1():
        with pytest.raises(TimeoutError, match="DEPROVISION_SOFT_TIMEOUT") as exc_info:
            await wf.AgentDeprovisioningWorkflow().run(agent_id, False)
        assert "not acknowledged" in str(exc_info.value)

    async def run2():
        await _second_acquirer_acquires_and_mutates(_activities, agent_id, "run2-owner", event_log)

    with (
        patch("temporalio.activity.heartbeat"),
        patch.object(wf.workflow, "execute_activity", new=fake_execute_activity),
        patch.object(wf.workflow, "start_activity", new=fake_start_activity),
        # Both timers resolve promptly, matching test_workflows_unit.py's
        # identical ack-never-arrives test: the soft timeout fires, then the
        # ack-grace window elapses too since the fake activity's task never
        # truly completes.
        patch.object(wf.workflow, "sleep", new=_sleep_sequence(None, None)),
        patch.object(wf.workflow, "info", return_value=fake_info),
        patch.object(wf.workflow, "logger", new=MagicMock()),
    ):
        await asyncio.gather(run1(), run2())

    assert "run2_retry" in event_log, "the second acquirer never contended for the lock at all"
    assert "release_called" in event_log
    assert "run2_acquired" in event_log
    assert event_log.index("release_called") < event_log.index("run2_acquired")

    assert [e.removeprefix("call:") for e in event_log if e.startswith("call:")] == [
        "acquire_agent_lock_activity",
        "release_agent_lock_activity",
    ]
