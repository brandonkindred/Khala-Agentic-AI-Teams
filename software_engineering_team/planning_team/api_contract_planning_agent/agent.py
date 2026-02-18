"""API and Contract Design agent: produces OpenAPI, error model, versioning, contract tests plan."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from shared.llm import LLMClient

from .models import ApiContractPlanningInput, ApiContractPlanningOutput
from .prompts import API_CONTRACT_PROMPT

logger = logging.getLogger(__name__)

DEFAULT_OPENAPI = """openapi: 3.0.3
info:
  title: API
  version: 1.0.0
paths: {}
"""


def _write_artifact(plan_dir: Path, filename: str, content: str) -> Path:
    """Write a plan artifact to plan_dir. Returns path."""
    plan_dir = Path(plan_dir).resolve()
    plan_dir.mkdir(parents=True, exist_ok=True)
    out_file = plan_dir / filename
    out_file.write_text(content, encoding="utf-8")
    logger.info("Wrote plan artifact to %s", out_file)
    return out_file


class ApiContractPlanningAgent:
    """
    Designs APIs as contracts first: OpenAPI, error model, versioning, contract tests.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: ApiContractPlanningInput) -> ApiContractPlanningOutput:
        """Produce OpenAPI spec, error model, versioning policy, contract tests plan."""
        logger.info("API Contract Planning: starting for %s", input_data.requirements_title)
        context = [
            f"**Product:** {input_data.requirements_title}",
            "**Acceptance Criteria:**",
            *[f"- {c}" for c in input_data.acceptance_criteria[:15]],
            "",
            "**Architecture:**",
            (input_data.architecture_overview or "")[:3000],
            "",
            "**Spec (excerpt):**",
            (input_data.spec_content or "")[:5000],
        ]
        prompt = API_CONTRACT_PROMPT + "\n\n---\n\n" + "\n".join(context)
        data: Dict[str, Any] = self.llm.complete_json(prompt, temperature=0.2) or {}

        openapi_yaml = (data.get("openapi_yaml") or "").strip() or DEFAULT_OPENAPI
        error_model = (data.get("error_model") or "").strip()
        versioning_policy = (data.get("versioning_policy") or "").strip()
        contract_tests_plan = (data.get("contract_tests_plan") or "").strip()
        summary = (data.get("summary") or "").strip()

        openapi_path = None
        if input_data.plan_dir:
            plan_path = Path(input_data.plan_dir).resolve()
            openapi_path = _write_artifact(plan_path, "openapi.yaml", openapi_yaml)
            if error_model:
                _write_artifact(plan_path, "api_error_model.md", "# API Error Model\n\n" + error_model)
            if versioning_policy:
                _write_artifact(plan_path, "api_versioning.md", "# API Versioning and Deprecation\n\n" + versioning_policy)
            if contract_tests_plan:
                _write_artifact(plan_path, "contract_tests_plan.md", "# Contract Tests Plan\n\n" + contract_tests_plan)

        logger.info("API Contract Planning: done")
        return ApiContractPlanningOutput(
            openapi_path=openapi_path,
            error_model_doc=error_model,
            versioning_policy=versioning_policy,
            contract_tests_plan=contract_tests_plan,
            summary=summary,
        )
