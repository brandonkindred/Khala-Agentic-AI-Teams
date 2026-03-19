from .base import ToolContext, tool
from ..tools import persist_artifact, request_human_approval


@tool(context=True)
def run_approval_and_comms(
    engagement_id: str, summary: str, tool_context: ToolContext
) -> dict:
    approval = request_human_approval(engagement_id, summary)
    artifact = persist_artifact(
        f"{tool_context.invocation_state['artifact_root']}/approval.json",
        approval.model_dump(),
    )
    return {"phase": "approval", "artifact": artifact, "approved": approval.approved}
