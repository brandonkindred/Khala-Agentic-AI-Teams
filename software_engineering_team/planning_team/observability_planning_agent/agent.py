from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from shared.llm import LLMClient

from .models import ObservabilityPlanningInput, ObservabilityPlanningOutput
from .prompts import OBSERVABILITY_PROMPT

logger = logging.getLogger(__name__)


class ObservabilityPlanningAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None
        self.llm = llm_client

    def run(self, input_data: ObservabilityPlanningInput) -> ObservabilityPlanningOutput:
        logger.info("Observability Planning: starting for %s", input_data.requirements_title)
        context = [f"**Product:** {input_data.requirements_title}", "**Architecture:**", (input_data.architecture_overview or "")[:3000], "**Infrastructure:**", (input_data.infrastructure_doc or "")[:2000], "**DevOps:**", (input_data.devops_doc or "")[:2000]]
        data: Dict[str, Any] = self.llm.complete_json(OBSERVABILITY_PROMPT + "\n\n---\n\n" + "\n".join(context), temperature=0.2) or {}
        out = ObservabilityPlanningOutput(
            slos_slis=(data.get("slos_slis") or "").strip(),
            logging_metrics_tracing=(data.get("logging_metrics_tracing") or "").strip(),
            alerting_runbooks=(data.get("alerting_runbooks") or "").strip(),
            capacity_plan=(data.get("capacity_plan") or "").strip(),
            summary=(data.get("summary") or "").strip(),
        )
        if input_data.plan_dir:
            p = Path(input_data.plan_dir).resolve()
            p.mkdir(parents=True, exist_ok=True)
            content = f"# Observability\n\n## SLOs/SLIs\n\n{out.slos_slis or 'TBD'}\n\n## Logging/Metrics/Tracing\n\n{out.logging_metrics_tracing or 'TBD'}\n\n## Alerting and Runbooks\n\n{out.alerting_runbooks or 'TBD'}\n\n## Capacity Plan\n\n{out.capacity_plan or 'TBD'}"
            (p / "observability.md").write_text(content, encoding="utf-8")
        return out
