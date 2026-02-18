from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from shared.llm import LLMClient

from .models import QaTestStrategyInput, QaTestStrategyOutput
from .prompts import QA_TEST_STRATEGY_PROMPT

logger = logging.getLogger(__name__)


class QaTestStrategyAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None
        self.llm = llm_client

    def run(self, input_data: QaTestStrategyInput) -> QaTestStrategyOutput:
        logger.info("QA Test Strategy: starting for %s", input_data.requirements_title)
        context = [
            f"**Product:** {input_data.requirements_title}",
            "**Acceptance Criteria:**",
            *[f"- {c}" for c in input_data.acceptance_criteria[:20]],
            "**REQ-IDs:** " + ", ".join(input_data.requirement_ids[:30]) if input_data.requirement_ids else "",
            "**Architecture:**",
            (input_data.architecture_overview or "")[:2000],
        ]
        data: Dict[str, Any] = self.llm.complete_json(QA_TEST_STRATEGY_PROMPT + "\n\n---\n\n" + "\n".join(context), temperature=0.2) or {}
        out = QaTestStrategyOutput(
            test_pyramid=(data.get("test_pyramid") or "").strip(),
            test_case_matrix=(data.get("test_case_matrix") or "").strip(),
            test_data_strategy=(data.get("test_data_strategy") or "").strip(),
            smoke_tests=(data.get("smoke_tests") or "").strip(),
            summary=(data.get("summary") or "").strip(),
        )
        if input_data.plan_dir:
            p = Path(input_data.plan_dir).resolve()
            p.mkdir(parents=True, exist_ok=True)
            content = f"# Test Strategy\n\n## Test Pyramid\n\n{out.test_pyramid or 'TBD'}\n\n## Test Case Matrix\n\n{out.test_case_matrix or 'TBD'}\n\n## Test Data Strategy\n\n{out.test_data_strategy or 'TBD'}\n\n## Smoke Tests\n\n{out.smoke_tests or 'TBD'}"
            (p / "test_strategy.md").write_text(content, encoding="utf-8")
        return out
