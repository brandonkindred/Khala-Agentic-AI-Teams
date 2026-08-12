"""Unit tests for the agent-keyed sandbox Lifecycle.

Covers acquire / teardown / reap / status, the Docker-availability preflight,
the ``/health`` probe, idle-reaper and ``/metrics`` snapshots, team resolution,
and the module-level free-function wrappers. Docker CLI calls and the health
probe are patched so tests run without a real Docker daemon.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_platform.sandbox import (
    Lifecycle,
    SandboxStatus,
    UnknownAgentError,
)
from agent_platform.sandbox import provisioner as provisioner_mod
from agent_platform.sandbox.state import SandboxHandle, SandboxState, now


def _lifecycle(tmp_path: Path) -> Lifecycle:
    return Lifecycle(state_file=tmp_path / "state.json")


def _patched_registry(team: str = "blogging"):
    return patch(
        "agent_platform.sandbox.lifecycle._resolve_team",
        return_value=team,
    )


@contextmanager
def _patched_docker(*, container_id: str = "abc123", host_port: int = 55123, running: bool = False):
    """Apply the five default docker-mock patches as a single context manager.

    Yields a namespace whose attributes are the individual mocks so tests can
    assert on call counts without unpacking a tuple in every `with` block.
    """
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "agent_platform.sandbox.lifecycle._check_docker_available",
                return_value=None,
            )
        )
        yield SimpleNamespace(
            run=stack.enter_context(
                patch.object(
                    provisioner_mod, "run_container", new=AsyncMock(return_value=container_id)
                )
            ),
            port=stack.enter_context(
                patch.object(
                    provisioner_mod, "inspect_host_port", new=AsyncMock(return_value=host_port)
                )
            ),
            running=stack.enter_context(
                patch.object(provisioner_mod, "is_running", new=AsyncMock(return_value=running))
            ),
            stop=stack.enter_context(
                patch.object(provisioner_mod, "stop_container", new=AsyncMock())
            ),
            wait=stack.enter_context(patch.object(Lifecycle, "_wait_healthy", new=AsyncMock())),
        )


@pytest.mark.asyncio
async def test_status_cold_for_unseen_agent(tmp_path: Path) -> None:
    """status() must return a COLD handle without touching docker for
    agents that have never been acquired."""
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d:
        handle = await lc.status("blogging.planner")
    assert handle.status == SandboxStatus.COLD
    assert handle.team == "blogging"
    assert handle.url is None
    # status() on an unseen agent must not provision anything.
    d.run.assert_not_awaited()
    d.port.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_reconciles_warm_handle_with_docker(tmp_path: Path) -> None:
    """If we think a sandbox is WARM but docker says the container is gone,
    status() must flip the stored state back to COLD."""
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker():
        await lc.acquire("blogging.planner")
    assert lc._state["blogging.planner"].status == SandboxStatus.WARM

    with (
        _patched_registry(),
        patch.object(provisioner_mod, "is_running", new=AsyncMock(return_value=False)),
    ):
        handle = await lc.status("blogging.planner")
    assert handle.status == SandboxStatus.COLD
    assert lc._state["blogging.planner"].status == SandboxStatus.COLD


@pytest.mark.asyncio
async def test_acquire_cold_to_warm(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d:
        handle = await lc.acquire("blogging.planner")
    d.run.assert_awaited_once()
    assert handle.status == SandboxStatus.WARM
    assert handle.url == "http://127.0.0.1:55123"
    assert handle.team == "blogging"
    assert handle.container_id == "abc123"


@pytest.mark.asyncio
async def test_acquire_is_idempotent_when_already_warm(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker(running=True) as d:
        first = await lc.acquire("blogging.planner")
        second = await lc.acquire("blogging.planner")
    assert d.run.await_count == 1
    assert first.status == SandboxStatus.WARM
    assert second.status == SandboxStatus.WARM
    assert second.container_id == first.container_id


@pytest.mark.asyncio
async def test_acquire_reuses_warm_handle_when_docker_check_would_fail(tmp_path: Path) -> None:
    """A WARM sandbox should be reused even if the Docker daemon is temporarily
    unavailable (e.g. live-restore). The preflight must not gate the fast path."""
    from agent_platform.sandbox.lifecycle import DockerUnavailableError

    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker(running=True):
        await lc.acquire("blogging.planner")

    with (
        _patched_registry(),
        patch(
            "agent_platform.sandbox.lifecycle._check_docker_available",
            side_effect=DockerUnavailableError("daemon gone"),
        ),
        patch.object(provisioner_mod, "is_running", new=AsyncMock(return_value=True)),
    ):
        handle = await lc.acquire("blogging.planner")
    assert handle.status == SandboxStatus.WARM


@pytest.mark.asyncio
async def test_acquire_reports_error_on_health_timeout(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d:
        d.wait.side_effect = RuntimeError("boom")
        handle = await lc.acquire("blogging.planner")
    assert handle.status == SandboxStatus.ERROR
    assert handle.error == "boom"


@pytest.mark.asyncio
async def test_acquire_reprovisions_when_container_vanished(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d1:
        await lc.acquire("blogging.planner")
    assert d1.run.await_count == 1

    with _patched_registry(), _patched_docker(container_id="xyz789", host_port=55999) as d2:
        handle = await lc.acquire("blogging.planner")
    assert d2.run.await_count == 1
    assert handle.container_id == "xyz789"
    assert handle.host_port == 55999


@pytest.mark.asyncio
async def test_teardown_removes_state(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d:
        await lc.acquire("blogging.planner")
        await lc.teardown("blogging.planner")
    d.stop.assert_awaited()
    assert await lc.list_active() == []


@pytest.mark.asyncio
async def test_teardown_preserves_state_when_docker_errors(tmp_path: Path) -> None:
    # When `stop_container` raises a DockerError (daemon unreachable, etc.),
    # state must NOT be evicted — we need the record to retry against the
    # still-alive container on the next tick.
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d:
        await lc.acquire("blogging.planner")
        d.stop.side_effect = provisioner_mod.DockerError("docker daemon unreachable")
        with pytest.raises(provisioner_mod.DockerError):
            await lc.teardown("blogging.planner")
    assert "blogging.planner" in lc._state


@pytest.mark.asyncio
async def test_note_activity_updates_last_used(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker():
        await lc.acquire("blogging.planner")
    before = lc._state["blogging.planner"].last_used_at
    lc._state["blogging.planner"].last_used_at = before - timedelta(seconds=60)
    await lc.note_activity("blogging.planner")
    after = lc._state["blogging.planner"].last_used_at
    assert after > before - timedelta(seconds=60)


@pytest.mark.asyncio
async def test_reap_once_tears_down_idle(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d:
        await lc.acquire("blogging.planner")
        lc._state["blogging.planner"].last_used_at = lc._state[
            "blogging.planner"
        ].last_used_at - timedelta(minutes=30)
        reaped = await lc.reap_once(threshold=60)
    assert reaped == ["blogging.planner"]
    d.stop.assert_awaited()
    assert "blogging.planner" not in lc._state


@pytest.mark.asyncio
async def test_reap_preserves_fresh_sandbox(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker():
        await lc.acquire("blogging.planner")
        reaped = await lc.reap_once(threshold=3600)
    assert reaped == []
    assert "blogging.planner" in lc._state


@pytest.mark.asyncio
async def test_reap_once_continues_after_teardown_failure(tmp_path: Path) -> None:
    # If one sandbox's teardown hits a DockerError, the reaper should log and
    # continue so sibling sandboxes still get reclaimed this tick.
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d:
        await lc.acquire("blogging.planner")
        await lc.acquire("blogging.writer")
        for aid in ("blogging.planner", "blogging.writer"):
            lc._state[aid].last_used_at = lc._state[aid].last_used_at - timedelta(minutes=30)

        # Fail the first teardown, succeed the second.
        calls = {"n": 0}

        async def flaky_stop(_container_id: str) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise provisioner_mod.DockerError("daemon blip")

        d.stop.side_effect = flaky_stop
        reaped = await lc.reap_once(threshold=60)

    # Exactly one sandbox torn down; the other remains for the next tick.
    assert len(reaped) == 1
    assert len(lc._state) == 1


@pytest.mark.asyncio
async def test_unknown_agent_raises_without_docker_call(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with (
        patch(
            "agent_platform.sandbox.lifecycle._resolve_team",
            side_effect=UnknownAgentError("No agent manifest for 'ghost.agent'"),
        ),
        patch(
            "agent_platform.sandbox.lifecycle._check_docker_available",
            return_value=None,
        ),
        patch.object(provisioner_mod, "run_container", new=AsyncMock()) as run_mock,
    ):
        with pytest.raises(UnknownAgentError):
            await lc.acquire("ghost.agent")
    run_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_acquire_raises_docker_unavailable_when_no_docker(tmp_path: Path) -> None:
    from agent_platform.sandbox.lifecycle import DockerUnavailableError

    lc = _lifecycle(tmp_path)
    with (
        _patched_registry(),
        patch(
            "agent_platform.sandbox.lifecycle._check_docker_available",
            side_effect=DockerUnavailableError("Docker daemon not found"),
        ),
        patch.object(provisioner_mod, "run_container", new=AsyncMock()) as run_mock,
    ):
        with pytest.raises(DockerUnavailableError, match="Docker daemon not found"):
            await lc.acquire("blogging.planner")
    run_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_acquire_propagates_docker_error_from_zombie_cleanup(tmp_path: Path) -> None:
    """DockerError from stop_container should propagate — if the daemon is
    unreachable for cleanup, provisioning would also fail."""
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d:
        d.stop.side_effect = provisioner_mod.DockerError("daemon unreachable")
        with pytest.raises(provisioner_mod.DockerError, match="daemon unreachable"):
            await lc.acquire("blogging.planner")


@pytest.mark.asyncio
async def test_acquire_continues_when_non_docker_cleanup_fails(tmp_path: Path) -> None:
    """Non-DockerError from stop_container (e.g. unexpected OSError) should
    log a warning and continue to provisioning."""
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d:
        d.stop.side_effect = OSError("unexpected")
        handle = await lc.acquire("blogging.planner")
    assert handle.status == SandboxStatus.WARM
    d.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_acquire_returns_warm_handle_when_is_running_check_fails(tmp_path: Path) -> None:
    """If is_running raises (transient daemon issue), return the cached warm
    handle optimistically rather than tearing down a potentially healthy sandbox."""
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker():
        first = await lc.acquire("blogging.planner")

    with _patched_registry(), _patched_docker(container_id="new123", host_port=55999) as d2:
        d2.running.side_effect = OSError("docker socket gone")
        handle = await lc.acquire("blogging.planner")
    assert handle.status == SandboxStatus.WARM
    assert handle.container_id == first.container_id
    d2.run.assert_not_awaited()


def test_check_docker_available_honors_persisted_context(tmp_path: Path) -> None:
    """_check_docker_available should pass when ~/.docker/config.json has a
    non-default currentContext, even without DOCKER_HOST or the default socket."""
    from agent_platform.sandbox.lifecycle import _check_docker_available

    docker_dir = tmp_path / ".docker"
    docker_dir.mkdir()
    (docker_dir / "config.json").write_text(json.dumps({"currentContext": "remote-server"}))

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch.dict("os.environ", {"DOCKER_HOST": "", "DOCKER_CONTEXT": ""}, clear=False),
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("pathlib.Path.exists", return_value=False),
    ):
        _check_docker_available()


def test_check_docker_available_ignores_default_context(tmp_path: Path) -> None:
    """A persisted context of 'default' should NOT skip the socket check."""
    from agent_platform.sandbox.lifecycle import (
        DockerUnavailableError,
        _check_docker_available,
    )

    docker_dir = tmp_path / ".docker"
    docker_dir.mkdir()
    (docker_dir / "config.json").write_text(json.dumps({"currentContext": "default"}))

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch.dict("os.environ", {"DOCKER_HOST": "", "DOCKER_CONTEXT": ""}, clear=False),
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("pathlib.Path.exists", return_value=False),
    ):
        with pytest.raises(DockerUnavailableError):
            _check_docker_available()


def test_check_docker_available_honors_docker_config_env(tmp_path: Path) -> None:
    """DOCKER_CONFIG pointing to a custom dir with a non-default context
    should skip the socket check."""
    from agent_platform.sandbox.lifecycle import _check_docker_available

    custom_dir = tmp_path / "custom-docker"
    custom_dir.mkdir()
    (custom_dir / "config.json").write_text(json.dumps({"currentContext": "remote-server"}))

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch.dict(
            "os.environ",
            {"DOCKER_HOST": "", "DOCKER_CONTEXT": "", "DOCKER_CONFIG": str(custom_dir)},
            clear=False,
        ),
        patch("pathlib.Path.exists", return_value=False),
    ):
        _check_docker_available()


def test_check_docker_available_ignores_docker_context_default_env(tmp_path: Path) -> None:
    """DOCKER_CONTEXT=default points to the local socket, so the socket check
    must still fire — setting it should not bypass the fast-fail."""
    from agent_platform.sandbox.lifecycle import (
        DockerUnavailableError,
        _check_docker_available,
    )

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch.dict("os.environ", {"DOCKER_HOST": "", "DOCKER_CONTEXT": "default"}, clear=False),
        patch(
            "agent_platform.sandbox.lifecycle._has_non_default_docker_context",
            return_value=False,
        ),
        patch("pathlib.Path.exists", return_value=False),
    ):
        with pytest.raises(DockerUnavailableError):
            _check_docker_available()


def test_check_docker_available_passes_with_non_default_docker_context_env() -> None:
    """DOCKER_CONTEXT set to a non-default value should skip the socket check."""
    from agent_platform.sandbox.lifecycle import _check_docker_available

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch.dict(
            "os.environ", {"DOCKER_HOST": "", "DOCKER_CONTEXT": "remote-server"}, clear=False
        ),
    ):
        _check_docker_available()


def test_state_persists_across_lifecycle_instances(tmp_path: Path) -> None:
    lc1 = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker():
        asyncio.run(lc1.acquire("blogging.planner"))
    lc2 = _lifecycle(tmp_path)
    assert set(lc2._state) == {"blogging.planner"}
    assert lc2._state["blogging.planner"].status == SandboxStatus.WARM
    assert lc2._state["blogging.planner"].container_id == "abc123"


@pytest.mark.asyncio
async def test_run_idle_reaper_is_cancellable(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with (
        patch(
            "agent_platform.sandbox.lifecycle.asyncio.sleep",
            new=AsyncMock(),
        ),
        patch.object(Lifecycle, "reap_once", new=AsyncMock(return_value=[])),
    ):
        task = asyncio.create_task(lc.run_idle_reaper(interval_s=0))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_handle_from_state_projects_url_and_idle() -> None:
    t = now()
    warm = SandboxState(
        agent_id="blogging.planner",
        team="blogging",
        container_name="khala-sbx-blogging.planner",
        container_id="abc123",
        host_port=55123,
        status=SandboxStatus.WARM,
        created_at=t,
        last_used_at=t,
    )
    handle = SandboxHandle.from_state(warm)
    assert handle.url == "http://127.0.0.1:55123"
    assert handle.container_id == "abc123"
    assert handle.host_port == 55123
    assert handle.team == "blogging"

    cold = SandboxState(
        agent_id="x.y",
        team="blogging",
        container_name="khala-sbx-x.y",
        status=SandboxStatus.COLD,
        created_at=t,
        last_used_at=t,
    )
    assert SandboxHandle.from_state(cold).url is None


# ---------------------------------------------------------------------------
# Module-level free-function wrappers (Phase 3, issue #265).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /metrics snapshot — issue #302
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_empty_state(tmp_path: Path) -> None:
    """A fresh Lifecycle has no residents, no reaper ticks, no samples."""
    lc = _lifecycle(tmp_path)
    snap = await lc.metrics()
    assert snap.resident == 0
    assert snap.by_team == {}
    assert snap.by_status == {}
    assert snap.ages_seconds.min == 0 == snap.ages_seconds.max
    assert snap.ages_seconds.p50 == 0 == snap.ages_seconds.p95
    assert snap.boot_ms.samples == 0
    assert snap.boot_ms.p50 == 0 == snap.boot_ms.p95
    assert snap.reaper.last_tick_at is None
    assert snap.reaper.interval_s is None
    assert snap.reaper.torn_down_total == 0
    assert snap.reaper.torn_down_last_tick == 0
    # Threshold comes from the env-backed helper so it's always populated.
    assert snap.reaper.threshold_s > 0


def test_percentile_uses_nearest_rank() -> None:
    """Regression for PR #304 Codex feedback: the helper must use ceil, not
    round, so p95 on 11 samples lands on index 10 (the textbook nearest-rank
    rank) rather than underestimating at index 9."""
    from agent_platform.sandbox.lifecycle import _percentile

    samples = list(range(1, 12))  # 1..11, pre-sorted
    # p95 of 11 samples: ceil(0.95 * 11) = 11 → index 10 → value 11.
    assert _percentile(samples, 95) == 11
    # p50 of 11 samples: ceil(0.5 * 11) = 6 → index 5 → value 6.
    assert _percentile(samples, 50) == 6
    # Empty input → 0 (safe default for the all-empty /metrics snapshot).
    assert _percentile([], 95) == 0
    # Single sample → that sample, regardless of percentile.
    assert _percentile([42], 50) == 42
    assert _percentile([42], 95) == 42


@pytest.mark.asyncio
async def test_metrics_after_acquire_records_boot_sample(tmp_path: Path) -> None:
    """acquire() pushes a boot_ms sample and lights up the by_team/by_status
    counters — confirming the Phase 6 cold-start hook feeds /metrics."""
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker():
        await lc.acquire("blogging.planner")
    snap = await lc.metrics()
    assert snap.resident == 1
    assert snap.by_team == {"blogging": 1}
    assert snap.by_status == {"warm": 1}
    assert snap.boot_ms.samples == 1
    # Percentiles collapse to the single sample on n=1.
    assert snap.boot_ms.p50 == snap.boot_ms.p95
    assert snap.ages_seconds.min >= 0
    assert snap.ages_seconds.max >= snap.ages_seconds.min


@pytest.mark.asyncio
async def test_metrics_reaper_counters_advance_per_tick(tmp_path: Path) -> None:
    """Every reap_once() call must stamp last_tick_at and update the torn-down
    counters — even when it finds nothing to reap — so operators can tell the
    reaper is alive."""
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker():
        await lc.acquire("blogging.planner")

    # First tick: fresh sandbox, nothing reaped, but last_tick_at stamps.
    await lc.reap_once(threshold=3600)
    snap = await lc.metrics()
    assert snap.reaper.last_tick_at is not None
    assert snap.reaper.torn_down_total == 0
    assert snap.reaper.torn_down_last_tick == 0

    # Back-date and reap — mirrors the pattern used in
    # test_reap_once_tears_down_idle.
    lc._state["blogging.planner"].last_used_at = lc._state[
        "blogging.planner"
    ].last_used_at - timedelta(minutes=30)
    with _patched_registry(), _patched_docker():
        reaped = await lc.reap_once(threshold=60)
    assert reaped == ["blogging.planner"]
    snap = await lc.metrics()
    assert snap.resident == 0
    assert snap.reaper.torn_down_total == 1
    assert snap.reaper.torn_down_last_tick == 1


@pytest.mark.asyncio
async def test_metrics_boot_ms_window_is_bounded(tmp_path: Path) -> None:
    """The boot_ms buffer must stay bounded so long-running APIs don't leak —
    guards against someone swapping the deque for an unbounded list."""
    lc = _lifecycle(tmp_path)
    for i in range(600):
        lc._boot_ms_samples.append(i)
    snap = await lc.metrics()
    # Deque with maxlen=500 drops the oldest 100 samples.
    assert snap.boot_ms.samples == 500
    assert snap.boot_ms.p50 > 0
    assert snap.boot_ms.p95 >= snap.boot_ms.p50


@pytest.mark.asyncio
async def test_metrics_by_status_groups_across_agents(tmp_path: Path) -> None:
    """Two acquires on different teams land in distinct by_team buckets;
    by_status uses the lowercase enum value."""
    lc = _lifecycle(tmp_path)
    with _patched_registry(team="blogging"), _patched_docker():
        await lc.acquire("blogging.planner")
    with (
        _patched_registry(team="branding"),
        _patched_docker(container_id="brand1", host_port=55124),
    ):
        await lc.acquire("branding.logo")
    snap = await lc.metrics()
    assert snap.resident == 2
    assert snap.by_team == {"blogging": 1, "branding": 1}
    assert snap.by_status == {"warm": 2}
    assert snap.boot_ms.samples == 2


@pytest.mark.asyncio
async def test_run_idle_reaper_records_interval(tmp_path: Path) -> None:
    """run_idle_reaper must publish its configured interval so /metrics can
    surface it — operators need both the threshold and the tick cadence."""
    lc = _lifecycle(tmp_path)

    async def _cancel(self_: Lifecycle, *, threshold: int) -> list[str]:
        raise asyncio.CancelledError

    with (
        # Mock the in-loop sleep so the reaper doesn't stall 42s before reaping.
        # Patching asyncio.sleep globally would also mock any `await sleep(0)`
        # in the test itself — hence we drive cancellation from reap_once below.
        patch(
            "agent_platform.sandbox.lifecycle.asyncio.sleep",
            new=AsyncMock(),
        ),
        patch.object(Lifecycle, "reap_once", new=_cancel),
    ):
        with pytest.raises(asyncio.CancelledError):
            await lc.run_idle_reaper(interval_s=42)

    assert lc._reaper_interval_s == 42
    snap = await lc.metrics()
    assert snap.reaper.interval_s == 42


@pytest.mark.asyncio
async def test_module_helpers_delegate_to_singleton(tmp_path: Path, monkeypatch) -> None:
    """`sandbox.acquire/teardown/…` must operate on the same Lifecycle instance
    so that status swings are observable across calls — the unified API routes
    rely on this to reconcile invoke + list + teardown."""
    from agent_platform import sandbox as sb
    from agent_platform.sandbox import lifecycle as lifecycle_mod

    lc = _lifecycle(tmp_path)
    lifecycle_mod.get_lifecycle.cache_clear()
    monkeypatch.setattr(lifecycle_mod, "get_lifecycle", lambda: lc)

    with _patched_registry(), _patched_docker() as d:
        handle = await sb.acquire("blogging.planner")
    assert handle.status == SandboxStatus.WARM
    assert (await sb.list_active())[0].agent_id == "blogging.planner"

    # note_activity bumps last_used_at on the same state row.
    await sb.note_activity("blogging.planner")

    # teardown via the module helper clears state.
    with _patched_registry(), _patched_docker():
        await sb.teardown("blogging.planner")
    assert await sb.list_active() == []
    d.run.assert_awaited_once()


# -------------------------------------------------------------------------
# Additional Lifecycle coverage: health probe, idle reaper, reap_once,
# team resolution, and module-level sandbox helpers.
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_healthy_exceeds_deadline(tmp_path: Path, monkeypatch) -> None:
    from agent_platform.sandbox import lifecycle as lc_mod

    lc = lc_mod.Lifecycle(state_file=tmp_path / "s.json")

    # Force the deadline immediately and httpx to always raise.
    monkeypatch.setattr(lc_mod, "boot_timeout_seconds", lambda: 0)

    async def fake_get(self, *args, **kwargs):
        import httpx

        raise httpx.ConnectError("nope")

    with patch("httpx.AsyncClient.get", new=fake_get):
        with pytest.raises(RuntimeError, match="did not report healthy"):
            await lc._wait_healthy(host_port=12345)


@pytest.mark.asyncio
async def test_wait_healthy_success(tmp_path: Path, monkeypatch) -> None:
    from agent_platform.sandbox import lifecycle as lc_mod

    lc = lc_mod.Lifecycle(state_file=tmp_path / "s.json")

    async def fake_get(self, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        return resp

    with patch("httpx.AsyncClient.get", new=fake_get):
        await lc._wait_healthy(host_port=12345)


@pytest.mark.asyncio
async def test_run_idle_reaper_swallows_non_cancel_exception(tmp_path: Path) -> None:
    from agent_platform.sandbox import lifecycle as lc_mod

    lc = lc_mod.Lifecycle(state_file=tmp_path / "s.json")

    calls = {"n": 0}

    async def flaky_reap(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient")
        raise asyncio.CancelledError()

    with (
        patch.object(lc_mod.asyncio, "sleep", new=AsyncMock()),
        patch.object(lc_mod.Lifecycle, "reap_once", new=flaky_reap),
    ):
        with pytest.raises(asyncio.CancelledError):
            await lc.run_idle_reaper(interval_s=0)


@pytest.mark.asyncio
async def test_reap_once_skips_non_warm(tmp_path: Path) -> None:
    from agent_platform.sandbox import lifecycle as lc_mod
    from agent_platform.sandbox.state import (
        SandboxState,
        SandboxStatus,
        now,
    )

    lc = lc_mod.Lifecycle(state_file=tmp_path / "s.json")
    t = now()
    lc._state["a1"] = SandboxState(
        agent_id="a1",
        team="t",
        container_name="c1",
        status=SandboxStatus.ERROR,  # not warm
        created_at=t,
        last_used_at=t - timedelta(hours=1),
    )

    reaped = await lc.reap_once(threshold=60)
    assert reaped == []
    # State row still present (not touched).
    assert "a1" in lc._state


@pytest.mark.asyncio
async def test_resolve_team_success() -> None:
    from agent_platform.sandbox.lifecycle import _resolve_team

    fake_manifest = MagicMock()
    fake_manifest.team = "myteam"
    fake_registry = MagicMock()
    fake_registry.get.return_value = fake_manifest

    with patch(
        "agent_registry.get_registry",
        return_value=fake_registry,
    ):
        assert await _resolve_team("agent.x") == "myteam"


@pytest.mark.asyncio
async def test_resolve_team_unknown() -> None:
    from agent_platform.sandbox.lifecycle import (
        UnknownAgentError,
        _resolve_team,
    )

    fake_registry = MagicMock()
    fake_registry.get.return_value = None

    with patch("agent_registry.get_registry", return_value=fake_registry):
        with pytest.raises(UnknownAgentError):
            await _resolve_team("ghost")


@pytest.mark.asyncio
async def test_resolve_team_calls_get_registry_off_the_event_loop_thread() -> None:
    """Regression: ``get_registry()`` itself — not just the ``.get(agent_id)``
    lookup on its result — must run inside the ``asyncio.to_thread`` worker.
    ``get_registry`` is only cheap on a cache hit; a cold-cache first call
    performs ``AgentRegistry.load()``'s full manifest directory scan. Evaluating
    ``get_registry()`` as an eagerly-computed argument to ``asyncio.to_thread``
    (the pre-fix shape) would run that scan directly on the event loop thread."""
    import threading

    fake_manifest = MagicMock()
    fake_manifest.team = "myteam"
    fake_registry = MagicMock()
    fake_registry.get.return_value = fake_manifest
    caller_thread = threading.current_thread()
    captured: dict = {}

    def _recording_get_registry():
        captured["thread"] = threading.current_thread()
        return fake_registry

    from agent_platform.sandbox.lifecycle import _resolve_team

    with patch("agent_registry.get_registry", side_effect=_recording_get_registry):
        assert await _resolve_team("agent.x") == "myteam"

    assert captured["thread"] is not caller_thread


@pytest.mark.asyncio
async def test_module_helper_status(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import AsyncMock as _AsyncMock

    from agent_platform import sandbox as sb
    from agent_platform.sandbox import lifecycle as lc_mod

    lc = lc_mod.Lifecycle(state_file=tmp_path / "s.json")
    lc_mod.get_lifecycle.cache_clear()
    monkeypatch.setattr(lc_mod, "get_lifecycle", lambda: lc)

    with patch.object(lc_mod, "_resolve_team", _AsyncMock(return_value="t")):
        handle = await sb.status("some.agent")
    assert handle.agent_id == "some.agent"


@pytest.mark.asyncio
async def test_module_helper_metrics(tmp_path: Path, monkeypatch) -> None:
    from agent_platform import sandbox as sb
    from agent_platform.sandbox import lifecycle as lc_mod

    lc = lc_mod.Lifecycle(state_file=tmp_path / "s.json")
    lc_mod.get_lifecycle.cache_clear()
    monkeypatch.setattr(lc_mod, "get_lifecycle", lambda: lc)
    snap = await sb.metrics()
    assert snap.resident == 0


@pytest.mark.asyncio
async def test_module_helper_run_idle_reaper(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import AsyncMock as _AsyncMock

    from agent_platform import sandbox as sb
    from agent_platform.sandbox import lifecycle as lc_mod

    lc = lc_mod.Lifecycle(state_file=tmp_path / "s.json")
    lc_mod.get_lifecycle.cache_clear()
    monkeypatch.setattr(lc_mod, "get_lifecycle", lambda: lc)

    with (
        patch.object(lc_mod.asyncio, "sleep", new=_AsyncMock()),
        patch.object(lc_mod.Lifecycle, "reap_once", new=_AsyncMock(return_value=[])),
    ):
        task = asyncio.create_task(sb.run_idle_reaper(interval_s=0))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_lifecycle_note_activity_missing_no_op(tmp_path: Path) -> None:
    from agent_platform.sandbox.lifecycle import Lifecycle

    lc = Lifecycle(state_file=tmp_path / "s.json")
    # No state for "ghost" — call must be a no-op rather than raise.
    await lc.note_activity("ghost")


@pytest.mark.asyncio
async def test_lifecycle_teardown_missing_no_op(tmp_path: Path) -> None:
    from agent_platform.sandbox.lifecycle import Lifecycle

    lc = Lifecycle(state_file=tmp_path / "s.json")
    await lc.teardown("ghost")


@pytest.mark.asyncio
async def test_lifecycle_persist_swallows_oserror(tmp_path: Path, monkeypatch) -> None:
    """If `state.save` raises, the public method completes without re-raising."""
    from agent_platform.sandbox import lifecycle as lc_mod

    lc = lc_mod.Lifecycle(state_file=tmp_path / "s.json")

    def fail_save(path, state):
        raise OSError("disk full")

    with patch.object(lc_mod.state_mod, "save", side_effect=fail_save):
        await lc._persist()


@pytest.mark.asyncio
async def test_lifecycle_persist_swallows_non_oserror(tmp_path: Path, monkeypatch) -> None:
    """The docstring promises `_persist()` never raises; a non-OSError failure
    inside `state.save` (e.g. a serialization error) must be swallowed the
    same way an OSError is, not propagate as an unhandled task exception."""
    from agent_platform.sandbox import lifecycle as lc_mod

    lc = lc_mod.Lifecycle(state_file=tmp_path / "s.json")

    def fail_save(path, state):
        raise TypeError("not serializable")

    with patch.object(lc_mod.state_mod, "save", side_effect=fail_save):
        await lc._persist()
