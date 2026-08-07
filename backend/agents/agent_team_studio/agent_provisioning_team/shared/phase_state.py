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
    """Reconstruct a validated setup-phase snapshot from a serialized result.

    Preconditions:
        * ``raw`` is a mapping matching the serialized (``model_dump``) shape of a
          completed setup phase — at minimum a boolean ``success`` field, with an
          optional ``environment`` (``EnvironmentInfo``) and ``error``.
    Postconditions:
        * Returns a validated ``SetupSnapshot``.
        * Raises ``pydantic.ValidationError`` when ``raw`` does not match the
          snapshot shape; the error is not swallowed or coerced.
    """
    return SetupSnapshot.model_validate(raw)


def restore_credentials(raw: Dict[str, Any]) -> CredentialGenerationSnapshot:
    """Reconstruct a validated credential-generation snapshot from a serialized result.

    Preconditions:
        * ``raw`` is a mapping matching the serialized (``model_dump``) shape of a
          completed credential-generation phase — a boolean ``success`` field, plus
          optional ``credentials`` (``{tool_name: GeneratedCredentials}``),
          ``tool_names``, and ``error``.
    Postconditions:
        * Returns a validated ``CredentialGenerationSnapshot``.
        * Raises ``pydantic.ValidationError`` when ``raw`` does not match the
          snapshot shape; the error is not swallowed or coerced.
    """
    return CredentialGenerationSnapshot.model_validate(raw)


def restore_account_provisioning(raw: Dict[str, Any]) -> AccountProvisioningSnapshot:
    """Reconstruct a validated account-provisioning snapshot from a serialized result.

    Preconditions:
        * ``raw`` is a mapping matching the serialized (``model_dump``) shape of a
          completed account-provisioning phase — a boolean ``success`` field, plus
          optional ``tool_results`` (``list[ToolProvisionResult]``),
          ``tools_completed``, ``tools_total``, and ``error``.
    Postconditions:
        * Returns a validated ``AccountProvisioningSnapshot``.
        * Raises ``pydantic.ValidationError`` when ``raw`` does not match the
          snapshot shape; the error is not swallowed or coerced.
    """
    return AccountProvisioningSnapshot.model_validate(raw)


def restore_access_audit(raw: Dict[str, Any]) -> AccessAuditResult:
    """Reconstruct a validated access-audit result from a serialized result.

    Unlike the sibling helpers, this one returns ``AccessAuditResult`` directly
    rather than a local ``*Snapshot`` wrapper — and that asymmetry is deliberate.
    The sibling snapshots exist because their phases emit *ad-hoc* result objects
    whose combined field shape matches no single model (they replace the unsafe
    ``type("R", (), prior_results[...])()`` pattern described in the module
    docstring), so a validated Pydantic wrapper has to be introduced. The audit
    phase already produces a first-class ``AccessAuditResult`` model
    (``run_access_audit``), so restoring it is just re-validating that same
    model; wrapping it in a snapshot would add redundant indirection. Callers —
    the Temporal ``audit_activity`` and ``ProvisioningResult.access_audit`` —
    consume the ``AccessAuditResult`` shape directly, so returning it unwrapped
    keeps that contract intact.

    Preconditions:
        * ``raw`` is a mapping matching the serialized (``model_dump``) shape of a
          completed access-audit phase — a boolean ``passed`` field, plus optional
          ``verifications``, ``warnings``, and ``errors``.
    Postconditions:
        * Returns a validated ``AccessAuditResult``.
        * Raises ``pydantic.ValidationError`` when ``raw`` does not match the
          expected shape; the error is not swallowed or coerced.
    """
    return AccessAuditResult.model_validate(raw)


def restore_documentation(raw: Dict[str, Any]) -> DocumentationSnapshot:
    """Reconstruct a validated documentation snapshot from a serialized result.

    Preconditions:
        * ``raw`` is a mapping matching the serialized (``model_dump``) shape of a
          completed documentation phase — a boolean ``success`` field and an
          optional ``onboarding`` (``OnboardingPacket`` or ``None``).
    Postconditions:
        * Returns a validated ``DocumentationSnapshot``.
        * Raises ``pydantic.ValidationError`` when ``raw`` does not match the
          snapshot shape; the error is not swallowed or coerced.
    """
    return DocumentationSnapshot.model_validate(raw)
