"""
Access audit phase: Verify least-privilege compliance.

This is phase 4 of the provisioning workflow.
"""

from typing import Callable, Dict, List, Optional

from ..models import (
    AccessAuditResult,
    AccessTier,
    AccessVerification,
    ToolProvisionResult,
)
from ..shared.access_policy import validate_permissions
from ..shared.tool_manifest import ToolManifest
from ..tool_agents.base import ToolProvisionerInterface
from ..tool_agents.docker_provisioner import DockerProvisionerTool
from ..tool_agents.generic_provisioner import GenericProvisionerTool
from ..tool_agents.git_provisioner import GitProvisionerTool
from ..tool_agents.postgres_provisioner import PostgresProvisionerTool
from ..tool_agents.redis_provisioner import RedisProvisionerTool


def _build_provisioners() -> Dict[str, ToolProvisionerInterface]:
    """Build the default set of tool provisioners."""
    return {
        "docker_provisioner": DockerProvisionerTool(),
        "postgres_provisioner": PostgresProvisionerTool(),
        "git_provisioner": GitProvisionerTool(),
        "redis_provisioner": RedisProvisionerTool(),
        "generic_provisioner": GenericProvisionerTool(),
    }


def run_access_audit(
    agent_id: str,
    tool_results: List[ToolProvisionResult],
    access_tier: AccessTier,
    manifest: Optional[ToolManifest] = None,
    provisioners: Optional[Dict[str, ToolProvisionerInterface]] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> AccessAuditResult:
    """
    Execute the access audit phase.

    Verifies that provisioned access matches the requested tier
    and doesn't exceed least-privilege requirements.

    Args:
        agent_id: Unique identifier for the agent
        tool_results: Results from account provisioning phase
        access_tier: Requested access tier
        manifest: Tool manifest (optional, for additional validation)
        provisioners: Provisioner instances for verification
        progress_callback: Callback for progress updates

    Returns:
        AccessAuditResult with verification results
    """
    provisioners or _build_provisioners()

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
                    expected_tier=access_tier.value,
                    actual_permissions=[],
                    errors=[f"Tool provisioning failed: {result.error}"],
                )
            )
            all_errors.append(f"{result.tool_name}: provisioning failed")
            continue

        if progress_callback:
            progress_callback(f"Auditing {result.tool_name}...")

        passed, warnings = validate_permissions(
            result.tool_name,
            access_tier,
            result.permissions,
        )

        verification = AccessVerification(
            tool_name=result.tool_name,
            passed=passed,
            expected_tier=access_tier.value,
            actual_permissions=result.permissions,
            warnings=warnings,
        )

        verifications.append(verification)
        all_warnings.extend(warnings)

        if not passed:
            all_errors.append(f"{result.tool_name}: over-permissioned")

    if progress_callback:
        progress_callback("Access audit complete")

    overall_passed = all(v.passed for v in verifications)

    return AccessAuditResult(
        passed=overall_passed,
        tier_requested=access_tier.value,
        verifications=verifications,
        warnings=all_warnings,
        errors=all_errors,
    )


def audit_single_tool(
    agent_id: str,
    tool_name: str,
    expected_tier: AccessTier,
    provisioner: Optional[ToolProvisionerInterface] = None,
) -> AccessVerification:
    """
    Audit access for a single tool.

    Args:
        agent_id: Agent identifier
        tool_name: Tool to audit
        expected_tier: Expected access tier
        provisioner: Provisioner instance

    Returns:
        AccessVerification result
    """
    provs = _build_provisioners()

    provisioner_name = f"{tool_name}_provisioner"
    prov = provisioner or provs.get(provisioner_name)

    if prov is None:
        return AccessVerification(
            tool_name=tool_name,
            passed=False,
            expected_tier=expected_tier.value,
            actual_permissions=[],
            errors=[f"No provisioner found for {tool_name}"],
        )

    return prov.verify_access(agent_id, expected_tier)


def generate_audit_report(audit_result: AccessAuditResult) -> str:
    """
    Generate a human-readable audit report.

    Args:
        audit_result: The audit result to report on

    Returns:
        Formatted audit report string
    """
    lines = [
        "# Access Audit Report",
        "",
        f"**Tier Requested:** {audit_result.tier_requested}",
        f"**Overall Status:** {'PASSED' if audit_result.passed else 'FAILED'}",
        "",
        "## Tool Verifications",
        "",
    ]

    for v in audit_result.verifications:
        status = "✓" if v.passed else "✗"
        lines.append(f"### {status} {v.tool_name}")
        lines.append(f"- Expected: {v.expected_tier}")
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
