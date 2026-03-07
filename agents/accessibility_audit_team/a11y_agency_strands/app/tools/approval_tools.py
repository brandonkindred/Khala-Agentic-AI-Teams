from ..models import ApprovalRequest


def request_human_approval(engagement_id: str, summary: str) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=f"approval-{engagement_id}",
        engagement_id=engagement_id,
        summary=summary,
        approved=False,
    )
