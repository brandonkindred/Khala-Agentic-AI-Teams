"""Additional targeted tests to close remaining coverage gaps.

Covers:
* anatomy_assets edge paths
* prompts.py format_* helpers and template factories
* graphs/provisioning_graph.py importability
* credential_store key file path
* phases/documentation LLM happy/fallback paths
* sandbox provisioner extra branches
* temporal.activities._load_ctx import path
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# prompts.py — format/template helpers
# ---------------------------------------------------------------------------


def test_format_onboarding_summary_prompt_contains_inputs() -> None:
    from agent_provisioning_team.prompts import format_onboarding_summary_prompt

    p = format_onboarding_summary_prompt(agent_id="a1", tool_names="pg, redis")
    assert "a1" in p
    assert "pg" in p
    # The preamble always includes the anatomy reference text.
    assert "AGENT_ANATOMY" in p or "anatomy" in p.lower()


def test_format_tool_getting_started_prompt() -> None:
    from agent_provisioning_team.prompts import format_tool_getting_started_prompt

    p = format_tool_getting_started_prompt(
        tool_name="pg", description="db", connection_details="conn", permissions="r,w"
    )
    assert "pg" in p


def test_format_environment_overview_prompt() -> None:
    from agent_provisioning_team.prompts import format_environment_overview_prompt

    p = format_environment_overview_prompt(
        container_name="c1",
        workspace_path="/w",
        tools_list="- pg",
        env_vars="A=B",
    )
    assert "/w" in p


def test_format_ai_agent_create_prompt() -> None:
    from agent_provisioning_team.prompts import format_ai_agent_create_prompt

    p = format_ai_agent_create_prompt(requirements="build x")
    assert "build x" in p


def test_format_ai_agent_refine_prompt() -> None:
    from agent_provisioning_team.prompts import format_ai_agent_refine_prompt

    p = format_ai_agent_refine_prompt("current", "goals")
    assert "current" in p and "goals" in p


def test_onboarding_summary_template_factory() -> None:
    from agent_provisioning_team.prompts import onboarding_summary_prompt

    p = onboarding_summary_prompt()
    assert "{agent_id}" in p


def test_tool_getting_started_template_factory() -> None:
    from agent_provisioning_team.prompts import tool_getting_started_prompt

    p = tool_getting_started_prompt()
    assert "{tool_name}" in p


def test_environment_overview_template_factory() -> None:
    from agent_provisioning_team.prompts import environment_overview_prompt

    p = environment_overview_prompt()
    assert "{container_name}" in p


def test_ai_agent_create_template_factory() -> None:
    from agent_provisioning_team.prompts import ai_agent_create_prompt

    p = ai_agent_create_prompt()
    assert "{requirements}" in p


def test_ai_agent_refine_template_factory() -> None:
    from agent_provisioning_team.prompts import ai_agent_refine_prompt

    p = ai_agent_refine_prompt()
    assert "{current_definition}" in p


# ---------------------------------------------------------------------------
# anatomy_assets edge cases
# ---------------------------------------------------------------------------


def test_load_agent_anatomy_text_cached() -> None:
    from agent_provisioning_team import anatomy_assets

    # Reset cache
    anatomy_assets._anatomy_text_cache = None
    first = anatomy_assets.load_agent_anatomy_text()
    second = anatomy_assets.load_agent_anatomy_text()
    assert first is second


def test_try_materialize_anatomy_bundle_root_skip() -> None:
    from agent_provisioning_team.anatomy_assets import try_materialize_anatomy_bundle

    assert try_materialize_anatomy_bundle(".") is None
    assert try_materialize_anatomy_bundle("/") is None
    assert try_materialize_anatomy_bundle("") is None


def test_try_materialize_anatomy_bundle_writes_files(tmp_path: Path) -> None:
    from agent_provisioning_team.anatomy_assets import try_materialize_anatomy_bundle

    result = try_materialize_anatomy_bundle(str(tmp_path))
    # If the source AGENT_ANATOMY.md exists in the package, result is a path.
    if result is not None:
        assert Path(result).exists()
        assert (Path(result) / "AGENT_ANATOMY.md").exists()


def test_list_design_asset_paths() -> None:
    from agent_provisioning_team.anatomy_assets import list_design_asset_paths

    paths = list_design_asset_paths()
    assert isinstance(paths, list)


def test_get_anatomy_prompt_preamble_includes_diagram_block() -> None:
    from agent_provisioning_team.anatomy_assets import get_anatomy_prompt_preamble

    text = get_anatomy_prompt_preamble()
    assert "AGENT_ANATOMY.md" in text
    assert "diagram" in text.lower()


# ---------------------------------------------------------------------------
# graphs/provisioning_graph.py — only need to import + invoke once
# ---------------------------------------------------------------------------


def test_build_provisioning_graph_returns_graph() -> None:
    from agent_provisioning_team.graphs.provisioning_graph import build_provisioning_graph

    g = build_provisioning_graph()
    assert g is not None


# ---------------------------------------------------------------------------
# credential_store — additional paths
# ---------------------------------------------------------------------------


def test_credential_store_with_keyfile_env(tmp_path: Path, monkeypatch) -> None:
    """PA_CREDENTIAL_KEY_FILE pointing at an existing file is read first."""
    from cryptography.fernet import Fernet

    from agent_provisioning_team.shared.credential_store import CredentialStore

    key_file = tmp_path / "key.bin"
    k = Fernet.generate_key()
    key_file.write_bytes(k)

    monkeypatch.setenv("PA_CREDENTIAL_KEY_FILE", str(key_file))
    monkeypatch.delenv("PROVISION_CREDENTIAL_KEY", raising=False)

    store = CredentialStore(storage_dir=tmp_path / "store")
    assert store.fernet is not None
    # Roundtrip a value to prove the key works.
    store.store_credentials("a1", "pg", {"password": "p"})
    assert store.get_credentials("a1", "pg") == {"password": "p"}


def test_credential_store_missing_keyfile_falls_through(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.setenv("PA_CREDENTIAL_KEY_FILE", str(tmp_path / "ghost"))
    monkeypatch.delenv("PROVISION_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("PROVISION_REQUIRE_KEY", raising=False)
    # No key file → falls through to auto-generated dev key.
    store = CredentialStore(storage_dir=tmp_path / "store")
    assert store.fernet is not None


def test_credential_store_generate_key_static() -> None:
    from agent_provisioning_team.shared.credential_store import CredentialStore

    k = CredentialStore.generate_key()
    assert isinstance(k, str) and len(k) > 40


def test_credential_store_username_sanitization() -> None:
    from agent_provisioning_team.shared.credential_store import CredentialStore

    u = CredentialStore.generate_username("agent-1!@#", "pg/sql")
    # Only alnum + _ in the output
    for c in u:
        assert c.isalnum() or c == "_"


def test_credential_store_get_credentials_corrupt(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet

    from agent_provisioning_team.shared.credential_store import CredentialStore

    k1 = Fernet.generate_key().decode()
    store = CredentialStore(storage_dir=tmp_path, encryption_key=k1)
    # Hand-write a corrupt file
    path = store._agent_file("agent-x")
    path.write_bytes(b"garbage")
    assert store.get_credentials("agent-x") is None


def test_credential_store_rotate_key_invalid_raises(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.credential_store import (
        CredentialStore,
        CredentialStoreConfigError,
    )

    store = CredentialStore(storage_dir=tmp_path)
    with pytest.raises(CredentialStoreConfigError):
        store.rotate_key("not-a-valid-fernet-key")


def test_credential_store_delete_missing_returns_false(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    assert store.delete_credentials("nobody") is False


def test_credential_store_list_agents(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    store.store_credentials("a1", "pg", {"password": "p"})
    store.store_credentials("a2", "redis", {"password": "p"})
    out = sorted(store.list_agents())
    assert out == ["a1", "a2"]


def test_credential_store_store_credentials_handles_corrupt_existing(tmp_path: Path) -> None:
    """If an existing encrypted file is corrupt, store overwrites it."""
    from agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    path = store._agent_file("a1")
    path.write_bytes(b"garbage")

    store.store_credentials("a1", "pg", {"password": "p"})
    assert store.get_credentials("a1", "pg") == {"password": "p"}


def test_credential_store_get_credentials_returns_all_when_no_tool(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    store.store_credentials("a1", "pg", {"password": "p1"})
    store.store_credentials("a1", "redis", {"password": "p2"})
    out = store.get_credentials("a1")
    assert set(out.keys()) == {"pg", "redis"}


def test_credential_store_load_key_with_blank_env(tmp_path: Path, monkeypatch) -> None:
    """Empty PROVISION_CREDENTIAL_KEY is treated as unset."""
    from agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.setenv("PROVISION_CREDENTIAL_KEY", "")
    monkeypatch.delenv("PA_CREDENTIAL_KEY_FILE", raising=False)
    monkeypatch.delenv("PROVISION_REQUIRE_KEY", raising=False)
    store = CredentialStore(storage_dir=tmp_path)
    assert store.fernet is not None


@pytest.mark.parametrize("partial", [b"", b"   ", b"not-a-valid-fernet-key"])
def test_credential_store_tolerates_partial_key_file(tmp_path: Path, monkeypatch, partial) -> None:
    """A present-but-invalid key file (the concurrent-write race window) is
    replaced rather than crashing the store with an invalid Fernet key."""
    from agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.delenv("PROVISION_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("PA_CREDENTIAL_KEY_FILE", raising=False)
    monkeypatch.delenv("PROVISION_REQUIRE_KEY", raising=False)
    sdir = tmp_path / "store"
    sdir.mkdir()
    (sdir / ".encryption_key").write_bytes(partial)  # simulate a half-written file

    store = CredentialStore(storage_dir=sdir)  # must not raise
    store.store_credentials("a1", "pg", {"password": "secret"})
    assert store.get_credentials("a1", "pg") == {"password": "secret"}


def test_credential_store_concurrent_init_converges_on_one_key(tmp_path: Path, monkeypatch) -> None:
    """Concurrent first-time inits converge on a single key and never clobber
    a key a peer has already published and encrypted credentials under."""
    import threading

    from agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.delenv("PROVISION_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("PA_CREDENTIAL_KEY_FILE", raising=False)
    monkeypatch.delenv("PROVISION_REQUIRE_KEY", raising=False)
    sdir = tmp_path / "shared"
    n = 12
    errors: list[Exception] = []
    barrier = threading.Barrier(n)

    def _build(i: int) -> None:
        try:
            barrier.wait()  # release all threads into key-init at once
            store = CredentialStore(storage_dir=sdir)
            store.store_credentials(f"agent{i}", "pg", {"password": f"p{i}"})
        except Exception as exc:  # pragma: no cover - only on regression
            errors.append(exc)

    threads = [threading.Thread(target=_build, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent init raised: {errors}"
    # If any init had clobbered another's published key, the credentials that
    # peer encrypted would now be undecryptable. A fresh store must decrypt all.
    fresh = CredentialStore(storage_dir=sdir)
    for i in range(n):
        assert fresh.get_credentials(f"agent{i}", "pg") == {"password": f"p{i}"}


# ---------------------------------------------------------------------------
# documentation phase — LLM path (mock LLMClient.is_configured)
# ---------------------------------------------------------------------------


def test_documentation_uses_llm_summary_when_configured(tmp_path: Path) -> None:
    from agent_provisioning_team.phases import documentation as doc_mod
    from agent_provisioning_team.shared.tool_manifest import ToolManifest

    captured = {}

    class _StubLLM:
        is_configured = True

        def complete(self, req):
            captured["called"] = True
            return "FAKE_LLM_SUMMARY"

    stub = _StubLLM()
    with patch.object(doc_mod, "_LLM", stub):
        result = doc_mod.run_documentation(
            agent_id="a1",
            manifest=ToolManifest(),
            credentials={},
            tool_results=[],
            workspace_path=str(tmp_path),
        )
    assert result.success is True
    assert "FAKE_LLM_SUMMARY" in result.onboarding.summary


def test_documentation_llm_summary_falls_back_on_exception(tmp_path: Path) -> None:
    from agent_provisioning_team.phases import documentation as doc_mod
    from agent_provisioning_team.shared.tool_manifest import ToolManifest

    class _BoomLLM:
        is_configured = True

        def complete(self, req):
            raise RuntimeError("api down")

    with patch.object(doc_mod, "_LLM", _BoomLLM()):
        result = doc_mod.run_documentation(
            agent_id="a1",
            manifest=ToolManifest(),
            credentials={},
            tool_results=[],
            workspace_path=str(tmp_path),
        )
    # Falls back to deterministic template
    assert "tool(s) configured" in result.onboarding.summary


def test_documentation_uses_llm_getting_started_when_configured(tmp_path: Path) -> None:
    from agent_provisioning_team.models import (
        GeneratedCredentials,
        ToolProvisionResult,
    )
    from agent_provisioning_team.phases import documentation as doc_mod
    from agent_provisioning_team.shared.tool_manifest import ToolDefinition, ToolManifest

    manifest = ToolManifest(
        tools=[
            ToolDefinition(
                name="redis",
                provisioner="redis_provisioner",
                config={},
                onboarding={
                    "description": "Redis",
                    "env_var": "REDIS_URL",
                    "getting_started": "",  # empty → triggers LLM path
                },
            ),
        ]
    )

    class _StubLLM:
        is_configured = True

        def complete(self, req):
            return "FAKE_TOOL_DOC"

    with patch.object(doc_mod, "_LLM", _StubLLM()):
        result = doc_mod.run_documentation(
            agent_id="a1",
            manifest=manifest,
            credentials={
                "redis": GeneratedCredentials(tool_name="redis", connection_string="redis://x")
            },
            tool_results=[
                ToolProvisionResult(
                    tool_name="redis",
                    success=True,
                    permissions=["+@all"],
                    provisioner_key="redis_provisioner",
                )
            ],
            workspace_path=str(tmp_path),
        )

    # LLM-generated docs appear in the tool's getting_started field
    assert any("FAKE_TOOL_DOC" in t.getting_started for t in result.onboarding.tools)


def test_documentation_llm_getting_started_falls_back_on_exception(tmp_path: Path) -> None:
    from agent_provisioning_team.models import (
        GeneratedCredentials,
        ToolProvisionResult,
    )
    from agent_provisioning_team.phases import documentation as doc_mod
    from agent_provisioning_team.shared.tool_manifest import ToolDefinition, ToolManifest

    manifest = ToolManifest(
        tools=[
            ToolDefinition(
                name="redis",
                provisioner="redis_provisioner",
                config={},
                onboarding={
                    "description": "Redis",
                    "env_var": "REDIS_URL",
                    "getting_started": "",
                },
            ),
        ]
    )

    class _BoomLLM:
        is_configured = True

        def complete(self, req):
            raise RuntimeError("api down")

    with patch.object(doc_mod, "_LLM", _BoomLLM()):
        result = doc_mod.run_documentation(
            agent_id="a1",
            manifest=manifest,
            credentials={
                "redis": GeneratedCredentials(tool_name="redis", connection_string="redis://x")
            },
            tool_results=[
                ToolProvisionResult(
                    tool_name="redis",
                    success=True,
                    permissions=["+@all"],
                    provisioner_key="redis_provisioner",
                )
            ],
            workspace_path=str(tmp_path),
        )
    # Falls back to deterministic template (mentions env var).
    assert any("REDIS_URL" in t.getting_started for t in result.onboarding.tools)


def test_documentation_getting_started_template_substitutes_username(tmp_path: Path) -> None:
    """{username} and {connection_string} placeholders get substituted from creds.extra."""
    from agent_provisioning_team.models import (
        GeneratedCredentials,
        ToolProvisionResult,
    )
    from agent_provisioning_team.phases.documentation import run_documentation
    from agent_provisioning_team.shared.tool_manifest import ToolDefinition, ToolManifest

    manifest = ToolManifest(
        tools=[
            ToolDefinition(
                name="pg",
                provisioner="postgres_provisioner",
                config={},
                onboarding={
                    "description": "PG",
                    "getting_started": "user={username} extra={port}",
                },
            ),
        ]
    )

    creds = GeneratedCredentials(
        tool_name="pg",
        username="u1",
        password="p",
        connection_string="conn",
        extra={"port": 5432},
    )
    tool_results = [
        ToolProvisionResult(
            tool_name="pg",
            success=True,
            permissions=["ALL"],
            provisioner_key="postgres_provisioner",
        )
    ]

    result = run_documentation(
        agent_id="a1",
        manifest=manifest,
        credentials={"pg": creds},
        tool_results=tool_results,
        workspace_path=str(tmp_path),
    )

    rendered = result.onboarding.tools[0].getting_started
    assert "user=u1" in rendered
    assert "extra=5432" in rendered


# ---------------------------------------------------------------------------
# temporal.activities._load_ctx — exercise both branches
# ---------------------------------------------------------------------------


def test_load_ctx_returns_orchestrator_and_manifest(tmp_path: Path) -> None:
    from agent_provisioning_team.temporal import activities as t_acts

    # Build a minimal manifest YAML on disk so load_manifest finds something.
    f = tmp_path / "m.yaml"
    f.write_text(
        """
