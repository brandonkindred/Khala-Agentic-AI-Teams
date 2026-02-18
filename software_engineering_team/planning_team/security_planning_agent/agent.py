from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from shared.llm import LLMClient

from .models import SecurityPlanningInput, SecurityPlanningOutput
from .prompts import SECURITY_PROMPT

logger = logging.getLogger(__name__)


class SecurityPlanningAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None
        self.llm = llm_client

    def run(self, input_data: SecurityPlanningInput) -> SecurityPlanningOutput:
        logger.info("Security Planning: starting for %s", input_data.requirements_title)
        context = [f"**Product:** {input_data.requirements_title}", "**Architecture:**", (input_data.architecture_overview or "")[:3000], "**Data Lifecycle:**", (input_data.data_lifecycle or "")[:2000]]
        data: Dict[str, Any] = self.llm.complete_json(SECURITY_PROMPT + "\n\n---\n\n" + "\n".join(context), temperature=0.2) or {}
        out = SecurityPlanningOutput(
            threat_model=(data.get("threat_model") or "").strip(),
            security_checklist=(data.get("security_checklist") or "").strip(),
            data_classification=(data.get("data_classification") or "").strip(),
            audit_requirements=(data.get("audit_requirements") or "").strip(),
            summary=(data.get("summary") or "").strip(),
        )
        if input_data.plan_dir:
            p = Path(input_data.plan_dir).resolve()
            p.mkdir(parents=True, exist_ok=True)
            content = f"# Security and Compliance\n\n## Threat Model\n\n{out.threat_model or 'TBD'}\n\n## Security Checklist\n\n{out.security_checklist or 'TBD'}\n\n## Data Classification\n\n{out.data_classification or 'TBD'}\n\n## Audit Requirements\n\n{out.audit_requirements or 'TBD'}"
            (p / "security_and_compliance.md").write_text(content, encoding="utf-8")
        return out
