"""
Access audit phase: Surface the permissions provisioned for each tool.

Phase 4 of the provisioning workflow. The historical least-privilege /
tier-validation logic was removed when the tier system was dropped (#456):
every sandbox is now provisioned with full access, so there is no expected
tier to validate against. The phase still runs so callers continue to see
the per-tool ``AccessVerification`` shape (and any provisioner errors that
showed up during account provisioning), but it no longer fails on
"over-permissioned" results — over-permissioning is the design intent now.
"""

from typing import Callable, List, Optional

from ..models import (
    AccessAuditResult,
    AccessVerification,
    ToolProvisionResult,
)
from ..shared.tool_agent_registry import build_default_tool_agents
from ..tool_agents.base import ToolProvisionerInterface


def run_access_audit(
    agent_id: str,
    tool_results: List[ToolProvisionResult],
    progress_callback: Optional[Callable[[str], None]] = None,
) -> AccessAuditResult:
    """Audit provisioned access for each tool.

    Preconditions:
        * ``agent_id`` is non-empty.
        * ``tool_results`` entries are ``ToolProvisionResult`` instances.
    Postconditions:
        * Returns an ``AccessAuditResult`` whose ``passed`` is True iff every
          tool succeeded during account provisioning.
        * Failures during provisioning surface as per-tool errors;
          permission grants are recorded as-is (not validated against a tier).

    Args:
        agent_id: Unique identifier for the agent
        tool_results: Results from account provisioning phase
        progress_callback: Callback for progress updates
    """
    assert agent_id, "agent_id must be non-empty"

    verifications: List[AccessVerification] = []
    all_warnings: List[str] = []
    all_errors: List[str] = []

    if progress_callback:
        progress_callback("Starting access audit...")

    for result in tool_results:
        if not result.success:
            verifications.append(
                AccessVerification(
                    tool_name=result.tool_name,
                    passed=False,
                    actual_permissions=[],
                    errors=[f"Tool provisioning failed: {result.error}"],
                )
            )
            all_errors.append(f"{result.tool_name}: provisioning failed")
            continue

        if progress_callback:
            progress_callback(f"Auditing {result.tool_name}...")

        verifications.append(
            AccessVerification(
                tool_name=result.tool_name,
                passed=True,
                actual_permissions=result.permissions,
            )
        )

    if progress_callback:
        progress_callback("Access audit complete")

    overall_passed = all(v.passed for v in verifications)

    return AccessAuditResult(
        passed=overall_passed,
        verifications=verifications,
        warnings=all_warnings,
        errors=all_errors,
    )


def audit_single_tool(
    agent_id: str,
    tool_name: str,
    provisioner: Optional[ToolProvisionerInterface] = None,
    provisioner_key: Optional[str] = None,
) -> AccessVerification:
    """Re-verify a single tool by delegating to its provisioner.

    Preconditions:
        * ``agent_id`` and ``tool_name`` are non-empty.
        * When given, ``provisioner_key`` is the tool's registry key — the
          same value stamped onto ``ToolProvisionResult.provisioner_key`` by
          ``run_account_provisioning`` (``tool.provisioner`` from the
          manifest, e.g. ``"postgres_provisioner"``) and consumed by
          ``ProvisioningOrchestrator.compensate()``. It is NOT derived from
          ``tool_name`` — a tool's manifest name and its provisioner
          registry key are independent (e.g. tool_name "postgresql" maps to
          registry key "postgres_provisioner").
    Postconditions:
        * Returns the resolved provisioner's ``verify_access(agent_id)``
          result, or a failed ``AccessVerification`` naming ``tool_name``
          when no provisioner could be resolved.
    """
    assert agent_id, "agent_id must be non-empty"
    assert tool_name, "tool_name must be non-empty"

    prov = provisioner
    if prov is None:
        provs = build_default_tool_agents()
        key = provisioner_key or f"{tool_name}_provisioner"
        prov = provs.get(key)

    if prov is None:
        return AccessVerification(
            tool_name=tool_name,
            passed=False,
            actual_permissions=[],
            errors=[f"No provisioner found for {tool_name}"],
        )

    return prov.verify_access(agent_id)


def generate_audit_report(audit_result: AccessAuditResult) -> str:
    """Generate a human-readable audit report."""
    lines = [
        "# Access Audit Report",
        "",
        f"**Overall Status:** {'PASSED' if audit_result.passed else 'FAILED'}",
        "",
        "## Tool Verifications",
        "",
    ]

    for v in audit_result.verifications:
        status = "✓" if v.passed else "✗"
        lines.append(f"### {status} {v.tool_name}")
        lines.append(f"- Permissions: {', '.join(v.actual_permissions) or 'none'}")

        if v.warnings:
            lines.append("- Warnings:")
            for w in v.warnings:
                lines.append(f"  - {w}")

        if v.errors:
            lines.append("- Errors:")
            for e in v.errors:
                lines.append(f"  - {e}")

        lines.append("")

    if audit_result.warnings:
        lines.append("## Overall Warnings")
        for w in audit_result.warnings:
            lines.append(f"- {w}")
        lines.append("")

    if audit_result.errors:
        lines.append("## Overall Errors")
        for e in audit_result.errors:
            lines.append(f"- {e}")

    return "\n".join(lines)
