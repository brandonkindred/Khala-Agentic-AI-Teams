"""
Typed reconstruction helpers for resumed provisioning workflows.

Replaces the unsafe `type("R", (), prior_results["setup"])()` pattern in
`orchestrator.py` with proper Pydantic-backed snapshots that validate
field shapes when a workflow is resumed after a crash.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

from ..models import (
    AccessAuditResult,
    EnvironmentInfo,
    GeneratedCredentials,
    OnboardingPacket,
    ToolProvisionResult,
)


class _Snapshot(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)


class SetupSnapshot(_Snapshot):
    success: bool
    environment: Optional[EnvironmentInfo] = None
    error: Optional[str] = None


class CredentialGenerationSnapshot(_Snapshot):
    success: bool
    credentials: Dict[str, GeneratedCredentials] = {}
    error: Optional[str] = None


class AccountProvisioningSnapshot(_Snapshot):
    success: bool
    tool_results: List[ToolProvisionResult] = []
    tools_completed: int = 0
    tools_total: int = 0
    error: Optional[str] = None


class DocumentationSnapshot(_Snapshot):
    success: bool
    onboarding: Optional[OnboardingPacket] = None


def restore_setup(raw: Dict[str, Any]) -> SetupSnapshot:
    return SetupSnapshot.model_validate(raw)


def restore_credentials(raw: Dict[str, Any]) -> CredentialGenerationSnapshot:
    return CredentialGenerationSnapshot.model_validate(raw)


def restore_account_provisioning(raw: Dict[str, Any]) -> AccountProvisioningSnapshot:
    return AccountProvisioningSnapshot.model_validate(raw)


def restore_access_audit(raw: Dict[str, Any]) -> AccessAuditResult:
    return AccessAuditResult.model_validate(raw)


def restore_documentation(raw: Dict[str, Any]) -> DocumentationSnapshot:
    return DocumentationSnapshot.model_validate(raw)
