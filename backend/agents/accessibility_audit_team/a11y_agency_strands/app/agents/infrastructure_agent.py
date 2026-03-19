from .base import ToolContext, tool
from ..tools import persist_artifact


@tool(context=True)
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
    return {"phase": "infrastructure_audit", "artifact": artifact}
