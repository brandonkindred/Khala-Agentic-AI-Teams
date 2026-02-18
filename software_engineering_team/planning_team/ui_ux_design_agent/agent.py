from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from shared.llm import LLMClient

from .models import UiUxDesignInput, UiUxDesignOutput
from .prompts import UI_UX_PROMPT

logger = logging.getLogger(__name__)


class UiUxDesignAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None
        self.llm = llm_client

    def run(self, input_data: UiUxDesignInput) -> UiUxDesignOutput:
        logger.info("UI/UX Design: starting for %s", input_data.requirements_title)
        context = [f"**Product:** {input_data.requirements_title}", "**Features:**", (input_data.features_doc or "")[:4000], "**Spec:**", (input_data.spec_content or "")[:5000]]
        data: Dict[str, Any] = self.llm.complete_json(UI_UX_PROMPT + "\n\n---\n\n" + "\n".join(context), temperature=0.2) or {}
        out = UiUxDesignOutput(
            user_journeys=(data.get("user_journeys") or "").strip(),
            wireframes=(data.get("wireframes") or "").strip(),
            component_inventory=(data.get("component_inventory") or "").strip(),
            accessibility_requirements=(data.get("accessibility_requirements") or "").strip(),
            summary=(data.get("summary") or "").strip(),
        )
        if input_data.plan_dir:
            p = Path(input_data.plan_dir).resolve()
            p.mkdir(parents=True, exist_ok=True)
            content = f"# UI/UX Design\n\n## User Journeys\n\n{out.user_journeys or 'TBD'}\n\n## Wireframes\n\n{out.wireframes or 'TBD'}\n\n## Component Inventory\n\n{out.component_inventory or 'TBD'}\n\n## Accessibility\n\n{out.accessibility_requirements or 'TBD'}"
            (p / "ui_ux.md").write_text(content, encoding="utf-8")
        return out
