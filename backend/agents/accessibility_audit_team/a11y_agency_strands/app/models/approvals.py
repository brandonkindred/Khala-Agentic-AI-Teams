from pydantic import BaseModel


class ApprovalRequest(BaseModel):
    approval_id: str
    engagement_id: str
    summary: str
    approved: bool = False
