from .base import ToolContext, tool
from ..tools import build_page_inventory, persist_artifact, run_lighthouse_accessibility


@tool(context=True)
def run_page_audit(page_id: str, tool_context: ToolContext) -> dict:
    record = {"page": page_id, "tier": "tier1", "status": "audited"}
    build_page_inventory([record])
    run_lighthouse_accessibility(page_id)
    artifact = persist_artifact(f"{tool_context.invocation_state['artifact_root']}/page_{page_id}.json", record)
    return {"phase": "page_audit", "artifact": artifact, "page": page_id}
