"""
Domain models for the Agent Provisioning Team.

Defines phases, request/response models, and result types for the
provisioning workflow. There is no permission tiering: every sandbox is
provisioned with full access on every backing service.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from agent_team_studio.agent_provisioning_team.shared.path_safety import safe_path_component


class Phase(str, Enum):
    """Lifecycle phases of the provisioning workflow."""

    SETUP = "setup"
    CREDENTIAL_GENERATION = "credential_generation"
    ACCOUNT_PROVISIONING = "account_provisioning"
    ACCESS_AUDIT = "access_audit"
    DOCUMENTATION = "documentation"
    DELIVER = "deliver"


class ToolConfig(BaseModel):
    """Configuration for a single tool from the manifest."""

    name: str = Field(..., description="Tool name (e.g., postgresql, git)")
    provisioner: str = Field(..., description="Name of the provisioner to use")
    config: Dict[str, Any] = Field(default_factory=dict, description="Tool-specific config")
    onboarding: Dict[str, Any] = Field(default_factory=dict, description="Onboarding documentation")


class ManifestConfig(BaseModel):
    """Parsed tool manifest configuration."""

    version: str = Field(default="1.0", description="Manifest version")
    base_image: str = Field(default="python:3.11-slim", description="Docker base image")
    tools: List[ToolConfig] = Field(default_factory=list, description="Tools to provision")


class GeneratedCredentials(BaseModel):
    """Credentials generated for a single tool."""

    tool_name: str
    username: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    ssh_private_key: Optional[str] = None
    ssh_public_key: Optional[str] = None
    connection_string: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)


class EnvironmentInfo(BaseModel):
    """Information about the provisioned Docker environment."""

    container_id: str
    container_name: str
    ssh_host: str = Field(default="localhost")
    ssh_port: int = Field(default=22)
    workspace_path: str = Field(default="/workspace")
    status: str = Field(default="running")
    # True iff run_setup reused an existing container (its own already-running
    # fast path, or docker.provision()'s idempotent reuse) rather than
    # creating one fresh. Lets the workflow tell, from setup's own confirmed
    # outcome rather than a pre-setup guess, when nothing could possibly have
    # predated this run's environment (reused=False is unambiguous — a
    # container this call just created cannot also predate it). reused=True
    # is NOT symmetric evidence of a genuinely pre-existing environment: it
    # can also reflect this same run's own earlier (response-lost) attempt at
    # this same activity being read back as "already there" on retry, so
    # callers must not treat it as proof of pre-existing ownership. Defaults
    # to True (the conservative, "don't know, assume reused" reading) so a
    # dump reconstructed from data written before this field existed can't
    # silently read as an explicit False — both run_setup call sites always
    # set this explicitly, so the default only ever applies to that
    # backward-compatibility gap.
    reused: bool = Field(default=True)


class ToolProvisionResult(BaseModel):
    """Result of provisioning a single tool."""

    tool_name: str
    success: bool
    credentials: Optional[GeneratedCredentials] = None
    permissions: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
    # Registry key of the provisioner that produced this result (e.g.
    # "postgres_provisioner"). Used by ProvisioningOrchestrator.compensate()
    # to look the provisioner back up for rollback. Optional/None for
    # backward compatibility with results serialized before #293.
    provisioner_key: Optional[str] = None


class AccessVerification(BaseModel):
    """Result of verifying access for a tool."""

    tool_name: str
    passed: bool
    actual_permissions: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


class DeprovisionResult(BaseModel):
    """Result of deprovisioning a tool or environment."""

    tool_name: Optional[str] = None
    success: bool
    details: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class SetupResult(BaseModel):
    """Result of the setup phase (Docker container creation)."""

    success: bool
    environment: Optional[EnvironmentInfo] = None
    error: Optional[str] = None


class CredentialGenerationResult(BaseModel):
    """Result of the credential generation phase."""

    success: bool
    credentials: Dict[str, GeneratedCredentials] = Field(default_factory=dict)
    error: Optional[str] = None


class AccountProvisioningResult(BaseModel):
    """Result of the account provisioning phase."""

    success: bool
    tool_results: List[ToolProvisionResult] = Field(default_factory=list)
    tools_completed: int = 0
    tools_total: int = 0
    error: Optional[str] = None


class AccessAuditResult(BaseModel):
    """Result of the access audit phase."""

    passed: bool
    verifications: List[AccessVerification] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


class ToolOnboardingInfo(BaseModel):
    """Onboarding information for a single tool."""

    name: str
    description: str
    env_var: Optional[str] = None
    getting_started: str
    permissions: List[str] = Field(default_factory=list)


class OnboardingPacket(BaseModel):
    """Complete onboarding documentation for the agent."""

    summary: str
    tools: List[ToolOnboardingInfo] = Field(default_factory=list)
    environment_variables: Dict[str, str] = Field(default_factory=dict)
    anatomy_bundle_path: Optional[str] = Field(
        default=None,
        description="Host path to docs/agent_anatomy/ with AGENT_ANATOMY.md and design PNGs when materialized",
    )


class DocumentationResult(BaseModel):
    """Result of the documentation phase."""

    success: bool
    onboarding: Optional[OnboardingPacket] = None
    error: Optional[str] = None


class DeliverResult(BaseModel):
    """Result of the deliver phase."""

    success: bool
    finalized_at: Optional[datetime] = None
    error: Optional[str] = None


class ProvisioningResult(BaseModel):
    """Complete result of the provisioning workflow."""

    agent_id: str
    current_phase: Phase = Phase.SETUP
    completed_phases: List[Phase] = Field(default_factory=list)
    environment: Optional[EnvironmentInfo] = None
    credentials: Dict[str, GeneratedCredentials] = Field(default_factory=dict)
    tool_results: List[ToolProvisionResult] = Field(default_factory=list)
    access_audit: Optional[AccessAuditResult] = None
    onboarding: Optional[OnboardingPacket] = None
    success: bool = False
    error: Optional[str] = None


class ProvisionRequest(BaseModel):
    """Request to provision a new agent environment."""

    agent_id: str = Field(..., description="Unique identifier for the agent")
    manifest_path: str = Field(
        default="default.yaml",
        description="Path to the tool manifest (relative to manifests/)",
    )
    workspace_path: Optional[str] = Field(
        default=None,
        description="Custom workspace path inside the container",
    )

    @field_validator("agent_id")
    @classmethod
    def _agent_id_is_safe(cls, v: str) -> str:
        """Reject an agent_id that would escape the on-disk stores.

        The value is keyed into ``storage_dir / f"{agent_id}.<ext>"`` by the
        environment and credential stores, so a raised ``ValueError`` here turns
        a traversal attempt into a clean 422 at the HTTP edge instead of a 500.
        """
        return safe_path_component(v, kind="agent_id")


class ProvisionJobResponse(BaseModel):
    """Response when starting a provisioning job."""

    job_id: str
    status: str
    message: str


class ProvisionStatusResponse(BaseModel):
    """Response for job status queries."""

    job_id: str
    status: str
    agent_id: Optional[str] = None
    current_phase: Optional[str] = None
    current_tool: Optional[str] = None
    progress: int = 0
    tools_completed: int = 0
    tools_total: int = 0
    completed_phases: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    result: Optional[ProvisioningResult] = None


class ProvisionJobSummary(BaseModel):
    """Summary of a provisioning job for listing."""

    job_id: str
    agent_id: str
    status: str
    created_at: Optional[str] = None
    current_phase: Optional[str] = None
    progress: int = 0


class ProvisionJobsListResponse(BaseModel):
    """Response for listing provisioning jobs."""

    jobs: List[ProvisionJobSummary] = Field(default_factory=list)


class DeprovisionRequest(BaseModel):
    """Request to deprovision an agent."""

    agent_id: str = Field(..., description="Agent ID to deprovision")
    force: bool = Field(default=False, description="Force removal even if errors occur")

    @field_validator("agent_id")
    @classmethod
    def _agent_id_is_safe(cls, v: str) -> str:
        """Reject an agent_id that would escape the on-disk stores (path traversal)."""
        return safe_path_component(v, kind="agent_id")


class DeprovisionResponse(BaseModel):
    """Response for deprovisioning an agent."""

    agent_id: str
    success: bool
    details: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class DeprovisionCancelledError(Exception):
    """Raised when a cancellation checkpoint fires mid-``deprovision``.

    Signals that ``ProvisioningOrchestrator.deprovision`` stopped before
    completing its per-tool teardown sequence, so the caller (a Temporal
    activity wrapper) can distinguish an interrupted run from a normal
    completed one by exception type rather than by inspecting a response
    payload.
    """

    def __init__(self, agent_id: str, completed: Dict[str, Any]) -> None:
        self.agent_id = agent_id
        self.completed = completed
        super().__init__(f"Deprovision for {agent_id} cancelled mid-teardown")
