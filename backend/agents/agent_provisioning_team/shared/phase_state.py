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
    tool_names: List[str] = []
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
    """Reconstruct a ``SetupSnapshot`` from a persisted setup-phase result.

    Preconditions:
        * ``raw`` is a mapping whose fields conform to ``SetupSnapshot`` (the
          shape produced by the setup phase on a prior run).
    Postconditions:
        * Returns a validated ``SetupSnapshot``.
        * Raises ``pydantic.ValidationError`` when ``raw`` does not conform — no
          silent coercion.
    """
    return SetupSnapshot.model_validate(raw)


def restore_credentials(raw: Dict[str, Any]) -> CredentialGenerationSnapshot:
    """Reconstruct a ``CredentialGenerationSnapshot`` from a persisted result.

    Preconditions:
        * ``raw`` is a mapping whose fields conform to
          ``CredentialGenerationSnapshot`` (the shape produced by the credential
          generation phase on a prior run).
    Postconditions:
        * Returns a validated ``CredentialGenerationSnapshot``.
        * Raises ``pydantic.ValidationError`` when ``raw`` does not conform — no
          silent coercion.
    """
    return CredentialGenerationSnapshot.model_validate(raw)


def restore_account_provisioning(raw: Dict[str, Any]) -> AccountProvisioningSnapshot:
    """Reconstruct an ``AccountProvisioningSnapshot`` from a persisted result.

    Preconditions:
        * ``raw`` is a mapping whose fields conform to
          ``AccountProvisioningSnapshot`` (the shape produced by the account
          provisioning phase on a prior run).
    Postconditions:
        * Returns a validated ``AccountProvisioningSnapshot``.
        * Raises ``pydantic.ValidationError`` when ``raw`` does not conform — no
          silent coercion.
    """
    return AccountProvisioningSnapshot.model_validate(raw)


def restore_access_audit(raw: Dict[str, Any]) -> AccessAuditResult:
    """Reconstruct an ``AccessAuditResult`` from a persisted result.

    Preconditions:
        * ``raw`` is a mapping whose fields conform to ``AccessAuditResult`` (the
          shape produced by the access audit phase on a prior run).
    Postconditions:
        * Returns a validated ``AccessAuditResult``.
        * Raises ``pydantic.ValidationError`` when ``raw`` does not conform — no
          silent coercion.
    """
    return AccessAuditResult.model_validate(raw)


def restore_documentation(raw: Dict[str, Any]) -> DocumentationSnapshot:
    """Reconstruct a ``DocumentationSnapshot`` from a persisted result.

    Preconditions:
        * ``raw`` is a mapping whose fields conform to ``DocumentationSnapshot``
          (the shape produced by the documentation phase on a prior run).
    Postconditions:
        * Returns a validated ``DocumentationSnapshot``.
        * Raises ``pydantic.ValidationError`` when ``raw`` does not conform — no
          silent coercion.
    """
    return DocumentationSnapshot.model_validate(raw)
