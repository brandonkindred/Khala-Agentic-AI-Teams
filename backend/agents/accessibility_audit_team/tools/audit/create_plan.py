"""
Tool: audit.create_plan

Create an audit plan and seed the audit workspace.
"""

from typing import Any, Dict, List

from pydantic import BaseModel, Field

from ...models import (
    AuditConstraints,
    AuditPlan,
    AuditTargets,
    MobileAppTarget,
    SamplingStrategy,
    TestRunConfig,
)


class CreatePlanInput(BaseModel):
    """Input for creating an audit plan."""

    audit_id: str = Field(..., description="Unique audit identifier")
    name: str = Field(default="", description="Human-readable audit name")
    targets: Dict[str, Any] = Field(
        default_factory=dict,
        description="Targets: web_urls (list of strings), mobile_apps (list of {platform, name, version, build})",
    )
    constraints: Dict[str, Any] = Field(
        default_factory=dict,
        description="Constraints: timebox_hours, environments, auth_required",
    )
    critical_journeys: List[str] = Field(
        default_factory=list, description="Critical user journeys to prioritize"
    )
    sampling: Dict[str, Any] = Field(
        default_factory=dict, description="Sampling: max_pages, strategy"
    )


class CreatePlanOutput(BaseModel):
    """Output from creating an audit plan."""

    audit_plan: AuditPlan
    coverage_matrix_ref: str = Field(default="", description="Reference to created coverage matrix")
    test_run_config_ref: str = Field(default="", description="Reference to test run config")
    inventory_ref: str = Field(default="", description="Reference to target inventory")


async def create_plan(input_data: CreatePlanInput) -> CreatePlanOutput:
    """
    Create an audit plan and seed the audit workspace.

    This tool is called by the Accessibility Program Lead (APL) during
    the Intake phase to establish scope, targets, and testing strategy.
    """
    # Parse targets
    web_urls = input_data.targets.get("web_urls", [])
    mobile_apps_raw = input_data.targets.get("mobile_apps", [])
    mobile_apps = [
        MobileAppTarget(
            platform=app.get("platform", "ios"),
            name=app.get("name", ""),
            version=app.get("version", ""),
            build=app.get("build", ""),
        )
        for app in mobile_apps_raw
    ]

    targets = AuditTargets(web_urls=web_urls, mobile_apps=mobile_apps)

    # Parse constraints
    constraints = AuditConstraints(
        timebox_hours=input_data.constraints.get("timebox_hours"),
        environments=input_data.constraints.get("environments", ["prod"]),
        auth_required=input_data.constraints.get("auth_required", False),
    )

    # Parse sampling
    sampling = SamplingStrategy(
        max_pages=input_data.sampling.get("max_pages"),
        strategy=input_data.sampling.get("strategy", "journey_based"),
    )

    # Create default test run config
    test_run_config = TestRunConfig()

    # Create the audit plan
    audit_plan = AuditPlan(
        audit_id=input_data.audit_id,
        name=input_data.name,
        targets=targets,
        constraints=constraints,
        critical_journeys=input_data.critical_journeys,
        sampling=sampling,
        test_run_config=test_run_config,
        coverage_matrix_ref=f"coverage_matrix_{input_data.audit_id}",
        inventory_ref=f"inventory_{input_data.audit_id}",
    )

    return CreatePlanOutput(
        audit_plan=audit_plan,
        coverage_matrix_ref=f"coverage_matrix_{input_data.audit_id}",
        test_run_config_ref=f"test_run_config_{input_data.audit_id}",
        inventory_ref=f"inventory_{input_data.audit_id}",
    )
