"""DevSecOps review agent."""

from __future__ import annotations

import json

from devops_team.models import ReviewFinding
from strands import Agent

from llm_service import LLMClient, get_strands_model
from software_engineering_team.shared.security_service import derive_approved

from .models import DevSecOpsReviewInput, DevSecOpsReviewOutput
from .prompts import DEVSECOPS_REVIEW_PROMPT


class DevSecOpsReviewAgent:
    """Infra security reviewer for DevOps artifacts (IAM/secrets/network).

    Invariants: instance state is limited to ``llm`` and the resolved Strands
    ``_model``; ``run`` is stateless across calls (a fresh ``Agent`` per call).
    """

    def __init__(self, llm_client: LLMClient) -> None:
        """Resolve the review model.

        Preconditions: ``llm_client`` is not None (an ``LLMClient`` or a Strands
        ``Model``).
        Postconditions: ``self._model`` is a usable Strands model — the passed
        client when it is already a Strands ``Model``, else the ``devops`` model.
        """
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        from strands.models.model import Model as _StrandsModel

        if isinstance(llm_client, _StrandsModel):
            self._model = llm_client
        else:
            self._model = get_strands_model("devops")

    def run(self, input_data: DevSecOpsReviewInput) -> DevSecOpsReviewOutput:
        """Review DevOps artifacts and derive a blocking decision.

        Preconditions:
            ``input_data`` is a ``DevSecOpsReviewInput``; the model returns a JSON
            object with optional ``findings``/``approved``/``summary``.
        Postconditions:
            Returns a ``DevSecOpsReviewOutput`` whose ``approved`` follows the
            unified rule (:func:`derive_approved`): any blocking finding
            (critical/high severity or an explicit ``blocking`` flag) forces
            ``approved=False``; otherwise the model's ``approved`` is honored. An
            ``approved`` key that is present but null is treated as a non-approval
            (fail closed), matching the legacy contract; an absent key defers to
            the finding-derived default.
        """
        context = (
            f"task={input_data.task_description}\n"
            f"requirements={input_data.requirements}\n"
            f"artifacts={list(input_data.artifacts.keys())}\n"
        )
        data = json.loads(
            str(
                Agent(model=self._model)(
                    DEVSECOPS_REVIEW_PROMPT + "\n\n---\n\n" + context, temperature=0.0, think=True
                )
            ).strip()
        )
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
