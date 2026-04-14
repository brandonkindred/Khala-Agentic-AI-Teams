from ..models.phase_result import RemediationResult
from ..tools import create_jira_issues, persist_artifact
from .base import ToolContext, a11y_phase


@a11y_phase(context=True)
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
    return RemediationResult(artifact=artifact, tickets=tickets).model_dump()
