from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from shared.llm import LLMClient

from .models import DevOpsPlanningInput, DevOpsPlanningOutput
from .prompts import DEVOPS_PROMPT

logger = logging.getLogger(__name__)


class DevOpsPlanningAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None
        self.llm = llm_client

    def run(self, input_data: DevOpsPlanningInput) -> DevOpsPlanningOutput:
        logger.info("DevOps Planning: starting for %s", input_data.requirements_title)
        context = [f"**Product:** {input_data.requirements_title}", "**Architecture:**", (input_data.architecture_overview or "")[:3000], "**Infrastructure:**", (input_data.infrastructure_doc or "")[:2000]]
        data: Dict[str, Any] = self.llm.complete_json(DEVOPS_PROMPT + "\n\n---\n\n" + "\n".join(context), temperature=0.2) or {}
        out = DevOpsPlanningOutput(
            ci_pipeline=(data.get("ci_pipeline") or "").strip(),
            cd_pipeline=(data.get("cd_pipeline") or "").strip(),
            iac_workflow=(data.get("iac_workflow") or "").strip(),
            release_strategy=(data.get("release_strategy") or "").strip(),
            summary=(data.get("summary") or "").strip(),
        )
        if input_data.plan_dir:
            p = Path(input_data.plan_dir).resolve()
            p.mkdir(parents=True, exist_ok=True)
            content = f"# DevOps Pipeline\n\n## CI\n\n{out.ci_pipeline or 'TBD'}\n\n## CD\n\n{out.cd_pipeline or 'TBD'}\n\n## IaC\n\n{out.iac_workflow or 'TBD'}\n\n## Release Strategy\n\n{out.release_strategy or 'TBD'}"
            (p / "devops_pipeline.md").write_text(content, encoding="utf-8")
        return out
