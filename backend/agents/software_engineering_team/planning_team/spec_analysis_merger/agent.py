"""Spec Analysis Merger agent: merges chunk analyses into one."""

from __future__ import annotations

import json
import logging

from llm_service import LLMClient

from .models import MergedSpecAnalysis, SpecAnalysisMergerInput
from .prompts import SPEC_ANALYSIS_MERGER_PROMPT

logger = logging.getLogger(__name__)


class SpecAnalysisMerger:
    """
    Merges multiple spec chunk analyses into one consolidated analysis.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: SpecAnalysisMergerInput) -> MergedSpecAnalysis:
        """Merge chunk analyses and return consolidated result."""
        chunk_results = input_data.chunk_results
        spec_outline = (input_data.spec_outline or "").strip()

        if not chunk_results:
            return MergedSpecAnalysis()

        if len(chunk_results) == 1:
            single = chunk_results[0]
            return MergedSpecAnalysis(
                data_entities=single.get("data_entities") or [],
                api_endpoints=single.get("api_endpoints") or [],
                ui_screens=single.get("ui_screens") or [],
                user_flows=single.get("user_flows") or [],
                non_functional=single.get("non_functional") or [],
                infrastructure=single.get("infrastructure") or [],
                integrations=single.get("integrations") or [],
                total_deliverable_count=int(single.get("total_deliverable_count") or 0),
                summary=str(single.get("summary") or ""),
            )

        chunks_text = "\n\n---\n\n".join(
            f"**Chunk {i + 1}:**\n```json\n{json.dumps(c, indent=2)}\n```"
            for i, c in enumerate(chunk_results)
        )

        prompt = SPEC_ANALYSIS_MERGER_PROMPT
        prompt += "\n\n---\n\n**Chunk analyses to merge:**\n\n" + chunks_text
        if spec_outline:
            from software_engineering_team.shared.context_sizing import compute_spec_outline_chars

            max_outline = compute_spec_outline_chars(self.llm)
            prompt += (
                "\n\n---\n\n**Spec outline (section structure):**\n" + spec_outline[:max_outline]
            )

        logger.info(
            "SpecAnalysisMerger: merging %d chunk analyses",
            len(chunk_results),
        )

        data = self.llm.complete_json(prompt, temperature=0.1)

        return MergedSpecAnalysis(
            data_entities=data.get("data_entities") or [],
            api_endpoints=data.get("api_endpoints") or [],
            ui_screens=data.get("ui_screens") or [],
            user_flows=data.get("user_flows") or [],
            non_functional=data.get("non_functional") or [],
            infrastructure=data.get("infrastructure") or [],
            integrations=data.get("integrations") or [],
            total_deliverable_count=int(data.get("total_deliverable_count") or 0),
            summary=str(data.get("summary") or ""),
        )
