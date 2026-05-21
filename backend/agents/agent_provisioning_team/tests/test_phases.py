"""Unit tests for each phase function.

These cover the procedural orchestration in `phases/` directly so we
don't have to drive a full workflow through the orchestrator to land
coverage on every branch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_provisioning_team.models import (
    AccessAuditResult,
    DeliverResult,
    EnvironmentInfo,
    GeneratedCredentials,
    OnboardingPacket,
    Phase,
    ProvisioningResult,
    ToolOnboardingInfo,
    ToolProvisionResult,
)

# ---------------------------------------------------------------------------
# setup phase
# ---------------------------------------------------------------------------


def test_run_setup_reuses_existing_running_environment(tmp_path: Path) -> None:
    from agent_provisioning_team.phases.setup import run_setup
    from agent_provisioning_team.shared.environment_store import (
        EnvironmentInfo as StoreEnvInfo,
    )
    from agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_provisioning_team.shared.tool_manifest import ToolManifest

    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    env_store.register(
        StoreEnvInfo(
            agent_id="a1",
            container_id="c1",
            container_name="agent-a1",
            ssh_host="localhost",
            ssh_port=22001,
            workspace_path="/workspace/a1",
            status="running",
        )
    )

    manifest = ToolManifest()
    docker = MagicMock()

    calls = []

    def cb(msg):
        calls.append(msg)

    result = run_setup(
        agent_id="a1",
        manifest=manifest,
        environment_store=env_store,
        docker_provisioner=docker,
        progress_callback=cb,
    )

    assert result.success is True
    assert result.environment.container_id == "c1"
    # Existing env path doesn't shell out to docker
    docker.provision.assert_not_called()


def test_run_setup_creates_new_container(tmp_path: Path) -> None:
    from agent_provisioning_team.phases.setup import run_setup
    from agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_provisioning_team.shared.tool_manifest import ToolManifest

    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    manifest = ToolManifest()

    docker = MagicMock()
    docker.provision.return_value = ToolProvisionResult(
        tool_name="docker",
        success=True,
        details={
            "container_id": "c-new",
            "container_name": "agent-a2",
            "ssh_port": 22002,
            "workspace_path": "/workspace/a2",
        },
    )

    result = run_setup(
        agent_id="a2",
        manifest=manifest,
        environment_store=env_store,
        docker_provisioner=docker,
        progress_callback=lambda msg: None,
    )

    assert result.success is True
    assert result.environment.container_id == "c-new"
    assert env_store.exists("a2")


def test_run_setup_returns_failure_on_docker_error(tmp_path: Path) -> None:
    from agent_provisioning_team.phases.setup import run_setup
    from agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_provisioning_team.shared.tool_manifest import ToolManifest

    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    docker = MagicMock()
    docker.provision.return_value = ToolProvisionResult(
        tool_name="docker", success=False, error="docker daemon down"
    )

    result = run_setup(
        agent_id="a3",
        manifest=ToolManifest(),
        environment_store=env_store,
        docker_provisioner=docker,
    )

    assert result.success is False
    assert "docker daemon down" in result.error


def test_cleanup_setup_calls_docker_and_env_store(tmp_path: Path) -> None:
    from agent_provisioning_team.phases.setup import cleanup_setup

    docker = MagicMock()
    env_store = MagicMock()

    result = cleanup_setup("a1", environment_store=env_store, docker_provisioner=docker)
    assert result is True
    docker.deprovision.assert_called_once_with("a1")
    env_store.remove.assert_called_once_with("a1")


# ---------------------------------------------------------------------------
# credential_generation phase
# ---------------------------------------------------------------------------


def test_regenerate_credentials_creates_new_object(tmp_path: Path) -> None:
    from agent_provisioning_team.phases.credential_generation import regenerate_credentials
    from agent_provisioning_team.shared.credential_store import CredentialStore

    cred_store = CredentialStore(storage_dir=tmp_path)
    result = regenerate_credentials("agent-x", "postgresql", credential_store=cred_store)
    assert result is not None
    assert result.tool_name == "postgresql"
    assert result.password


def test_regenerate_credentials_for_git_includes_token(tmp_path: Path) -> None:
    from agent_provisioning_team.phases.credential_generation import regenerate_credentials
    from agent_provisioning_team.shared.credential_store import CredentialStore

    cred_store = CredentialStore(storage_dir=tmp_path)
    result = regenerate_credentials("agent-x", "git", credential_store=cred_store)
    assert result.token is not None


def test_get_stored_credentials_returns_empty_when_none(tmp_path: Path) -> None:
    from agent_provisioning_team.phases.credential_generation import get_stored_credentials
    from agent_provisioning_team.shared.credential_store import CredentialStore

    cred_store = CredentialStore(storage_dir=tmp_path)
    out = get_stored_credentials("nobody", credential_store=cred_store)
    assert out == {}


def test_get_stored_credentials_roundtrip(tmp_path: Path) -> None:
    from agent_provisioning_team.phases.credential_generation import (
        get_stored_credentials,
        run_credential_generation,
    )
    from agent_provisioning_team.shared.credential_store import CredentialStore
    from agent_provisioning_team.shared.tool_manifest import ToolDefinition, ToolManifest

    cred_store = CredentialStore(storage_dir=tmp_path)
    manifest = ToolManifest(
        tools=[
            ToolDefinition(
                name="postgresql",
                provisioner="postgres_provisioner",
                config={},
            )
        ]
    )

    result = run_credential_generation(
        agent_id="agent-y",
        manifest=manifest,
        credential_store=cred_store,
    )
    assert result.success is True
    assert "postgresql" in result.credentials

    fetched = get_stored_credentials("agent-y", credential_store=cred_store)
    assert "postgresql" in fetched


def test_run_credential_generation_with_progress_callback(tmp_path: Path) -> None:
    from agent_provisioning_team.phases.credential_generation import run_credential_generation
    from agent_provisioning_team.shared.credential_store import CredentialStore
    from agent_provisioning_team.shared.tool_manifest import ToolDefinition, ToolManifest

    progress_calls = []

    def cb(name, done, total):
        progress_calls.append((name, done, total))

    cred_store = CredentialStore(storage_dir=tmp_path)
    manifest = ToolManifest(
        tools=[
            ToolDefinition(name="pg", provisioner="postgres_provisioner", config={}),
        ]
    )

    result = run_credential_generation(
        agent_id="a",
        manifest=manifest,
        credential_store=cred_store,
        progress_callback=cb,
    )

    assert result.success is True
    # One per-tool call plus the final "complete" sentinel.
    assert any(c[0] == "complete" for c in progress_calls)


# ---------------------------------------------------------------------------
# account_provisioning phase
# ---------------------------------------------------------------------------


def test_run_account_provisioning_no_provisioner_registered() -> None:
    from agent_provisioning_team.phases.account_provisioning import run_account_provisioning
    from agent_provisioning_team.shared.tool_manifest import ToolDefinition, ToolManifest

    manifest = ToolManifest(
        tools=[
            ToolDefinition(name="weird", provisioner="generic_provisioner", config={}),
        ]
    )

    # Use a registry that maps to a different key so the lookup misses.
    fake_other = MagicMock()
    result = run_account_provisioning(
        agent_id="a1",
        manifest=manifest,
        credentials={"weird": GeneratedCredentials(tool_name="weird")},
        provisioners={"some_other_provisioner": fake_other},
    )
    assert result.success is False
    assert "Unknown provisioner" in result.tool_results[0].error


def test_run_account_provisioning_no_credentials() -> None:
    from agent_provisioning_team.phases.account_provisioning import run_account_provisioning
    from agent_provisioning_team.shared.tool_manifest import ToolDefinition, ToolManifest

    fake_prov = MagicMock()
    manifest = ToolManifest(
        tools=[
            ToolDefinition(name="t", provisioner="generic_provisioner", config={}),
        ]
    )

    result = run_account_provisioning(
        agent_id="a1",
        manifest=manifest,
        credentials={},  # no creds for "t"
        provisioners={"generic_provisioner": fake_prov},
    )
    assert result.success is False
    assert "No credentials" in result.tool_results[0].error


def test_run_account_provisioning_handles_provisioner_exception(tmp_path: Path) -> None:
    from agent_provisioning_team.phases.account_provisioning import run_account_provisioning
    from agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_provisioning_team.shared.tool_manifest import ToolDefinition, ToolManifest

    bad_prov = MagicMock()
    bad_prov.provision.side_effect = RuntimeError("boom in provision")

    manifest = ToolManifest(
        tools=[ToolDefinition(name="t", provisioner="generic_provisioner", config={})]
    )

    result = run_account_provisioning(
        agent_id="a1",
        manifest=manifest,
        credentials={"t": GeneratedCredentials(tool_name="t")},
        provisioners={"generic_provisioner": bad_prov},
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
    )

    assert result.success is False
    assert "boom" in result.tool_results[0].error


def test_run_account_provisioning_success(tmp_path: Path) -> None:
    from agent_provisioning_team.phases.account_provisioning import run_account_provisioning
    from agent_provisioning_team.shared.environment_store import (
        EnvironmentInfo as StoreEnvInfo,
    )
    from agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_provisioning_team.shared.tool_manifest import ToolDefinition, ToolManifest

    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    env_store.register(
        StoreEnvInfo(
            agent_id="a1",
            container_id="c1",
            container_name="agent-a1",
            workspace_path="/w",
        )
    )

    good = MagicMock()
    good.provision.return_value = ToolProvisionResult(
        tool_name="t",
        success=True,
        provisioner_key="generic_provisioner",
        permissions=["read"],
    )

    manifest = ToolManifest(
        tools=[ToolDefinition(name="t", provisioner="generic_provisioner", config={})]
    )

    cb_calls = []

    result = run_account_provisioning(
        agent_id="a1",
        manifest=manifest,
        credentials={"t": GeneratedCredentials(tool_name="t")},
        provisioners={"generic_provisioner": good},
        environment_store=env_store,
        progress_callback=lambda done, total, name: cb_calls.append((done, total, name)),
    )

    assert result.success is True
    assert result.tools_completed == 1
    assert any(c[2] == "complete" for c in cb_calls)


def test_deprovision_tools_all() -> None:
    from agent_provisioning_team.models import DeprovisionResult
    from agent_provisioning_team.phases.account_provisioning import deprovision_tools

    p1 = MagicMock()
    p1.deprovision.return_value = DeprovisionResult(tool_name="p1", success=True)
    p2 = MagicMock()
    p2.deprovision.side_effect = RuntimeError("boom")

    results = deprovision_tools("a1", provisioners={"p1": p1, "p2": p2})
    assert results == {"p1": True, "p2": False}


def test_deprovision_tools_filtered() -> None:
    from agent_provisioning_team.models import DeprovisionResult
    from agent_provisioning_team.phases.account_provisioning import deprovision_tools

    p1 = MagicMock()
    p1.deprovision.return_value = DeprovisionResult(tool_name="p1", success=True)
    p2 = MagicMock()
    p2.deprovision.return_value = DeprovisionResult(tool_name="p2", success=True)

    results = deprovision_tools("a1", tool_names=["p1"], provisioners={"p1": p1, "p2": p2})
    assert results == {"p1": True}


def test_build_provisioners_shim() -> None:
    from agent_provisioning_team.phases.account_provisioning import _build_provisioners

    out = _build_provisioners()
    assert isinstance(out, dict)
    assert "docker_provisioner" in out


# ---------------------------------------------------------------------------
# access_audit phase
# ---------------------------------------------------------------------------


def test_run_access_audit_all_success() -> None:
    from agent_provisioning_team.phases.access_audit import run_access_audit

    tool_results = [
        ToolProvisionResult(
            tool_name="t1",
            success=True,
            permissions=["read", "write"],
            provisioner_key="x",
        )
    ]
    msgs = []
    result = run_access_audit(
        agent_id="a1",
        tool_results=tool_results,
        progress_callback=lambda m: msgs.append(m),
    )
    assert result.passed is True
    assert len(result.verifications) == 1
    assert "read" in result.verifications[0].actual_permissions
    assert any("Starting" in m for m in msgs)


def test_run_access_audit_with_failed_tool() -> None:
    from agent_provisioning_team.phases.access_audit import run_access_audit

    tool_results = [
        ToolProvisionResult(tool_name="t1", success=False, error="rpc failed", provisioner_key="x"),
        ToolProvisionResult(
            tool_name="t2", success=True, permissions=["read"], provisioner_key="x"
        ),
    ]
    result = run_access_audit(agent_id="a1", tool_results=tool_results)
    assert result.passed is False
    assert any(not v.passed for v in result.verifications)
    assert any("rpc failed" in v.errors[0] for v in result.verifications if not v.passed)


def test_audit_single_tool_returns_provisioner_result() -> None:
    from agent_provisioning_team.models import AccessVerification
    from agent_provisioning_team.phases.access_audit import audit_single_tool

    prov = MagicMock()
    prov.verify_access.return_value = AccessVerification(
        tool_name="t", passed=True, actual_permissions=["read"]
    )

    v = audit_single_tool("a1", "t", provisioner=prov)
    assert v.passed is True


def test_audit_single_tool_no_provisioner_returns_error() -> None:
    from agent_provisioning_team.phases.access_audit import audit_single_tool

    with patch(
        "agent_provisioning_team.phases.access_audit.build_default_tool_agents",
        return_value={},
    ):
        v = audit_single_tool("a1", "nonexistent")
    assert v.passed is False
    assert "No provisioner" in v.errors[0]


def test_generate_audit_report_includes_status() -> None:
    from agent_provisioning_team.models import AccessVerification
    from agent_provisioning_team.phases.access_audit import generate_audit_report

    audit = AccessAuditResult(
        passed=True,
        verifications=[
            AccessVerification(
                tool_name="t1",
                passed=True,
                actual_permissions=["read"],
                warnings=["wide perms"],
            ),
            AccessVerification(
                tool_name="t2",
                passed=False,
                actual_permissions=[],
                errors=["fail"],
            ),
        ],
        warnings=["overall warning"],
        errors=["overall error"],
    )

    report = generate_audit_report(audit)
    assert "PASSED" in report
    assert "t1" in report and "t2" in report
    assert "wide perms" in report
    assert "fail" in report
    assert "overall warning" in report
    assert "overall error" in report


def test_generate_audit_report_failed_status() -> None:
    from agent_provisioning_team.phases.access_audit import generate_audit_report

    audit = AccessAuditResult(passed=False, verifications=[])
    report = generate_audit_report(audit)
    assert "FAILED" in report


def test_build_provisioners_audit_shim() -> None:
    from agent_provisioning_team.phases.access_audit import _build_provisioners

    out = _build_provisioners()
    assert isinstance(out, dict)


# ---------------------------------------------------------------------------
# documentation phase
# ---------------------------------------------------------------------------


def test_run_documentation_full_path(tmp_path: Path) -> None:
    from agent_provisioning_team.phases.documentation import run_documentation
    from agent_provisioning_team.shared.tool_manifest import ToolDefinition, ToolManifest

    manifest = ToolManifest(
        tools=[
            ToolDefinition(
                name="postgresql",
                provisioner="postgres_provisioner",
                config={},
                onboarding={
                    "description": "PG db",
                    "env_var": "POSTGRES_URL",
                    "getting_started": "Connect to {username}",
                },
            ),
        ]
    )

    creds = GeneratedCredentials(
        tool_name="postgresql",
        username="agent_u",
        password="pw",
        connection_string="postgresql://agent_u:pw@h:5432/db",
    )

    tool_results = [
        ToolProvisionResult(
            tool_name="postgresql",
            success=True,
            permissions=["ALL PRIVILEGES"],
            provisioner_key="postgres_provisioner",
        )
    ]

    msgs = []
    result = run_documentation(
        agent_id="agent-1",
        manifest=manifest,
        credentials={"postgresql": creds},
        tool_results=tool_results,
        workspace_path=str(tmp_path / "ws"),
        progress_callback=lambda m: msgs.append(m),
    )

    assert result.success is True
    assert result.onboarding is not None
    assert any(t.name == "postgresql" for t in result.onboarding.tools)
    assert result.onboarding.environment_variables["POSTGRES_URL"]
    assert result.onboarding.environment_variables["AGENT_ID"] == "agent-1"


def test_run_documentation_skips_failed_tools(tmp_path: Path) -> None:
    from agent_provisioning_team.phases.documentation import run_documentation
    from agent_provisioning_team.shared.tool_manifest import ToolManifest

    manifest = ToolManifest()  # no tools defined

    tool_results = [
        ToolProvisionResult(tool_name="failed", success=False, error="x", provisioner_key="y")
    ]

    result = run_documentation(
        agent_id="a1",
        manifest=manifest,
        credentials={},
        tool_results=tool_results,
        workspace_path=str(tmp_path),
    )

    assert result.success is True
    assert result.onboarding.tools == []


def test_run_documentation_skips_tool_not_in_manifest(tmp_path: Path) -> None:
    from agent_provisioning_team.phases.documentation import run_documentation
    from agent_provisioning_team.shared.tool_manifest import ToolManifest

    manifest = ToolManifest()  # No tools

    tool_results = [
        ToolProvisionResult(tool_name="ghost", success=True, permissions=[], provisioner_key="x")
    ]

    result = run_documentation(
        agent_id="a1",
        manifest=manifest,
        credentials={},
        tool_results=tool_results,
        workspace_path=str(tmp_path),
    )
    assert result.success is True
    # ghost isn't in manifest → no tool docs
    assert result.onboarding.tools == []


def test_run_documentation_uses_default_getting_started_template(tmp_path: Path) -> None:
    """If a tool has no getting_started config, the deterministic fallback runs."""
    from agent_provisioning_team.phases.documentation import run_documentation
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

    creds = GeneratedCredentials(
        tool_name="redis",
        username="u",
        password="p",
        connection_string="redis://u:p@h:6379",
    )

    tool_results = [
        ToolProvisionResult(
            tool_name="redis",
            success=True,
            permissions=["+@all"],
            provisioner_key="redis_provisioner",
        )
    ]

    result = run_documentation(
        agent_id="a1",
        manifest=manifest,
        credentials={"redis": creds},
        tool_results=tool_results,
        workspace_path=str(tmp_path),
    )

    tool_doc = result.onboarding.tools[0]
    # Template fallback mentions the env var
    assert "REDIS_URL" in tool_doc.getting_started


def test_generate_readme_includes_sections() -> None:
    from agent_provisioning_team.phases.documentation import generate_readme

    onboarding = OnboardingPacket(
        summary="hello",
        tools=[
            ToolOnboardingInfo(
                name="pg",
                description="Postgres",
                env_var="POSTGRES_URL",
                getting_started="psql $POSTGRES_URL",
                permissions=["ALL PRIVILEGES"],
            )
        ],
        environment_variables={
            "POSTGRES_URL": "postgresql://u:pw@h/db",
            "AGENT_ID": "a1",
        },
        anatomy_bundle_path="/ws/docs/agent_anatomy",
    )

    out = generate_readme(onboarding)
    assert "# Agent Workspace" in out
    assert "## Available Tools" in out
    assert "POSTGRES_URL" in out
    assert "ALL PRIVILEGES" in out
    assert "agent_anatomy" in out
    # Password content is hidden
    assert "***" not in out  # POSTGRES_URL key doesn't contain "password"


def test_generate_readme_redacts_password_envvar() -> None:
    from agent_provisioning_team.phases.documentation import generate_readme

    onboarding = OnboardingPacket(
        summary="hi",
        tools=[],
        environment_variables={"DB_PASSWORD": "secret123"},
    )
    out = generate_readme(onboarding)
    assert "secret123" not in out
    assert "***" in out


def test_generate_readme_no_anatomy_bundle() -> None:
    from agent_provisioning_team.phases.documentation import generate_readme

    onboarding = OnboardingPacket(summary="s", tools=[], environment_variables={})
    out = generate_readme(onboarding)
    # Falls through to the "when the workspace path is available" message.
    assert "docs/agent_anatomy" in out


# ---------------------------------------------------------------------------
# deliver phase
# ---------------------------------------------------------------------------


def test_run_deliver_updates_status(tmp_path: Path) -> None:
    from agent_provisioning_team.phases.deliver import run_deliver
    from agent_provisioning_team.shared.environment_store import (
        EnvironmentInfo as StoreEnvInfo,
    )
    from agent_provisioning_team.shared.environment_store import EnvironmentStore

    env_store = EnvironmentStore(storage_dir=tmp_path)
    env_store.register(
        StoreEnvInfo(
            agent_id="a1",
            container_id="c1",
            container_name="agent-a1",
            workspace_path="/w",
        )
    )

    env = EnvironmentInfo(container_id="c1", container_name="agent-a1")
    msgs = []
    result = run_deliver(
        agent_id="a1",
        environment=env,
        credentials={},
        tool_results=[],
        access_audit=None,
        onboarding=None,
        environment_store=env_store,
        progress_callback=lambda m: msgs.append(m),
    )

    assert result.success is True
    # status was bumped to "ready"
    env_after = env_store.get("a1")
    assert env_after.status == "ready"


def test_run_deliver_without_environment_does_not_update(tmp_path: Path) -> None:
    from agent_provisioning_team.phases.deliver import run_deliver
    from agent_provisioning_team.shared.environment_store import EnvironmentStore

    env_store = EnvironmentStore(storage_dir=tmp_path)
    result = run_deliver(
        agent_id="a1",
        environment=None,
        credentials={},
        tool_results=[],
        access_audit=None,
        onboarding=None,
        environment_store=env_store,
    )
    assert result.success is True


def test_build_final_result_success() -> None:
    from agent_provisioning_team.phases.deliver import build_final_result

    env = EnvironmentInfo(container_id="c1", container_name="c1")
    tr = [ToolProvisionResult(tool_name="t", success=True, provisioner_key="x")]
    final = build_final_result(
        agent_id="a1",
        environment=env,
        credentials={},
        tool_results=tr,
        access_audit=AccessAuditResult(passed=True, verifications=[]),
        onboarding=None,
        deliver_result=DeliverResult(success=True),
    )
    assert final.success is True
    assert final.current_phase == Phase.DELIVER


def test_build_final_result_with_failures() -> None:
    from agent_provisioning_team.phases.deliver import build_final_result

    tr = [
        ToolProvisionResult(tool_name="t1", success=False, error="boom", provisioner_key="x"),
    ]
    final = build_final_result(
        agent_id="a1",
        environment=None,  # missing env
        credentials={},
        tool_results=tr,
        access_audit=AccessAuditResult(passed=False, verifications=[]),
        onboarding=None,
        deliver_result=DeliverResult(success=True),
    )
    assert final.success is False
    assert "Environment setup failed" in final.error
    assert "t1" in final.error
    assert "Access audit failed" in final.error


def test_redact_connection_string_no_password() -> None:
    from agent_provisioning_team.phases.deliver import _redact_connection_string

    assert _redact_connection_string(None) is None
    assert _redact_connection_string("") is None
    # Already-redacted strings are left untouched (no `:pw@` pattern).
    assert _redact_connection_string("plain") == "plain"


def test_redact_credentials_with_ssh_key() -> None:
    from agent_provisioning_team.phases.deliver import redact_credentials_for_response

    result = ProvisioningResult(
        agent_id="a1",
        success=True,
        credentials={
            "git": GeneratedCredentials(
                tool_name="git",
                ssh_private_key="PRIVATE",
                ssh_public_key="PUBLIC",
                extra={"workspace_path": "/w", "password": "secret"},
            ),
        },
    )
    redacted = redact_credentials_for_response(result)
    assert redacted.credentials["git"].ssh_private_key == "***"
    assert redacted.credentials["git"].ssh_public_key == "PUBLIC"
    assert "password" not in redacted.credentials["git"].extra


def test_redact_details_scalar_passthrough() -> None:
    from agent_provisioning_team.phases.deliver import _redact_details

    assert _redact_details(123) == 123
    assert _redact_details(True) is True
    assert _redact_details(None) is None


def test_redact_details_handles_list() -> None:
    from agent_provisioning_team.phases.deliver import _redact_details

    out = _redact_details([{"password": "x"}, {"safe": "y"}])
    assert out[0]["password"] == "***"
    assert out[1]["safe"] == "y"
