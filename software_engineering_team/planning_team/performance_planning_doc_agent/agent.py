from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from shared.llm import LLMClient

from .models import PerformancePlanningDocInput, PerformancePlanningDocOutput
from .prompts import PERF_DOC_PROMPT

logger = logging.getLogger(__name__)


class PerformancePlanningDocAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None
        self.llm = llm_client

    def run(self, input_data: PerformancePlanningDocInput) -> PerformancePlanningDocOutput:
        logger.info("Performance Planning Doc: starting for %s", input_data.requirements_title)
        context = [f"**Product:** {input_data.requirements_title}", "**Architecture:**", (input_data.architecture_overview or "")[:2000]]
        data: Dict[str, Any] = self.llm.complete_json(PERF_DOC_PROMPT + "\n\n---\n\n" + "\n".join(context), temperature=0.2) or {}
        out = PerformancePlanningDocOutput(
            profiling_plan=(data.get("profiling_plan") or "").strip(),
            load_tests=(data.get("load_tests") or "").strip(),
            caching_cdn=(data.get("caching_cdn") or "").strip(),
            summary=(data.get("summary") or "").strip(),
        )
        if input_data.plan_dir:
            p = Path(input_data.plan_dir).resolve()
            p.mkdir(parents=True, exist_ok=True)
            content = f"# Performance\n\n## Profiling Plan\n\n{out.profiling_plan or 'TBD'}\n\n## Load Tests\n\n{out.load_tests or 'TBD'}\n\n## Caching/CDN\n\n{out.caching_cdn or 'TBD'}"
            (p / "performance.md").write_text(content, encoding="utf-8")
        return out
