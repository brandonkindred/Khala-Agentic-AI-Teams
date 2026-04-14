from ..models.phase_result import InfrastructureAuditResult
from ..tools import persist_artifact
from .base import ToolContext, a11y_phase


@a11y_phase(context=True)
def run_infrastructure_audit(target: str, tool_context: ToolContext) -> dict:
    output = {
        "target": target,
        "ci_checks": "partial",
        "deployment_risk": "medium",
        "regression_controls": "needs automation",
    }
    artifact = persist_artifact(
        f"{tool_context.invocation_state['artifact_root']}/infrastructure.json", output
    )
    return InfrastructureAuditResult(artifact=artifact).model_dump()
