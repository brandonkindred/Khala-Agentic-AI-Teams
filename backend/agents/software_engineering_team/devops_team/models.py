"""Shared models and contracts for the DevOps team orchestration flow."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

RiskLevel = Literal["low", "medium", "high", "critical"]
GateStatus = Literal["pass", "fail", "skipped", "not_run"]


class PlatformScope(BaseModel):
    cloud: str = ""
    runtime: str = ""
    environments: List[str] = Field(default_factory=list)

    @field_validator("environments")
    @classmethod
    def _validate_envs(cls, value: List[str]) -> List[str]:
        envs = [v.strip().lower() for v in value if v and v.strip()]
        seen: List[str] = []
        for env in envs:
            if env not in seen:
                seen.append(env)
        return seen


class RepoContext(BaseModel):
    app_repo: str = ""
    infra_repo: str = ""
    pipeline_repo: str = ""


class TaskGoal(BaseModel):
    summary: str = ""


class TaskScope(BaseModel):
    included: List[str] = Field(default_factory=list)
    excluded: List[str] = Field(default_factory=list)


class IaCConstraints(BaseModel):
    preferred: str = ""


class CicdConstraints(BaseModel):
    platform: str = ""


class DeploymentConstraints(BaseModel):
    strategy: str = ""
    tooling: str = ""


class SecretsConstraints(BaseModel):
    source: str = ""


class ComplianceConstraints(BaseModel):
    require_sbom: bool = False
    image_signing: str = ""


class DevOpsConstraints(BaseModel):
    iac: IaCConstraints = Field(default_factory=IaCConstraints)
    ci_cd: CicdConstraints = Field(default_factory=CicdConstraints)
    deployment: DeploymentConstraints = Field(default_factory=DeploymentConstraints)
    secrets: SecretsConstraints = Field(default_factory=SecretsConstraints)
    compliance: ComplianceConstraints = Field(default_factory=ComplianceConstraints)


class DevOpsTaskSpec(BaseModel):
    """Contract-first task input for DevOps execution."""

    task_id: str
    title: str = ""
    priority: str = "medium"
    platform_scope: PlatformScope = Field(default_factory=PlatformScope)
    repo_context: RepoContext = Field(default_factory=RepoContext)
    goal: TaskGoal = Field(default_factory=TaskGoal)
    scope: TaskScope = Field(default_factory=TaskScope)
    constraints: DevOpsConstraints = Field(default_factory=DevOpsConstraints)
    acceptance_criteria: List[str] = Field(default_factory=list)
    non_functional_requirements: Dict[str, Any] = Field(default_factory=dict)
    dependencies: List[str] = Field(default_factory=list)
    test_requirements: Dict[str, Any] = Field(default_factory=dict)
    risk_flags: List[str] = Field(default_factory=list)
    risk_level: RiskLevel = "medium"
    rollback_requirements: List[str] = Field(default_factory=list)
    security_constraints: List[str] = Field(default_factory=list)
    compliance_constraints: List[str] = Field(default_factory=list)
    change_window: str = ""
    environment: str = "dev"

    @field_validator("task_id")
    @classmethod
    def _validate_task_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task_id is required")
        return value.strip()

    @field_validator("acceptance_criteria")
    @classmethod
    def _normalize_acceptance(cls, value: List[str]) -> List[str]:
        return [v.strip() for v in value if v and v.strip()]

    @field_validator("priority")
    @classmethod
    def _normalize_priority(cls, value: str) -> str:
        aliases = {"p0": "critical", "p1": "high", "p2": "medium", "p3": "low"}
        return aliases.get(value.strip().lower(), value.strip().lower())

    @field_validator("environment")
    @classmethod
    def _normalize_env(cls, value: str) -> str:
        aliases = {"prod": "production", "stg": "staging"}
        return aliases.get(value.strip().lower(), value.strip().lower())

    @field_validator("risk_flags")
    @classmethod
    def _normalize_risk_flags(cls, value: List[str]) -> List[str]:
        return [v.strip() for v in value if v and v.strip()]

    @field_validator("rollback_requirements")
    @classmethod
    def _normalize_rollback(cls, value: List[str]) -> List[str]:
        return [v.strip() for v in value if v and v.strip()]

    @field_validator("security_constraints")
    @classmethod
    def _normalize_security(cls, value: List[str]) -> List[str]:
        return [v.strip() for v in value if v and v.strip()]


class SubtaskContract(BaseModel):
    subtask_id: str
    owner: str
    objective: str
    inputs: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    expected_artifact: List[str] = Field(default_factory=list)
    completion_criteria: List[str] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    finding_id: str
    severity: Literal["critical", "high", "medium", "low", "minor", "nit"] = "medium"
    area: str = ""
    file_ref: str = ""
    issue: str = ""
    rationale: str = ""
    recommended_fix: str = ""
    blocking: bool = False
    exploitability: str = ""


class CriterionTrace(BaseModel):
    criterion: str
    implementation_refs: List[str] = Field(default_factory=list)
    tests: List[Dict[str, str]] = Field(default_factory=list)


class ReleaseReadiness(BaseModel):
    deployment_strategy: str = ""
    rollback_available: bool = False
    alerting_configured: bool = False
    required_approvals: List[str] = Field(default_factory=list)
    runtime_verification_checklist: List[str] = Field(default_factory=list)


class GitCommitMetadata(BaseModel):
    hash: str = ""
    message: str = ""


class GitMergeMetadata(BaseModel):
    target_branch: str = "development"
    strategy: str = "squash"
    merge_commit_hash: str = ""
    status: str = ""


class GitOperationsMetadata(BaseModel):
    branch_created: str = ""
    commits: List[GitCommitMetadata] = Field(default_factory=list)
    merge: Optional[GitMergeMetadata] = None


class HandoffInfo(BaseModel):
    prod_approval_required: bool = True
    runbook_updated: bool = False


class DevOpsCompletionPackage(BaseModel):
    task_id: str
    status: Literal["completed", "failed", "blocked"] = "failed"
    files_changed: List[str] = Field(default_factory=list)
    acceptance_criteria_trace: List[CriterionTrace] = Field(default_factory=list)
    quality_gates: Dict[str, GateStatus] = Field(default_factory=dict)
    release_readiness: ReleaseReadiness = Field(default_factory=ReleaseReadiness)
    notes: List[str] = Field(default_factory=list)
    risks_remaining: List[str] = Field(default_factory=list)
    git_operations: GitOperationsMetadata = Field(default_factory=GitOperationsMetadata)
    handoff: HandoffInfo = Field(default_factory=HandoffInfo)


class DevOpsTeamResult(BaseModel):
    success: bool
    failure_reason: str = ""
    iterations: int = 1
    completion_package: Optional[DevOpsCompletionPackage] = None
