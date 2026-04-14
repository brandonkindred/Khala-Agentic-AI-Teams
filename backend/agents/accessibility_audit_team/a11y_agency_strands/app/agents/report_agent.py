from ..models import CaseStudy, ReportPackage
from ..models.phase_result import ReportingResult
from ..tools import (
    export_backlog_csv,
    persist_artifact,
    render_case_study,
    render_pdf,
    write_docx_from_template,
)
from .base import ToolContext, a11y_phase


@a11y_phase(context=True)
def run_reporting(engagement_id: str, findings: list[dict], tool_context: ToolContext) -> dict:
    backlog = export_backlog_csv(findings)

    client_context = tool_context.invocation_state.get("client_context", {})
    template_key = client_context.get("service_tier", "comprehensive")
    industry = client_context.get("industry")

    case_study_data = render_case_study(
        engagement_id=engagement_id,
        findings=findings,
        client_context=client_context,
        template_key=template_key,
        industry=industry,
    )
    case_study = CaseStudy(**case_study_data)

    report = ReportPackage(
        executive_summary="Executive summary",
        technical_report=write_docx_from_template(
            "technical_report", {"engagement_id": engagement_id}
        ),
        action_plan="Action plan",
        component_remediation_guide="Component guide",
        wcag_scorecard="WCAG scorecard",
        sec508_addendum="Section 508 addendum",
        backlog_export=backlog,
        case_study=case_study,
    )
    pdf_path = render_pdf(report.technical_report)

    case_study_artifact = persist_artifact(
        f"{tool_context.invocation_state['artifact_root']}/case_study_{engagement_id}.json",
        case_study.model_dump(),
    )

    artifact = persist_artifact(
        f"{tool_context.invocation_state['artifact_root']}/report_package.json",
        {
            **report.model_dump(),
            "technical_report_pdf": pdf_path,
            "case_study_artifact": case_study_artifact,
        },
    )
    return ReportingResult(artifact=artifact).model_dump()
