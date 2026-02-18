from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from shared.llm import LLMClient

from .models import FrontendArchitectureInput, FrontendArchitectureOutput
from .prompts import FRONTEND_ARCH_PROMPT

logger = logging.getLogger(__name__)


class FrontendArchitectureAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None
        self.llm = llm_client

    def run(self, input_data: FrontendArchitectureInput) -> FrontendArchitectureOutput:
        logger.info("Frontend Architecture: starting for %s", input_data.requirements_title)
        context = [f"**Product:** {input_data.requirements_title}", "**Architecture:**", (input_data.architecture_overview or "")[:2000], "**UI/UX:**", (input_data.ui_ux_doc or "")[:3000]]
        data: Dict[str, Any] = self.llm.complete_json(FRONTEND_ARCH_PROMPT + "\n\n---\n\n" + "\n".join(context), temperature=0.2) or {}
        out = FrontendArchitectureOutput(
            architecture_doc=(data.get("architecture_doc") or "").strip(),
            design_system=(data.get("design_system") or "").strip(),
            api_client_patterns=(data.get("api_client_patterns") or "").strip(),
            test_strategy=(data.get("test_strategy") or "").strip(),
            summary=(data.get("summary") or "").strip(),
        )
        if input_data.plan_dir:
            p = Path(input_data.plan_dir).resolve()
            p.mkdir(parents=True, exist_ok=True)
            content = f"# Frontend Architecture\n\n## Structure\n\n{out.architecture_doc or 'TBD'}\n\n## Design System\n\n{out.design_system or 'TBD'}\n\n## API Client\n\n{out.api_client_patterns or 'TBD'}\n\n## Test Strategy\n\n{out.test_strategy or 'TBD'}"
            (p / "frontend_architecture.md").write_text(content, encoding="utf-8")
        return out
