from ..tools import persist_artifact
from .base import ToolContext, tool


@tool(context=True)
def run_architecture_audit(target: str, tool_context: ToolContext) -> dict:
    output = {
        "target": target,
        "navigation_consistency": "medium",
        "recommendations": ["Improve breadcrumb semantics"],
    }
    artifact = persist_artifact(
        f"{tool_context.invocation_state['artifact_root']}/architecture.json", output
    )
    return {"phase": "architecture_audit", "artifact": artifact}
