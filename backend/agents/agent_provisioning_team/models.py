"""
Domain models for the Agent Provisioning Team.

Defines phases, access tiers, request/response models, and result types
for the provisioning workflow.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Phase(str, Enum):
    """Lifecycle phases of the provisioning workflow."""

    SETUP = "setup"
    CREDENTIAL_GENERATION = "credential_generation"
    ACCOUNT_PROVISIONING = "account_provisioning"
    ACCESS_AUDIT = "access_audit"
    DOCUMENTATION = "documentation"
    DELIVER = "deliver"


class AccessTier(str, Enum):
    """Access permission tiers following least-privilege principle."""

    MINIMAL = "minimal"
    STANDARD = "standard"
    ELEVATED = "elevated"
    FULL = "full"


class ToolConfig(BaseModel):
    """Configuration for a single tool from the manifest."""

    name: str = Field(..., description="Tool name (e.g., postgresql, git)")
    provisioner: str = Field(..., description="Name of the provisioner to use")
    access_level: str = Field(default="standard", description="Access level for this tool")
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


class ToolProvisionResult(BaseModel):
    """Result of provisioning a single tool."""

    tool_name: str
    success: bool
    credentials: Optional[GeneratedCredentials] = None
    permissions: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class AccessVerification(BaseModel):
    """Result of verifying access for a tool."""

    tool_name: str
    passed: bool
    expected_tier: str
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
    tier_requested: str
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
    access_tier: str
    environment_variables: Dict[str, str] = Field(default_factory=dict)


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
    access_tier: AccessTier = Field(
        default=AccessTier.STANDARD,
        description="Requested access tier",
    )
    workspace_path: Optional[str] = Field(
        default=None,
        description="Custom workspace path inside the container",
    )


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


class DeprovisionResponse(BaseModel):
    """Response for deprovisioning an agent."""

    agent_id: str
    success: bool
    details: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
