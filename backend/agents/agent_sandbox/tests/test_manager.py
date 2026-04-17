"""Unit tests for the SandboxManager state machine.

``docker compose`` subprocess and the health probe are patched so tests run
without a real Docker daemon.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_sandbox.config import TeamSandboxConfig
from agent_sandbox.manager import SandboxManager, UnknownTeamError
from agent_sandbox.models import SandboxStatus


def _configs() -> dict[str, TeamSandboxConfig]:
    return {
        "blogging": TeamSandboxConfig(
            team="blogging",
            service_name="blogging-sandbox",
            container_name="khala-sandbox-blogging",
            default_host_port=8200,
            port_env_var="BLOGGING_SANDBOX_PORT",
        )
    }


def _manager(tmp_path: Path) -> SandboxManager:
    return SandboxManager(
        compose_file=tmp_path / "compose.yml",
        state_file=tmp_path / "state.json",
        configs=_configs(),
    )


@pytest.mark.asyncio
async def test_ensure_warm_cold_to_warm(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    with (
        patch("agent_sandbox.manager.compose_mod.up_detached", new=AsyncMock()) as up,
        patch("agent_sandbox.manager.compose_mod.is_running", new=AsyncMock(return_value=False)),
        patch.object(SandboxManager, "_wait_healthy", new=AsyncMock()),
    ):
        handle = await mgr.ensure_warm("blogging")
    up.assert_awaited_once()
    assert handle.status == SandboxStatus.WARM
    assert handle.url == "http://localhost:8200"


@pytest.mark.asyncio
async def test_ensure_warm_is_idempotent_when_already_warm(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    with (
        patch("agent_sandbox.manager.compose_mod.up_detached", new=AsyncMock()) as up,
        patch(
            "agent_sandbox.manager.compose_mod.is_running",
            new=AsyncMock(return_value=True),
        ),
        patch.object(SandboxManager, "_wait_healthy", new=AsyncMock()),
    ):
        first = await mgr.ensure_warm("blogging")
        second = await mgr.ensure_warm("blogging")
    # `up_detached` is called on the first request (cold→warm); reused warm on second.
    assert up.await_count == 1
    assert first.status == SandboxStatus.WARM
    assert second.status == SandboxStatus.WARM


@pytest.mark.asyncio
async def test_ensure_warm_reports_error_on_health_timeout(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    with (
        patch("agent_sandbox.manager.compose_mod.up_detached", new=AsyncMock()),
        patch("agent_sandbox.manager.compose_mod.is_running", new=AsyncMock(return_value=False)),
        patch.object(
            SandboxManager, "_wait_healthy", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
    ):
        handle = await mgr.ensure_warm("blogging")
    assert handle.status == SandboxStatus.ERROR
    assert handle.error == "boom"


@pytest.mark.asyncio
async def test_status_reconciles_when_container_not_actually_running(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    with (
        patch("agent_sandbox.manager.compose_mod.up_detached", new=AsyncMock()),
        patch.object(SandboxManager, "_wait_healthy", new=AsyncMock()),
        patch("agent_sandbox.manager.compose_mod.is_running", new=AsyncMock(return_value=False)),
    ):
        await mgr.ensure_warm("blogging")
        # Still warm in state. Now simulate container disappearing.
        handle = await mgr.status("blogging")
    assert handle.status == SandboxStatus.COLD


@pytest.mark.asyncio
async def test_teardown_removes_state(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    with (
        patch("agent_sandbox.manager.compose_mod.up_detached", new=AsyncMock()),
        patch("agent_sandbox.manager.compose_mod.is_running", new=AsyncMock(return_value=False)),
        patch.object(SandboxManager, "_wait_healthy", new=AsyncMock()),
        patch("agent_sandbox.manager.compose_mod.stop_and_remove", new=AsyncMock()) as rm,
    ):
        await mgr.ensure_warm("blogging")
        await mgr.teardown("blogging")
    rm.assert_awaited_once()
    assert await mgr.list_warm() == []


@pytest.mark.asyncio
async def test_list_warm_returns_only_warm(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    with (
        patch("agent_sandbox.manager.compose_mod.up_detached", new=AsyncMock()),
        patch("agent_sandbox.manager.compose_mod.is_running", new=AsyncMock(return_value=False)),
        patch.object(SandboxManager, "_wait_healthy", new=AsyncMock()),
    ):
        await mgr.ensure_warm("blogging")
    warm = await mgr.list_warm()
    assert [h.team for h in warm] == ["blogging"]


@pytest.mark.asyncio
async def test_unknown_team_raises(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    with pytest.raises(UnknownTeamError):
        await mgr.ensure_warm("nonexistent_team")


def test_state_persists_across_manager_instances(tmp_path: Path) -> None:
    import asyncio

    mgr1 = _manager(tmp_path)
    with (
        patch("agent_sandbox.manager.compose_mod.up_detached", new=AsyncMock()),
        patch("agent_sandbox.manager.compose_mod.is_running", new=AsyncMock(return_value=False)),
        patch.object(SandboxManager, "_wait_healthy", new=AsyncMock()),
    ):
        asyncio.run(mgr1.ensure_warm("blogging"))
    # A fresh manager pointed at the same state file should see the warm entry.
    mgr2 = _manager(tmp_path)
    assert set(mgr2._state.keys()) == {"blogging"}
    assert mgr2._state["blogging"].status == SandboxStatus.WARM


@pytest.mark.asyncio
async def test_note_activity_updates_last_used(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    with (
        patch("agent_sandbox.manager.compose_mod.up_detached", new=AsyncMock()),
        patch("agent_sandbox.manager.compose_mod.is_running", new=AsyncMock(return_value=False)),
        patch.object(SandboxManager, "_wait_healthy", new=AsyncMock()),
    ):
        await mgr.ensure_warm("blogging")
    before = mgr._state["blogging"].last_used_at
    await mgr.note_activity("blogging")
    after = mgr._state["blogging"].last_used_at
    assert after >= before
