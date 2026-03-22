from ..tools import persist_artifact
from .base import ToolContext, tool


@tool(context=True)
def run_retest_cycle(engagement_id: str, tool_context: ToolContext) -> dict:
    output = {
        "engagement_id": engagement_id,
        "closed": [],
        "open": [],
        "monitoring_recommendation": "Run monthly checks",
    }
    artifact = persist_artifact(
        f"{tool_context.invocation_state['artifact_root']}/retest.json", output
    )
    return {"phase": "retest", "artifact": artifact}
