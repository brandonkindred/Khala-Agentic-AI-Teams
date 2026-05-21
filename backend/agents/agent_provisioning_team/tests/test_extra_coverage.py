"""Extra targeted tests to close the last coverage gaps.

* Lifecycle._wait_healthy timeout path
* Lifecycle.run_idle_reaper non-cancel exception swallowed
* Lifecycle.reap_once with non-warm state
* _resolve_team registry success/failure
* sandbox.provisioner._exec timeout (real coroutine)
* sandbox.provisioner._materialise_project_dir asset copy when grafana exists
* tool_agents.base.canonical_anatomy_preamble
* Last few credential_store branches
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Lifecycle._wait_healthy — deadline exceeded path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_healthy_exceeds_deadline(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.sandbox import lifecycle as lc_mod

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
    from agent_provisioning_team.sandbox import lifecycle as lc_mod

    lc = lc_mod.Lifecycle(state_file=tmp_path / "s.json")

    async def fake_get(self, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        return resp

    with patch("httpx.AsyncClient.get", new=fake_get):
        await lc._wait_healthy(host_port=12345)


# ---------------------------------------------------------------------------
# Lifecycle.run_idle_reaper non-cancel exception is logged and loop continues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_idle_reaper_swallows_non_cancel_exception(tmp_path: Path) -> None:
    from agent_provisioning_team.sandbox import lifecycle as lc_mod

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


# ---------------------------------------------------------------------------
# Lifecycle.reap_once skips non-warm states
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reap_once_skips_non_warm(tmp_path: Path) -> None:
    from agent_provisioning_team.sandbox import lifecycle as lc_mod
    from agent_provisioning_team.sandbox.state import (
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


# ---------------------------------------------------------------------------
# _resolve_team — happy and unknown
# ---------------------------------------------------------------------------


def test_resolve_team_success() -> None:
    from agent_provisioning_team.sandbox.lifecycle import _resolve_team

    fake_manifest = MagicMock()
    fake_manifest.team = "myteam"
    fake_registry = MagicMock()
    fake_registry.get.return_value = fake_manifest

    with patch(
        "agent_registry.get_registry",
        return_value=fake_registry,
    ):
        assert _resolve_team("agent.x") == "myteam"


def test_resolve_team_unknown() -> None:
    from agent_provisioning_team.sandbox.lifecycle import (
        UnknownAgentError,
        _resolve_team,
    )

    fake_registry = MagicMock()
    fake_registry.get.return_value = None

    with patch("agent_registry.get_registry", return_value=fake_registry):
        with pytest.raises(UnknownAgentError):
            _resolve_team("ghost")


# ---------------------------------------------------------------------------
# sandbox.provisioner._exec timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provisioner_exec_kills_on_timeout(monkeypatch) -> None:
    from agent_provisioning_team.sandbox import provisioner as pm

    # Real proc-stub that hangs in communicate until cancelled.
    proc_stub = MagicMock()
    proc_stub.kill = MagicMock()

    async def comm():
        await asyncio.sleep(10)
        return (b"", b"")

    async def wait():
        return None

    proc_stub.communicate = comm
    proc_stub.wait = wait
    proc_stub.returncode = 0

    async def fake_create(*args, **kwargs):
        return proc_stub

    monkeypatch.setattr(pm.asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(pm.DockerError, match="timed out"):
        # Wait for a very small timeout
        await pm._exec(["docker", "info"], timeout_s=0)

    proc_stub.kill.assert_called_once()


# ---------------------------------------------------------------------------
# sandbox.provisioner._materialise_project_dir — when grafana assets exist
# ---------------------------------------------------------------------------


def test_materialise_project_dir_copies_grafana(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.sandbox import provisioner as pm

    # sandbox_stack_assets_dir() returns the template's parent directory,
    # so colocate the support files with the template.
    template = tmp_path / "sandbox-stack.yml"
    template.write_text(
        "sandbox: {sandbox_id}\nagent: {agent_id}\nimage: {agent_image}\n"
        "pw: {pg_password}\nsecrets: {agent_secrets_file}\nport: {agent_host_port}\n"
    )
    (tmp_path / "postgres-init.sql").write_text("-- init")
    (tmp_path / "prometheus.yml").write_text("global: {}")
    grafana = tmp_path / "grafana-provisioning"
    grafana.mkdir()
    (grafana / "datasources.yml").write_text("apiVersion: 1")

    monkeypatch.setenv("AGENT_PROVISIONING_SANDBOX_STACK_TEMPLATE", str(template))
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path / "cache"))

    path = pm._materialise_project_dir("khala-sbx-test", host_port=12345, postgres_password="pw")
    assert (path / "docker-compose.yml").exists()
    assert (path / "grafana-provisioning" / "datasources.yml").exists()
    # Re-run should overwrite (tests the grafana_dst exists rmtree branch).
    pm._materialise_project_dir("khala-sbx-test", host_port=12345, postgres_password="pw")


# ---------------------------------------------------------------------------
# tool_agents.base.canonical_anatomy_preamble — instance method
# ---------------------------------------------------------------------------


def test_canonical_anatomy_prompt_preamble_via_instance(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.provisioner_state import ProvisionerStateStore
    from agent_provisioning_team.tool_agents.generic_provisioner import GenericProvisionerTool

    prov = GenericProvisionerTool("x")
    prov._state = ProvisionerStateStore("generic_x_provisioner", storage_dir=tmp_path)
    text = prov.canonical_anatomy_prompt_preamble()
    assert isinstance(text, str)


# ---------------------------------------------------------------------------
# credential_store — corrupt key error path
# ---------------------------------------------------------------------------


def test_credential_store_invalid_key_raises(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.credential_store import (
        CredentialStore,
        CredentialStoreConfigError,
    )

    with pytest.raises(CredentialStoreConfigError):
        CredentialStore(storage_dir=tmp_path, encryption_key="not-a-real-key")


def test_credential_store_rotate_skips_corrupt_files(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet

    from agent_provisioning_team.shared.credential_store import CredentialStore

    k = Fernet.generate_key().decode()
    store = CredentialStore(storage_dir=tmp_path, encryption_key=k)
    store.store_credentials("a1", "pg", {"password": "p"})

    # Drop a corrupt .enc file alongside the valid one.
    (tmp_path / "garbage.enc").write_bytes(b"not encrypted")

    new_k = Fernet.generate_key().decode()
    rotated = store.rotate_key(new_k)
    # Valid file rotated, garbage skipped — count is 1.
    assert rotated == 1


# ---------------------------------------------------------------------------
# start_workflow._run_async actually runs the coroutine
# ---------------------------------------------------------------------------


def test_run_async_runs_via_loop() -> None:
    from agent_provisioning_team.temporal import start_workflow as sw

    # Create an actual event loop and runs the coroutine in it.
    loop = asyncio.new_event_loop()
    fake_client = MagicMock()
    sentinel = {}

    async def go():
        sentinel["done"] = True
        return "OK"

    # Start the loop in a background thread so run_coroutine_threadsafe works.
    import threading

    def run_forever():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=run_forever, daemon=True)
    t.start()

    try:
        with (
            patch.object(sw, "get_temporal_client", return_value=fake_client),
            patch.object(sw, "get_temporal_loop", return_value=loop),
        ):
            result = sw._run_async(go())
        assert result == "OK"
        assert sentinel.get("done") is True
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)
        loop.close()


# ---------------------------------------------------------------------------
# provisioner_state — _save error path (raise during dump)
# ---------------------------------------------------------------------------


def test_provisioner_state_save_handles_io_error(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.shared.provisioner_state import ProvisionerStateStore

    store = ProvisionerStateStore("x", storage_dir=tmp_path)

    # Force os.replace to raise to exercise the cleanup path.
    with patch("os.replace", side_effect=OSError("io")):
        with pytest.raises(OSError):
            store.put("a1", {"x": 1})


# ---------------------------------------------------------------------------
# api/main — lifespan completion path
# ---------------------------------------------------------------------------


def test_lifespan_runs_cleanly(monkeypatch) -> None:
    """Entering + exiting the TestClient context runs the lifespan hook
    end-to-end with the executor + shutdown event."""
    from fastapi.testclient import TestClient

    from agent_provisioning_team.api import main as api_main

    monkeypatch.setattr(api_main, "_executor", None)
    with (
        patch.object(api_main, "list_jobs", return_value=[]),
        patch.object(api_main, "mark_all_running_jobs_failed"),
    ):
        with TestClient(api_main.app) as c:
            r = c.get("/health")
            assert r.status_code == 200


# ---------------------------------------------------------------------------
# anatomy_assets missing AGENT_ANATOMY.md fallback
# ---------------------------------------------------------------------------


def test_load_agent_anatomy_text_missing_file(monkeypatch) -> None:
    from agent_provisioning_team import anatomy_assets

    # Force the path attribute to a non-existent file.
    monkeypatch.setattr(anatomy_assets, "AGENT_ANATOMY_MD", anatomy_assets.PACKAGE_DIR / "ghost.md")
    anatomy_assets._anatomy_text_cache = None
    out = anatomy_assets.load_agent_anatomy_text()
    assert "Missing file" in out

    # Reset cache so subsequent tests see the real content again.
    anatomy_assets._anatomy_text_cache = None


def test_list_design_asset_paths_missing_dir(monkeypatch) -> None:
    from agent_provisioning_team import anatomy_assets

    monkeypatch.setattr(
        anatomy_assets, "DESIGN_ASSETS_DIR", anatomy_assets.PACKAGE_DIR / "ghost_dir"
    )
    out = anatomy_assets.list_design_asset_paths()
    assert out == []
