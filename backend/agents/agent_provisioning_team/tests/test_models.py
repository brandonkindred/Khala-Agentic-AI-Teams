"""Tests for agent_provisioning_team models."""


from agent_provisioning_team.models import (
    AccessTier,
    DeprovisionRequest,
    DeprovisionResponse,
    EnvironmentInfo,
    GeneratedCredentials,
    ManifestConfig,
    Phase,
    ProvisioningResult,
    ProvisionJobResponse,
    ProvisionJobsListResponse,
    ProvisionRequest,
    ToolConfig,
    ToolProvisionResult,
)


def test_phase_enum_values():
    assert Phase.SETUP == "setup"
    assert Phase.CREDENTIAL_GENERATION == "credential_generation"
    assert Phase.ACCOUNT_PROVISIONING == "account_provisioning"
    assert Phase.ACCESS_AUDIT == "access_audit"
    assert Phase.DOCUMENTATION == "documentation"
    assert Phase.DELIVER == "deliver"


def test_access_tier_enum_ordering():
    tiers = [AccessTier.MINIMAL, AccessTier.STANDARD, AccessTier.ELEVATED, AccessTier.FULL]
    assert len(tiers) == 4
    assert AccessTier.MINIMAL == "minimal"
    assert AccessTier.FULL == "full"


def test_tool_config_defaults():
    tc = ToolConfig(name="postgresql", provisioner="postgres_provisioner")
    assert tc.name == "postgresql"
    assert tc.access_level == "standard"
    assert tc.config == {}
    assert tc.onboarding == {}


def test_manifest_config_with_tools():
    tc = ToolConfig(name="git", provisioner="git_provisioner")
    mc = ManifestConfig(tools=[tc])
    assert len(mc.tools) == 1
    assert mc.tools[0].name == "git"
    assert mc.version == "1.0"
    assert mc.base_image == "python:3.11-slim"


def test_generated_credentials_optional_fields():
    creds = GeneratedCredentials(tool_name="redis")
    assert creds.tool_name == "redis"
    assert creds.username is None
    assert creds.password is None
    assert creds.token is None
    assert creds.ssh_private_key is None
    assert creds.extra == {}


def test_generated_credentials_with_values():
    creds = GeneratedCredentials(
        tool_name="postgres",
        username="agent_user",
        password="s3cr3t",
        connection_string="postgresql://localhost:5432/agentdb",
    )
    assert creds.username == "agent_user"
    assert creds.connection_string is not None


def test_environment_info():
    env = EnvironmentInfo(container_id="abc123", container_name="agent-container")
    assert env.container_id == "abc123"
    assert env.ssh_host == "localhost"
    assert env.ssh_port == 22
    assert env.status == "running"


def test_tool_provision_result_success():
    result = ToolProvisionResult(tool_name="postgres", success=True)
    assert result.success is True
    assert result.error is None
    assert result.permissions == []


def test_tool_provision_result_failure():
    result = ToolProvisionResult(tool_name="redis", success=False, error="Connection refused")
    assert result.success is False
    assert result.error == "Connection refused"


def test_provisioning_result_defaults():
    result = ProvisioningResult(agent_id="agent-001")
    assert result.agent_id == "agent-001"
    assert result.current_phase == Phase.SETUP
    assert result.completed_phases == []
    assert result.success is False
    assert result.error is None


def test_provision_request_defaults():
    req = ProvisionRequest(agent_id="agent-002")
    assert req.agent_id == "agent-002"
    assert req.manifest_path == "default.yaml"
    assert req.access_tier == AccessTier.STANDARD


def test_provision_request_custom_tier():
    req = ProvisionRequest(agent_id="agent-003", access_tier=AccessTier.ELEVATED)
    assert req.access_tier == AccessTier.ELEVATED


def test_provision_job_response():
    resp = ProvisionJobResponse(job_id="job-001", status="running", message="started")
    assert resp.job_id == "job-001"
    assert resp.status == "running"


def test_provision_jobs_list_response_empty():
    resp = ProvisionJobsListResponse()
    assert resp.jobs == []


def test_deprovision_request():
    req = DeprovisionRequest(agent_id="agent-to-remove")
    assert req.agent_id == "agent-to-remove"
    assert req.force is False


def test_deprovision_response():
    resp = DeprovisionResponse(agent_id="agent-001", success=True)
    assert resp.success is True
    assert resp.details == {}
    assert resp.error is None
