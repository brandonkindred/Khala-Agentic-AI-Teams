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

from typing import Callable, Dict, List, Optional

from ..models import (
    AccessAuditResult,
    AccessVerification,
    ToolProvisionResult,
)
from ..shared.tool_agent_registry import build_default_tool_agents
from ..shared.tool_manifest import ToolManifest
from ..tool_agents.base import ToolProvisionerInterface


# Backwards-compat shim for older imports/tests.
def _build_provisioners() -> Dict[str, ToolProvisionerInterface]:
    return build_default_tool_agents()


def run_access_audit(
    agent_id: str,
    tool_results: List[ToolProvisionResult],
    manifest: Optional[ToolManifest] = None,
    provisioners: Optional[Dict[str, ToolProvisionerInterface]] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> AccessAuditResult:
    """Audit provisioned access for each tool.

    Args:
        agent_id: Unique identifier for the agent
        tool_results: Results from account provisioning phase
        manifest: Tool manifest (optional, kept for parity with future per-tool checks)
        provisioners: Provisioner instances (held for future re-verification)
        progress_callback: Callback for progress updates

    Returns:
        AccessAuditResult — passes whenever every tool succeeded in
        account provisioning. Failures during provisioning surface as
        per-tool errors and overall ``passed=False``; permission grants
        are recorded as-is, not validated against a tier.
    """
    # `provs` is held for future per-tool re-verification hooks; keeping the
    # parameter avoids churning the orchestrator call site if/when tier-free
    # auditing grows back.
    _ = provisioners if provisioners is not None else build_default_tool_agents()

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
) -> AccessVerification:
    """Re-verify a single tool by delegating to its provisioner."""
    provs = build_default_tool_agents()
    provisioner_name = f"{tool_name}_provisioner"
    prov = provisioner or provs.get(provisioner_name)

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
