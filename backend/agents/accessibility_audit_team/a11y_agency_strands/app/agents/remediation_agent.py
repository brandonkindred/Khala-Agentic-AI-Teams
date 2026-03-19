from .base import ToolContext, tool
from ..tools import create_jira_issues, persist_artifact


@tool(context=True)
def run_remediation_planning(findings: list[dict], tool_context: ToolContext) -> dict:
    tickets = create_jira_issues(findings)
    roadmap = {
        "0-30": ["Fix critical journey blockers"],
        "30-90": ["Resolve high-severity systemic issues"],
        "90+": ["Institutionalize regression prevention"],
    }
    artifact = persist_artifact(
        f"{tool_context.invocation_state['artifact_root']}/remediation_plan.json",
        {"tickets": tickets, "roadmap": roadmap},
    )
    return {"phase": "remediation", "artifact": artifact, "tickets": tickets}
