"""Models for DevSecOps review agent."""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from software_engineering_team.devops_team.models import ReviewFinding


class DevSecOpsReviewInput(BaseModel):
    task_description: str = ""
    requirements: str = ""
    artifacts: Dict[str, str] = Field(default_factory=dict)


class DevSecOpsReviewOutput(BaseModel):
    # Default True matches the legacy ``data.get("approved", not blocking)``
    # semantics: absent the LLM explicitly flagging approved=False, the
    # agent's post-processing re-derives approval from blocking findings
    # so the default only bites when findings is empty (no reason to block).
    approved: bool = True
    findings: List[ReviewFinding] = Field(default_factory=list)
    summary: str = ""


class DevSecOpsReviewLLMResponse(BaseModel):
    """Narrow LLM-authored shape for one DevSecOps review call's response.

    ``DevSecOpsReviewAgent.run`` validates every reply against this model via
    ``shared.single_shot_review.run_single_shot_review`` (schema-validated
    mode), replacing the previous ``complete_json_with_continuation`` call
    plus manual, unguarded ``ReviewFinding(**f)`` construction -- a malformed
    finding dict used to crash ``run()`` outright, with no retry and no
    fallback.

    ``findings`` and ``summary`` are required, not defaulted:
    ``DEVSECOPS_REVIEW_PROMPT`` (the infra profile of the shared security
    review prompt) explicitly tells the model to always emit these two
    top-level keys, so a reply missing either is a truncated/malformed
    response that should fail validation and drive the corrective retry, not
    silently default to an empty/clean-looking result. Each ``findings``
    item is validated against ``ReviewFinding`` itself, so a finding missing
    its required ``finding_id`` now also fails validation and drives a
    retry, instead of raising out of a bare ``ReviewFinding(**f)`` call.

    ``approved`` is the one deliberate exception, defaulted to ``None``
    rather than required: the legacy contract this agent has always honored
    treats an *absent* ``approved`` key as "the model expressed no opinion,
    defer entirely to the finding-derived default" -- a legitimate, expected
    reply shape, not evidence of truncation -- while a *present-but-null*
    value is an explicit non-approval (fail closed). Pydantic can't
    distinguish "absent" from "present but equal to the default" by value
    alone, so ``DevSecOpsReviewAgent.run`` reads ``"approved" in
    response.model_fields_set`` to recover that distinction before calling
    :func:`software_engineering_team.shared.security_service.derive_approved`.
    """

    approved: Optional[bool] = None
    findings: List[ReviewFinding] = Field(
        description="Infra security findings for the reviewed artifacts."
    )
    summary: str = Field(description="Overall DevSecOps review assessment.")
