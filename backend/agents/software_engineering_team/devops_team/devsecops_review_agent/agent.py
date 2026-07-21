"""DevSecOps review agent."""

from __future__ import annotations

from typing import Any, Dict

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent
from software_engineering_team.devops_team.models import ReviewFinding
from software_engineering_team.shared.security_service import derive_approved

from .models import DevSecOpsReviewInput, DevSecOpsReviewOutput
from .prompts import DEVSECOPS_REVIEW_PROMPT


class DevSecOpsReviewAgent(DevOpsSingleShotAgent):
    """Infra security reviewer for DevOps artifacts (IAM/secrets/network).

    Invariants: instance state is limited to ``llm`` and the resolved Strands
    ``_model`` from the base; ``run`` is stateless across calls.
    """

    PROMPT = DEVSECOPS_REVIEW_PROMPT
    temperature = 0.0

    def build_context(self, input_data: DevSecOpsReviewInput) -> str:
        """Build the review prompt context from task, requirements, and artifacts.

        Preconditions: ``input_data`` is a valid ``DevSecOpsReviewInput``.
        Postconditions: returns the same context string shape the pre-migration
        agent appended after the prompt separator.
        """
        return (
            f"task={input_data.task_description}\n"
            f"requirements={input_data.requirements}\n"
            f"artifacts={list(input_data.artifacts.keys())}\n"
        )

    def build_output(
        self, input_data: DevSecOpsReviewInput, data: Dict[str, Any]
    ) -> DevSecOpsReviewOutput:
        """Map the LLM JSON dict onto ``DevSecOpsReviewOutput``.

        Preconditions:
            ``data`` is the dict from ``complete_json_with_continuation``; it may
            include optional ``findings``/``approved``/``summary``.
        Postconditions:
            Returns a ``DevSecOpsReviewOutput`` whose ``approved`` follows the
            unified rule (:func:`derive_approved`): any blocking finding
            (critical/high severity or an explicit ``blocking`` flag) forces
            ``approved=False``; otherwise the model's ``approved`` is honored. An
            ``approved`` key that is present but null is treated as a non-approval
            (fail closed), matching the legacy contract; an absent key defers to
            the finding-derived default.
        """
        findings = [ReviewFinding(**f) for f in (data.get("findings") or []) if isinstance(f, dict)]
        # Distinguish an absent ``approved`` key (no opinion -> defer to findings)
        # from a present-but-null value (an explicit non-approval -> fail closed).
        llm_approved = bool(data["approved"]) if "approved" in data else None
        approved = derive_approved(findings, llm_approved=llm_approved)
        return DevSecOpsReviewOutput(
            approved=approved,
            findings=findings,
            summary=data.get("summary", ""),
        )
