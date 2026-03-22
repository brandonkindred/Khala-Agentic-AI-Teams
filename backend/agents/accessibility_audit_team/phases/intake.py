"""
Phase 0: Intake and Audit Plan

APL collects scope, platforms, environments, constraints, target users and produces:
- Inventory (URLs/screens/components)
- Critical journeys
- Sampling strategy
- Risk assumptions
- Tool run configuration

SLMS sets mapping taxonomy and reporting tags.

Outputs: AuditPlan, CoverageMatrix, TestRunConfig
"""

from typing import Any, Optional

from ..agents import AccessibilityProgramLead, StandardsMappingSpecialist
from ..models import (
    AuditRequest,
    IntakeResult,
    Phase,
)


async def run_intake_phase(
    audit_request: AuditRequest,
    llm_client: Optional[Any] = None,
) -> IntakeResult:
    """
    Run the intake phase to create an audit plan.

    Args:
        audit_request: The audit request with targets and constraints
        llm_client: Optional LLM client for agent processing

    Returns:
        IntakeResult with audit plan, coverage matrix, and test config
    """
    # Initialize agents
    apl = AccessibilityProgramLead(llm_client)
    slms = StandardsMappingSpecialist(llm_client)

    # APL creates the audit plan
    apl_context = {
        "phase": Phase.INTAKE,
        "audit_request": audit_request,
    }

    apl_result = await apl.process(apl_context)

    if not apl_result.get("success"):
        return IntakeResult(
            success=False,
            error=apl_result.get("error", "APL failed to create audit plan"),
        )

    intake_result: IntakeResult = apl_result.get("intake_result")

    # SLMS sets up mapping taxonomy
    slms_context = {
        "phase": Phase.INTAKE,
        "audit_id": intake_result.audit_plan.audit_id,
    }

    slms_result = await slms.process(slms_context)

    if slms_result.get("success"):
        # Store guardrails for later use
        intake_result.summary += " SLMS established mapping guardrails."

    return intake_result


async def create_audit_plan(
    audit_id: str,
    name: str = "",
    web_urls: list = None,
    mobile_apps: list = None,
    critical_journeys: list = None,
    timebox_hours: int = None,
    auth_required: bool = False,
    max_pages: int = None,
    sampling_strategy: str = "journey_based",
    llm_client: Optional[Any] = None,
) -> IntakeResult:
    """
    Convenience function to create an audit plan.

    Returns:
        IntakeResult with the created plan
    """
    from ..models import MobileAppTarget, WCAGLevel

    # Convert mobile apps to MobileAppTarget if needed
    mobile_app_targets = []
    if mobile_apps:
        for app in mobile_apps:
            if isinstance(app, dict):
                mobile_app_targets.append(MobileAppTarget(**app))
            else:
                mobile_app_targets.append(app)

    request = AuditRequest(
        audit_id=audit_id,
        name=name,
        web_urls=web_urls or [],
        mobile_apps=mobile_app_targets,
        critical_journeys=critical_journeys or [],
        timebox_hours=timebox_hours,
        auth_required=auth_required,
        max_pages=max_pages,
        sampling_strategy=sampling_strategy,
        wcag_levels=[WCAGLevel.A, WCAGLevel.AA],
    )

    return await run_intake_phase(request, llm_client)
