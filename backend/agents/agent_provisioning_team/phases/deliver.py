"""
Deliver phase: Finalize provisioning and prepare the result.

This is phase 6 (final) of the provisioning workflow.
"""

from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from ..models import (
    AccessAuditResult,
    DeliverResult,
    EnvironmentInfo,
    GeneratedCredentials,
    OnboardingPacket,
    ProvisioningResult,
    ToolProvisionResult,
)
from ..shared.environment_store import EnvironmentStore


def run_deliver(
    agent_id: str,
    environment: Optional[EnvironmentInfo],
    credentials: Dict[str, GeneratedCredentials],
    tool_results: List[ToolProvisionResult],
    access_audit: Optional[AccessAuditResult],
    onboarding: Optional[OnboardingPacket],
    environment_store: Optional[EnvironmentStore] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> DeliverResult:
    """
    Execute the deliver phase.

    Finalizes the provisioning by:
    1. Updating environment status to 'ready'
    2. Recording final provisioning state
    3. Preparing the delivery timestamp

    Args:
        agent_id: Unique identifier for the agent
        environment: Environment info from setup phase
        credentials: Generated credentials
        tool_results: Results from account provisioning
        access_audit: Results from access audit
        onboarding: Onboarding documentation
        environment_store: Store for updating environment
        progress_callback: Callback for progress updates

    Returns:
        DeliverResult indicating success
    """
    env_store = environment_store or EnvironmentStore()

    if progress_callback:
        progress_callback("Finalizing provisioning...")

    if environment:
        env_store.update_status(agent_id, "ready")

    if progress_callback:
        progress_callback("Provisioning complete")

    return DeliverResult(
        success=True,
        finalized_at=datetime.now(timezone.utc),
    )


def build_final_result(
    agent_id: str,
    environment: Optional[EnvironmentInfo],
    credentials: Dict[str, GeneratedCredentials],
    tool_results: List[ToolProvisionResult],
    access_audit: Optional[AccessAuditResult],
    onboarding: Optional[OnboardingPacket],
    deliver_result: DeliverResult,
) -> ProvisioningResult:
    """
    Build the final provisioning result from all phase outputs.

    Args:
        agent_id: Agent identifier
        environment: Environment from setup
        credentials: Generated credentials
        tool_results: Tool provisioning results
        access_audit: Access audit results
        onboarding: Onboarding documentation
        deliver_result: Deliver phase result

    Returns:
        Complete ProvisioningResult
    """
    from ..models import Phase

    all_success = (
        (environment is not None)
        and all(r.success for r in tool_results)
        and (access_audit is None or access_audit.passed)
        and deliver_result.success
    )

    error = None
    if not all_success:
        errors: List[str] = []
        if environment is None:
            errors.append("Environment setup failed")
        failed_tools = [r.tool_name for r in tool_results if not r.success]
        if failed_tools:
            errors.append(f"Tools failed: {', '.join(failed_tools)}")
        if access_audit and not access_audit.passed:
            errors.append("Access audit failed")
        error = "; ".join(errors) if errors else None

    return ProvisioningResult(
        agent_id=agent_id,
        current_phase=Phase.DELIVER,
        completed_phases=[
            Phase.SETUP,
            Phase.CREDENTIAL_GENERATION,
            Phase.ACCOUNT_PROVISIONING,
            Phase.ACCESS_AUDIT,
            Phase.DOCUMENTATION,
            Phase.DELIVER,
        ],
        environment=environment,
        credentials=credentials,
        tool_results=tool_results,
        access_audit=access_audit,
        onboarding=onboarding,
        success=all_success,
        error=error,
    )


def redact_credentials_for_response(
    result: ProvisioningResult,
) -> ProvisioningResult:
    """
    Create a copy of the result with sensitive credentials redacted.

    Passwords and tokens are replaced with '***' for safe API responses.

    Args:
        result: Original provisioning result

    Returns:
        Copy with redacted credentials
    """
    redacted_creds: Dict[str, GeneratedCredentials] = {}

    for tool_name, cred in result.credentials.items():
        redacted_creds[tool_name] = GeneratedCredentials(
            tool_name=cred.tool_name,
            username=cred.username,
            password="***" if cred.password else None,
            token="***" if cred.token else None,
            ssh_private_key="***" if cred.ssh_private_key else None,
            ssh_public_key=cred.ssh_public_key,
            connection_string=_redact_connection_string(cred.connection_string),
            extra={k: v for k, v in cred.extra.items() if "password" not in k.lower()},
        )

    redacted_tool_results = [
        ToolProvisionResult(
            tool_name=tr.tool_name,
            success=tr.success,
            credentials=None,  # already represented in redacted_creds above
            permissions=tr.permissions,
            details=_redact_details(tr.details) if tr.details else {},
            error=tr.error,
        )
        for tr in result.tool_results
    ]

    return ProvisioningResult(
        agent_id=result.agent_id,
        current_phase=result.current_phase,
        completed_phases=result.completed_phases,
        environment=result.environment,
        credentials=redacted_creds,
        tool_results=redacted_tool_results,
        access_audit=result.access_audit,
        onboarding=result.onboarding,
        success=result.success,
        error=result.error,
    )


# Field substrings that should never round-trip back to API consumers in
# clear text. Matched case-insensitively against dict keys at any depth.
_SENSITIVE_KEY_SUBSTRINGS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "privatekey",
    "connection_string",
    "conn_str",
    "dsn",
)


def _redact_details(value):
    """Recursively redact sensitive fields from a tool result `details` blob."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key_l = str(k).lower()
            if any(s in key_l for s in _SENSITIVE_KEY_SUBSTRINGS):
                out[k] = "***"
            else:
                out[k] = _redact_details(v)
        return out
    if isinstance(value, list):
        return [_redact_details(v) for v in value]
    if isinstance(value, str):
        return _redact_connection_string(value) if "://" in value and "@" in value else value
    return value


def _redact_connection_string(conn_str: Optional[str]) -> Optional[str]:
    """Redact password from a connection string."""
    if not conn_str:
        return None

    import re

    return re.sub(r":([^:@]+)@", ":***@", conn_str)
