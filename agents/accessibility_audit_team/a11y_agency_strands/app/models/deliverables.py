from pydantic import BaseModel


class ReportPackage(BaseModel):
    executive_summary: str
    technical_report: str
    action_plan: str
    component_remediation_guide: str
    wcag_scorecard: str
    sec508_addendum: str | None = None
    backlog_export: str


class DeliveryResult(BaseModel):
    delivered_artifacts: list[str]
    delivery_notes: str
