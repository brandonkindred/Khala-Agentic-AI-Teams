"""Repair Expert agent: analyzes agent crashes and suggests code fixes."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from shared.llm import LLMClient
from shared.prompt_utils import log_llm_prompt

from .models import RepairInput, RepairOutput
from .prompts import REPAIR_PROMPT

logger = logging.getLogger(__name__)


class RepairExpertAgent:
    """
    Analyzes tracebacks from crashed backend/frontend agents and produces minimal
    code fixes for the agent codebase (software_engineering_team/).
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

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
        prompt = REPAIR_PROMPT + "\n\n---\n\n" + context
        log_llm_prompt(logger, "Repair", "analyze", input_data.task_id[:40], prompt)
        try:
            raw = self.llm.complete_json(prompt, temperature=0.1)
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
