"""Unit tests for the agent-keyed sandbox Lifecycle (issue #264, Phase 2).

Docker CLI calls and the ``/health`` probe are patched so tests run without
a real Docker daemon. Mirrors the pattern in
``backend/agents/agent_sandbox/tests/test_manager.py``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_provisioning_team.sandbox import (
    Lifecycle,
    SandboxStatus,
    UnknownAgentError,
)
from agent_provisioning_team.sandbox import provisioner as provisioner_mod
from agent_provisioning_team.sandbox.provisioner import _build_run_argv, container_name_for


def _lifecycle(tmp_path: Path) -> Lifecycle:
    return Lifecycle(state_file=tmp_path / "state.json")


def _patched_registry(team: str = "blogging"):
    """Return a patch context that stubs ``_resolve_team`` to avoid loading real YAML."""
    return patch(
        "agent_provisioning_team.sandbox.lifecycle._resolve_team",
        return_value=team,
    )


def _patched_docker(
    *,
    container_id: str = "abc123",
    host_port: int = 55123,
    running: bool = False,
):
    """Bundle the default docker-mock patches used across tests."""
    return (
        patch.object(provisioner_mod, "run_container", new=AsyncMock(return_value=container_id)),
        patch.object(provisioner_mod, "inspect_host_port", new=AsyncMock(return_value=host_port)),
        patch.object(provisioner_mod, "is_running", new=AsyncMock(return_value=running)),
        patch.object(provisioner_mod, "stop_container", new=AsyncMock()),
        patch.object(Lifecycle, "_wait_healthy", new=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_acquire_cold_to_warm(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    run, port, running, stop, wait = _patched_docker()
    with _patched_registry(), run as run_mock, port, running, stop, wait:
        handle = await lc.acquire("blogging.planner")
    run_mock.assert_awaited_once()
    assert handle.status == SandboxStatus.WARM
    assert handle.url == "http://127.0.0.1:55123"
    assert handle.team == "blogging"
    assert handle.container_id == "abc123"


@pytest.mark.asyncio
async def test_acquire_is_idempotent_when_already_warm(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    run, port, _, stop, wait = _patched_docker()
    # Second acquire should see container still running and skip `run_container`.
    with (
        _patched_registry(),
        run as run_mock,
        port,
        patch.object(provisioner_mod, "is_running", new=AsyncMock(return_value=True)),
        stop,
        wait,
    ):
        first = await lc.acquire("blogging.planner")
        second = await lc.acquire("blogging.planner")
    assert run_mock.await_count == 1
    assert first.status == SandboxStatus.WARM
    assert second.status == SandboxStatus.WARM
    assert second.container_id == first.container_id


@pytest.mark.asyncio
async def test_acquire_reports_error_on_health_timeout(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    run, port, running, stop, _ = _patched_docker()
    with (
        _patched_registry(),
        run,
        port,
        running,
        stop,
        patch.object(Lifecycle, "_wait_healthy", new=AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        handle = await lc.acquire("blogging.planner")
    assert handle.status == SandboxStatus.ERROR
    assert handle.error == "boom"


@pytest.mark.asyncio
async def test_acquire_reprovisions_when_container_vanished(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    run, port, running, stop, wait = _patched_docker()
    # First warm; then on the second acquire, pretend the container is gone.
    with _patched_registry(), run as run_mock, port, running, stop, wait:
        await lc.acquire("blogging.planner")
    with (
        _patched_registry(),
        patch.object(
            provisioner_mod, "run_container", new=AsyncMock(return_value="xyz789")
        ) as re_run,
        patch.object(provisioner_mod, "inspect_host_port", new=AsyncMock(return_value=55999)),
        patch.object(provisioner_mod, "is_running", new=AsyncMock(return_value=False)),
        patch.object(provisioner_mod, "stop_container", new=AsyncMock()),
        patch.object(Lifecycle, "_wait_healthy", new=AsyncMock()),
    ):
        handle = await lc.acquire("blogging.planner")
    assert run_mock.await_count == 1
    assert re_run.await_count == 1
    assert handle.container_id == "xyz789"
    assert handle.host_port == 55999


@pytest.mark.asyncio
async def test_teardown_removes_state(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    run, port, running, stop, wait = _patched_docker()
    with _patched_registry(), run, port, running, stop as stop_mock, wait:
        await lc.acquire("blogging.planner")
        await lc.teardown("blogging.planner")
    stop_mock.assert_awaited()
    assert await lc.list_active() == []


@pytest.mark.asyncio
async def test_note_activity_updates_last_used(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    run, port, running, stop, wait = _patched_docker()
    with _patched_registry(), run, port, running, stop, wait:
        await lc.acquire("blogging.planner")
    before = lc._state["blogging.planner"].last_used_at
    # Rewind so any clock granularity doesn't make the comparison a no-op.
    lc._state["blogging.planner"].last_used_at = before - timedelta(seconds=60)
    await lc.note_activity("blogging.planner")
    after = lc._state["blogging.planner"].last_used_at
    assert after > before - timedelta(seconds=60)


@pytest.mark.asyncio
async def test_reap_once_tears_down_idle(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    run, port, running, stop, wait = _patched_docker()
    with _patched_registry(), run, port, running, stop as stop_mock, wait:
        await lc.acquire("blogging.planner")
        # Fast-forward the last-used timestamp so the reaper sees the sandbox as idle.
        lc._state["blogging.planner"].last_used_at = lc._state[
            "blogging.planner"
        ].last_used_at - timedelta(minutes=30)
        reaped = await lc.reap_once(threshold=60)
    assert reaped == ["blogging.planner"]
    stop_mock.assert_awaited()
    assert "blogging.planner" not in lc._state


@pytest.mark.asyncio
async def test_reap_preserves_fresh_sandbox(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    run, port, running, stop, wait = _patched_docker()
    with _patched_registry(), run, port, running, stop, wait:
        await lc.acquire("blogging.planner")
        reaped = await lc.reap_once(threshold=3600)
    assert reaped == []
    assert "blogging.planner" in lc._state


@pytest.mark.asyncio
async def test_unknown_agent_raises_without_docker_call(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with (
        patch(
            "agent_provisioning_team.sandbox.lifecycle._resolve_team",
            side_effect=UnknownAgentError("No agent manifest for 'ghost.agent'"),
        ),
        patch.object(provisioner_mod, "run_container", new=AsyncMock()) as run_mock,
    ):
        with pytest.raises(UnknownAgentError):
            await lc.acquire("ghost.agent")
    run_mock.assert_not_awaited()


def test_state_persists_across_lifecycle_instances(tmp_path: Path) -> None:
    lc1 = _lifecycle(tmp_path)
    run, port, running, stop, wait = _patched_docker()
    with _patched_registry(), run, port, running, stop, wait:
        asyncio.run(lc1.acquire("blogging.planner"))
    lc2 = _lifecycle(tmp_path)
    assert set(lc2._state) == {"blogging.planner"}
    assert lc2._state["blogging.planner"].status == SandboxStatus.WARM
    assert lc2._state["blogging.planner"].container_id == "abc123"


def test_build_run_argv_applies_hardening() -> None:
    argv = _build_run_argv(agent_id="blogging.planner", container_name="khala-sbx-blogging.planner")
    # Acceptance criteria from issue #264 / #255.
    assert "--cap-drop=ALL" in argv
    assert "--read-only" in argv
    assert "--security-opt=no-new-privileges:true" in argv
    assert "--security-opt=seccomp=default" in argv
    assert "--pids-limit=512" in argv
    assert "--memory=1g" in argv
    assert "--cpus=1.0" in argv
    # Loopback-only port binding.
    assert "-p" in argv
    p_index = argv.index("-p")
    assert argv[p_index + 1] == "127.0.0.1::8090"
    # tmpfs for /tmp and /run (read-only root leaves no writable paths otherwise).
    assert argv.count("--tmpfs") == 2
    # SANDBOX_AGENT_ID wiring for the Phase 1 entrypoint.
    assert any(a == "SANDBOX_AGENT_ID=blogging.planner" for a in argv)
    # Attaches to the existing khala-sandbox bridge network.
    assert "--network" in argv
    n_index = argv.index("--network")
    assert argv[n_index + 1] == "khala-sandbox"


def test_container_name_is_dns_safe() -> None:
    assert container_name_for("blogging.planner") == "khala-sbx-blogging.planner"
    assert container_name_for("weird agent/name!") == "khala-sbx-weird-agent-name"
    assert container_name_for("") == "khala-sbx-agent"


@pytest.mark.asyncio
async def test_run_idle_reaper_is_cancellable(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)

    # Very short interval + reap_once stub — we just assert the loop honors cancel.
    async def _fast_sleep(_):
        return None

    with (
        patch(
            "agent_provisioning_team.sandbox.lifecycle.asyncio.sleep",
            new=AsyncMock(side_effect=_fast_sleep),
        ),
        patch.object(Lifecycle, "reap_once", new=AsyncMock(return_value=[])),
    ):
        task = asyncio.create_task(lc.run_idle_reaper(interval_s=0))
        # Let it loop a few times then cancel.
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_lifecycle_handle_carries_container_metadata(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    # Directly seed a WARM entry and ensure _handle projects all fields.
    from agent_provisioning_team.sandbox.state import SandboxState, now

    t = now()
    st = SandboxState(
        agent_id="blogging.planner",
        team="blogging",
        container_name="khala-sbx-blogging.planner",
        container_id="abc123",
        host_port=55123,
        status=SandboxStatus.WARM,
        created_at=t,
        last_used_at=t,
    )
    handle = lc._handle(st)
    assert handle.url == "http://127.0.0.1:55123"
    assert handle.container_id == "abc123"
    assert handle.host_port == 55123
    assert handle.team == "blogging"
    # COLD/ERROR statuses should not expose a URL.
    st_cold = SandboxState(
        agent_id="x.y",
        team="blogging",
        container_name="khala-sbx-x.y",
        status=SandboxStatus.COLD,
        created_at=t,
        last_used_at=t,
    )
    assert lc._handle(st_cold).url is None


# --- tests for the small module-level helpers ------------------------------


def test_sandbox_image_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_provisioning_team.sandbox import state as state_mod

    monkeypatch.setenv("AGENT_PROVISIONING_SANDBOX_IMAGE", "my/custom:tag")
    assert state_mod.sandbox_image() == "my/custom:tag"
    monkeypatch.delenv("AGENT_PROVISIONING_SANDBOX_IMAGE")
    assert state_mod.sandbox_image() == "khala-agent-sandbox:latest"


def test_idle_threshold_prefers_per_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_provisioning_team.sandbox import state as state_mod

    monkeypatch.setenv("AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES", "2")
    assert state_mod.idle_teardown_seconds() == 120
    monkeypatch.delenv("AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES")
    monkeypatch.setenv("SANDBOX_IDLE_TEARDOWN_MINUTES", "10")
    assert state_mod.idle_teardown_seconds() == 600
    monkeypatch.delenv("SANDBOX_IDLE_TEARDOWN_MINUTES")
    assert state_mod.idle_teardown_seconds() == 300  # 5-minute default


def test_state_file_path_uses_agent_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from agent_provisioning_team.sandbox import state as state_mod

    monkeypatch.delenv("AGENT_PROVISIONING_SANDBOX_STATE_FILE", raising=False)
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    path = state_mod.state_file_path()
    assert path == tmp_path / "agent_provisioning" / "sandboxes" / "state.json"
