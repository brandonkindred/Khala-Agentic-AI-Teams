from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from shared.llm import LLMClient

from .models import InfrastructurePlanningInput, InfrastructurePlanningOutput
from .prompts import INFRA_PROMPT

logger = logging.getLogger(__name__)


class InfrastructurePlanningAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None
        self.llm = llm_client

    def run(self, input_data: InfrastructurePlanningInput) -> InfrastructurePlanningOutput:
        logger.info("Infrastructure Planning: starting for %s", input_data.requirements_title)
        context = [f"**Product:** {input_data.requirements_title}", "**Architecture:**", (input_data.architecture_overview or "")[:3000], "**Tenancy:**", (input_data.tenancy_model or "TBD")]
        data: Dict[str, Any] = self.llm.complete_json(INFRA_PROMPT + "\n\n---\n\n" + "\n".join(context), temperature=0.2) or {}
        out = InfrastructurePlanningOutput(
            cloud_diagram=(data.get("cloud_diagram") or "").strip(),
            environment_strategy=(data.get("environment_strategy") or "").strip(),
            iam_model=(data.get("iam_model") or "").strip(),
            cost_model=(data.get("cost_model") or "").strip(),
            summary=(data.get("summary") or "").strip(),
        )
        if input_data.plan_dir:
            p = Path(input_data.plan_dir).resolve()
            p.mkdir(parents=True, exist_ok=True)
            content = f"# Infrastructure\n\n## Cloud Architecture\n\n{out.cloud_diagram or 'TBD'}\n\n## Environments\n\n{out.environment_strategy or 'TBD'}\n\n## IAM\n\n{out.iam_model or 'TBD'}\n\n## Cost Model\n\n{out.cost_model or 'TBD'}"
            (p / "infrastructure.md").write_text(content, encoding="utf-8")
        return out
