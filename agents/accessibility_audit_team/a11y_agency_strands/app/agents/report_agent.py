from .base import ToolContext, tool
from ..models import ReportPackage
from ..tools import export_backlog_csv, persist_artifact, render_pdf, write_docx_from_template


@tool(context=True)
def run_reporting(engagement_id: str, findings: list[dict], tool_context: ToolContext) -> dict:
    backlog = export_backlog_csv(findings)
    report = ReportPackage(
        executive_summary="Executive summary",
        technical_report=write_docx_from_template("technical_report", {"engagement_id": engagement_id}),
        action_plan="Action plan",
        component_remediation_guide="Component guide",
        wcag_scorecard="WCAG scorecard",
        sec508_addendum="Section 508 addendum",
        backlog_export=backlog,
    )
    pdf_path = render_pdf(report.technical_report)
    artifact = persist_artifact(f"{tool_context.invocation_state['artifact_root']}/report_package.json", {
        **report.model_dump(),
        "technical_report_pdf": pdf_path,
    })
    return {"phase": "reporting", "artifact": artifact}