version: "1.0"
tools:
  - name: pg
    provisioner: postgres_provisioner
    config: {database_prefix: "x_"}
""",
        encoding="utf-8",
    )

    orch, manifest = t_acts._load_ctx(str(f))
    assert orch is not None
    assert manifest is not None
    assert manifest.tool_names == ["pg"]


# ---------------------------------------------------------------------------
# sandbox.provisioner — more lifecycle branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_handles_timeout(monkeypatch) -> None:
    from agent_provisioning_team.sandbox import provisioner as pm

    async def fake_create_subprocess_exec(*args, **kwargs):
        # Simulate a stuck subprocess by returning a proc whose communicate
        # never resolves before our timeout fires.
        proc = MagicMock()
        proc.communicate = MagicMock(return_value=asyncio.Future())
        proc.kill = MagicMock()
        proc.wait = MagicMock(return_value=asyncio.Future())
        proc.returncode = 1
        # Make wait_for actually time out
        proc.communicate.return_value.set_exception(asyncio.TimeoutError())
        proc.wait.return_value.set_result(None)
        return proc

    # Easier: patch asyncio.wait_for to raise TimeoutError directly.
    monkeypatch.setattr(
        pm.asyncio,
        "create_subprocess_exec",
        lambda *a, **kw: _make_proc_stub(),
    )

    with patch.object(pm.asyncio, "wait_for", side_effect=asyncio.TimeoutError()):
        with pytest.raises(pm.DockerError, match="timed out"):
            await pm._exec(["docker", "info"], timeout_s=1)


def _make_proc_stub():
    """Return a coroutine that resolves to a stub Process-like object."""

    async def _coro():
        proc = MagicMock()

        async def _comm():
            return (b"", b"")

        async def _wait():
            return None

        proc.communicate = _comm
        proc.kill = MagicMock()
        proc.wait = _wait
        proc.returncode = 0
        return proc

    return _coro()


@pytest.mark.asyncio
async def test_is_running_true(monkeypatch) -> None:
    from unittest.mock import AsyncMock as _AsyncMock

    from agent_provisioning_team.sandbox import provisioner as pm

    monkeypatch.setattr(pm, "_exec", _AsyncMock(return_value=(0, "true\n", "")))
    assert await pm.is_running("c1") is True


@pytest.mark.asyncio
async def test_is_running_false(monkeypatch) -> None:
    from unittest.mock import AsyncMock as _AsyncMock

    from agent_provisioning_team.sandbox import provisioner as pm

    monkeypatch.setattr(pm, "_exec", _AsyncMock(return_value=(0, "false\n", "")))
    assert await pm.is_running("c1") is False


@pytest.mark.asyncio
async def test_is_running_returns_false_on_error(monkeypatch) -> None:
    from unittest.mock import AsyncMock as _AsyncMock

    from agent_provisioning_team.sandbox import provisioner as pm

    monkeypatch.setattr(pm, "_exec", _AsyncMock(return_value=(1, "", "no such container")))
    assert await pm.is_running("c1") is False


@pytest.mark.asyncio
async def test_inspect_host_port_raises_on_invalid_output(monkeypatch) -> None:
    from unittest.mock import AsyncMock as _AsyncMock

    from agent_provisioning_team.sandbox import provisioner as pm

    monkeypatch.setattr(pm, "_exec", _AsyncMock(return_value=(0, "not-a-port\n", "")))
    with pytest.raises(pm.DockerError):
        await pm.inspect_host_port("c1")


@pytest.mark.asyncio
async def test_inspect_host_port_raises_on_nonzero(monkeypatch) -> None:
    from unittest.mock import AsyncMock as _AsyncMock

    from agent_provisioning_team.sandbox import provisioner as pm

    monkeypatch.setattr(pm, "_exec", _AsyncMock(return_value=(1, "", "boom")))
    with pytest.raises(pm.DockerError):
        await pm.inspect_host_port("c1")


@pytest.mark.asyncio
async def test_inspect_host_port_success(monkeypatch) -> None:
    from unittest.mock import AsyncMock as _AsyncMock

    from agent_provisioning_team.sandbox import provisioner as pm

    monkeypatch.setattr(pm, "_exec", _AsyncMock(return_value=(0, "55123\n", "")))
    port = await pm.inspect_host_port("c1")
    assert port == 55123


@pytest.mark.asyncio
async def test_run_container_cleans_up_on_inspect_failure(tmp_path: Path, monkeypatch) -> None:
    """If `docker inspect` fails after compose up, project dir is cleaned up."""
    from agent_provisioning_team.sandbox import provisioner as pm

    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))

    async def fake_exec(cmd, *, timeout_s=30):
        if cmd[:2] == ["docker", "compose"]:
            return 0, "", ""
        if cmd[:2] == ["docker", "inspect"]:
            return 1, "", "no such container"
        return 0, "", ""

    monkeypatch.setattr(pm, "_exec", fake_exec)

    # Resolvable agent so run_container reaches the compose-up → inspect-fail path
    # (the cleanup branch under test) rather than the earlier unknown-agent fail-fast.
    with pytest.raises(pm.DockerError):
        await pm.run_container(
            agent_id="blogging.planner", container_name="khala-sbx-inspect-fail", team="x"
        )

    # Project dir must be removed on cleanup.
    project_dir = (
        tmp_path / "agent_provisioning" / "sandboxes" / "stacks" / "khala-sbx-inspect-fail"
    )
    assert not project_dir.exists()


@pytest.mark.asyncio
async def test_run_container_cleans_up_on_docker_error_in_exec(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.sandbox import provisioner as pm

    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))

    async def boom(cmd, *, timeout_s=30):
        if cmd[:2] == ["docker", "compose"]:
            raise pm.DockerError("compose timed out")
        return 0, "", ""

    monkeypatch.setattr(pm, "_exec", boom)

    with pytest.raises(pm.DockerError):
        await pm.run_container(
            agent_id="blogging.planner", container_name="khala-sbx-boom", team="x"
        )

    project_dir = tmp_path / "agent_provisioning" / "sandboxes" / "stacks" / "khala-sbx-boom"
    assert not project_dir.exists()


@pytest.mark.asyncio
async def test_stop_container_raises_for_unexpected_failure(monkeypatch) -> None:

    from agent_provisioning_team.sandbox import provisioner as pm

    async def fake_exec(cmd, *, timeout_s=30):
        if cmd[:3] == ["docker", "inspect", "--format"]:
            return 1, "", "no such container"
        if cmd[:2] == ["docker", "compose"]:
            # Real failure (not "no such")
            return 1, "", "unexpected error from compose"
        return 0, "", ""

    monkeypatch.setattr(pm, "_exec", fake_exec)
    with pytest.raises(pm.DockerError):
        await pm.stop_container("khala-sbx-error")


def test_ensure_network_is_noop_shim() -> None:
    from agent_provisioning_team.sandbox import provisioner as pm

    asyncio.run(pm.ensure_network())


def test_cleanup_secrets_file_swallows_oserror(tmp_path: Path, monkeypatch) -> None:
    """rmtree failures are logged but not raised."""
    from agent_provisioning_team.sandbox import provisioner as pm

    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    project_dir = pm.sandbox_project_dir("khala-sbx-x")
    project_dir.mkdir(parents=True)
    (project_dir / "agent.env").write_text("X=1")

    with patch.object(pm.shutil, "rmtree", side_effect=OSError("io")):
        pm.cleanup_secrets_file("khala-sbx-x")
    # No exception raised. The project dir still exists because rmtree was mocked.


# ---------------------------------------------------------------------------
# sandbox.lifecycle — module helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_module_helper_status(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team import sandbox as sb
    from agent_provisioning_team.sandbox import lifecycle as lc_mod

    lc = lc_mod.Lifecycle(state_file=tmp_path / "s.json")
    lc_mod.get_lifecycle.cache_clear()
    monkeypatch.setattr(lc_mod, "get_lifecycle", lambda: lc)

    with patch.object(lc_mod, "_resolve_team", return_value="t"):
        handle = await sb.status("some.agent")
    assert handle.agent_id == "some.agent"


@pytest.mark.asyncio
async def test_module_helper_metrics(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team import sandbox as sb
    from agent_provisioning_team.sandbox import lifecycle as lc_mod

    lc = lc_mod.Lifecycle(state_file=tmp_path / "s.json")
    lc_mod.get_lifecycle.cache_clear()
    monkeypatch.setattr(lc_mod, "get_lifecycle", lambda: lc)
    snap = await sb.metrics()
    assert snap.resident == 0


@pytest.mark.asyncio
async def test_module_helper_run_idle_reaper(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import AsyncMock as _AsyncMock

    from agent_provisioning_team import sandbox as sb
    from agent_provisioning_team.sandbox import lifecycle as lc_mod

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
    from agent_provisioning_team.sandbox.lifecycle import Lifecycle

    lc = Lifecycle(state_file=tmp_path / "s.json")
    # No state for "ghost" — call must be a no-op rather than raise.
    await lc.note_activity("ghost")


@pytest.mark.asyncio
async def test_lifecycle_teardown_missing_no_op(tmp_path: Path) -> None:
    from agent_provisioning_team.sandbox.lifecycle import Lifecycle

    lc = Lifecycle(state_file=tmp_path / "s.json")
    await lc.teardown("ghost")


@pytest.mark.asyncio
async def test_lifecycle_persist_swallows_oserror(tmp_path: Path, monkeypatch) -> None:
    """If `state.save` raises, the public method completes without re-raising."""
    from agent_provisioning_team.sandbox import lifecycle as lc_mod

    lc = lc_mod.Lifecycle(state_file=tmp_path / "s.json")

    def fail_save(path, state):
        raise OSError("disk full")

    with patch.object(lc_mod.state_mod, "save", side_effect=fail_save):
        lc._persist()


# ---------------------------------------------------------------------------
# sandbox.state load + save edge cases
# ---------------------------------------------------------------------------


def test_state_load_missing_file_returns_empty(tmp_path: Path) -> None:
    from agent_provisioning_team.sandbox import state as state_mod

    out = state_mod.load(tmp_path / "ghost.json")
    assert out == {}


def test_state_load_corrupt_returns_empty(tmp_path: Path) -> None:
    from agent_provisioning_team.sandbox import state as state_mod

    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert state_mod.load(bad) == {}


def test_state_load_drops_malformed_entries(tmp_path: Path) -> None:
    import json

    from agent_provisioning_team.sandbox import state as state_mod

    f = tmp_path / "s.json"
    f.write_text(
        json.dumps({"a1": {"missing": "fields"}}),  # invalid SandboxState shape
        encoding="utf-8",
    )
    out = state_mod.load(f)
    assert out == {}


def test_state_save_roundtrip(tmp_path: Path) -> None:
    from agent_provisioning_team.sandbox.state import (
        SandboxStatus,
        load,
        new_state,
        save,
    )

    state = {
        "a1": new_state(agent_id="a1", team="t", container_name="khala-sbx-a1"),
    }
    state["a1"].status = SandboxStatus.COLD
    f = tmp_path / "s.json"
    save(f, state)
    loaded = load(f)
    assert "a1" in loaded
    assert loaded["a1"].status == SandboxStatus.COLD


def test_boot_timeout_seconds(monkeypatch) -> None:
    from agent_provisioning_team.sandbox.state import boot_timeout_seconds

    monkeypatch.delenv("AGENT_PROVISIONING_SANDBOX_BOOT_TIMEOUT_S", raising=False)
    assert boot_timeout_seconds() == 90
    monkeypatch.setenv("AGENT_PROVISIONING_SANDBOX_BOOT_TIMEOUT_S", "30")
    assert boot_timeout_seconds() == 30


def test_sandbox_stack_template_path_override(monkeypatch, tmp_path: Path) -> None:
    from agent_provisioning_team.sandbox import state as state_mod

    template = tmp_path / "custom.yml"
    template.write_text("services: {}", encoding="utf-8")
    monkeypatch.setenv("AGENT_PROVISIONING_SANDBOX_STACK_TEMPLATE", str(template))
    assert state_mod.sandbox_stack_template_path() == template
    assert state_mod.sandbox_stack_assets_dir() == template.parent


def test_sandbox_stack_template_path_default(monkeypatch) -> None:
    from agent_provisioning_team.sandbox import state as state_mod

    monkeypatch.delenv("AGENT_PROVISIONING_SANDBOX_STACK_TEMPLATE", raising=False)
    p = state_mod.sandbox_stack_template_path()
    # Should resolve to the in-tree path; existence not required for the
    # function itself.
    assert "agent_sandbox_image" in str(p)


def test_state_file_path_with_override(monkeypatch) -> None:
    from agent_provisioning_team.sandbox import state as state_mod

    monkeypatch.setenv("AGENT_PROVISIONING_SANDBOX_STATE_FILE", "/tmp/x.json")
    assert str(state_mod.state_file_path()) == "/tmp/x.json"
