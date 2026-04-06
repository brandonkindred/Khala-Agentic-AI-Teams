from pydantic import BaseModel


class CaseStudy(BaseModel):
    """Rendered case study artifact from the case study templates."""

    template_used: str = ""
    template_key: str = ""
    industry: str | None = None
    engagement_id: str = ""
    artifact: str = ""
    sections: list[dict] = []
    metrics: dict = {}


class ReportPackage(BaseModel):
    executive_summary: str
    technical_report: str
    action_plan: str
    component_remediation_guide: str
    wcag_scorecard: str
    sec508_addendum: str | None = None
    backlog_export: str
    case_study: CaseStudy | None = None


class DeliveryResult(BaseModel):
    delivered_artifacts: list[str]
    delivery_notes: str
