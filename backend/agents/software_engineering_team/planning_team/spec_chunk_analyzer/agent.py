"""Spec Chunk Analyzer agent: analyzes one chunk of a spec."""

from __future__ import annotations

import logging

from llm_service import LLMClient
from software_engineering_team.shared.context_sizing import compute_spec_chunk_chars

from .models import SpecChunkAnalysis, SpecChunkAnalyzerInput
from .prompts import SPEC_CHUNK_ANALYZER_PROMPT

logger = logging.getLogger(__name__)


class SpecChunkAnalyzer:
    """
    Analyzes one chunk of a product specification.
    Output schema matches the full spec analysis for later merging.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: SpecChunkAnalyzerInput) -> SpecChunkAnalysis:
        """Analyze one spec chunk and return structured analysis."""
        spec_chunk = input_data.spec_chunk
        max_chunk_chars = compute_spec_chunk_chars(self.llm)
        if len(spec_chunk) > max_chunk_chars:
            logger.warning(
                "SpecChunkAnalyzer: truncating chunk from %s to %s chars (model context)",
                len(spec_chunk),
                max_chunk_chars,
            )
            spec_chunk = spec_chunk[:max_chunk_chars] + "\n\n... [truncated]"

        header = input_data.requirements_header or {}
        header_parts = [
            f"**Product Title:** {header.get('title', '')}",
            f"**Description:** {header.get('description', '')}",
        ]
        if header.get("acceptance_criteria"):
            header_parts.append("**Acceptance Criteria:**")
            header_parts.extend(f"- {c}" for c in header["acceptance_criteria"])
        if header.get("constraints"):
            header_parts.append("**Constraints:**")
            header_parts.extend(f"- {c}" for c in header["constraints"])
        header_parts.append(f"**Priority:** {header.get('priority', '')}")

        prompt = SPEC_CHUNK_ANALYZER_PROMPT.format(
            chunk_index=input_data.chunk_index,
            total_chunks=input_data.total_chunks,
        )
        prompt += "\n\n---\n\n" + "\n".join(header_parts)
        prompt += "\n\n---\n\n**Spec fragment (chunk {} of {}):**\n---\n{}\n---".format(
            input_data.chunk_index,
            input_data.total_chunks,
            spec_chunk,
        )

        logger.info(
            "SpecChunkAnalyzer: analyzing chunk %s/%s (%s chars)",
            input_data.chunk_index,
            input_data.total_chunks,
            len(spec_chunk),
        )

        data = self.llm.complete_json(prompt, temperature=0.1)

        return SpecChunkAnalysis(
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
