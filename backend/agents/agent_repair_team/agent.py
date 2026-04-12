"""Repair Expert agent: analyzes agent crashes and suggests code fixes."""

from __future__ import annotations

import json
import logging
from typing import Any

from software_engineering_team.shared.prompt_utils import log_llm_prompt

from .models import RepairInput, RepairOutput
from .prompts import REPAIR_PROMPT

logger = logging.getLogger(__name__)


class RepairExpertAgent:
    """
    Analyzes tracebacks from crashed backend/frontend agents and produces minimal
    code fixes for the agent codebase (software_engineering_team/).
    """

    def __init__(self, llm_client: Any = None) -> None:
        if llm_client is not None:
            self._agent = llm_client
        else:
            from strands import Agent

            from llm_service import get_strands_model

            self._agent = Agent(
                model=get_strands_model("repair"),
                system_prompt=REPAIR_PROMPT,
            )

    def run(self, input_data: RepairInput) -> RepairOutput:
        """
        Analyze the crash and produce suggested fixes.
        Does not apply fixes; caller must validate paths and apply edits.
        """
        context = f"""**Traceback:**
```
{input_data.traceback}
```

**Exception:** {input_data.exception_type}: {input_data.exception_message}
**Task:** {input_data.task_id}
**Agent type:** {input_data.agent_type}
**Agent source path (edits must be under this):** {input_data.agent_source_path}
"""
        prompt = context
        log_llm_prompt(logger, "Repair", "analyze", input_data.task_id[:40], prompt)
        try:
            result = self._agent(prompt)
            raw = str(result).strip()
            data = json.loads(raw) if isinstance(raw, str) else raw
            fixes = data.get("suggested_fixes", [])
            if not isinstance(fixes, list):
                fixes = []
            summary = data.get("summary", "") or ""
            return RepairOutput(suggested_fixes=fixes, summary=summary, applied=False)
        except Exception as e:
            logger.warning("Repair agent failed to parse LLM output: %s", e)
            return RepairOutput(
                suggested_fixes=[],
                summary=f"Repair analysis failed: {e}",
                applied=False,
            )
