"""Task Generator agent: generates task plan from spec analysis via LLM.

Thin wrapper that builds the prompt, calls the LLM, and returns the raw
JSON result. No recursive decomposition or escalation heuristics.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from llm_service import LLMClient
from software_engineering_team.shared.context_sizing import (
    compute_task_generator_arch_chars,
    compute_task_generator_existing_chars,
    compute_task_generator_features_chars,
    compute_task_generator_spec_chars,
)

from .models import TaskGeneratorInput
from .prompts import TASK_GENERATOR_CONTEXT_NOTE

logger = logging.getLogger(__name__)


class TaskGeneratorAgent:
    """
    Generates task plan from spec analysis and capped context.
    Calls the LLM once and returns the raw JSON result.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: TaskGeneratorInput) -> Dict[str, Any]:
        """Generate task plan from analysis and capped inputs. Returns raw LLM dict."""
        from tech_lead_agent.prompts import TECH_LEAD_PROMPT

        prompt = self._build_prompt(input_data, TECH_LEAD_PROMPT)
        data = self.llm.complete_json(prompt, temperature=0.2)
        return data

    def _build_prompt(self, input_data: TaskGeneratorInput, tech_lead_prompt: str) -> str:
        """Assemble the full prompt from input data and context limits."""
        reqs = input_data.requirements
        merged = input_data.merged_spec_analysis
        max_spec = compute_task_generator_spec_chars(self.llm)
        max_existing = compute_task_generator_existing_chars(self.llm)
        max_features = compute_task_generator_features_chars(self.llm)
        codebase = (input_data.codebase_analysis or "")[:max_spec]
        spec_trunc = (input_data.spec_content_truncated or "")[:max_spec]
        existing = (input_data.existing_codebase or "")[:max_existing]
        features = (input_data.features_doc or "")[:max_features]

        context_parts: List[str] = [
            TASK_GENERATOR_CONTEXT_NOTE,
            f"**Product Title:** {reqs.title}",
            f"**Description:** {reqs.description}",
            "**Acceptance Criteria:**",
            *[f"- {c}" for c in reqs.acceptance_criteria],
            "**Constraints:**",
            *[f"- {c}" for c in reqs.constraints],
            f"**Priority:** {reqs.priority}",
        ]

        resolved = input_data.resolved_questions or []
        resolved_question_texts = {r.get("question", "") for r in resolved if isinstance(r, dict)}
        remaining_open = [
            q for q in (input_data.open_questions or []) if q not in resolved_question_texts
        ]

        if resolved:
            context_parts.extend(
                [
                    "",
                    "**USER-PROVIDED RESOLUTIONS (use these exactly):**",
                    *[
                        f"- **{r.get('question', '')}** -> {r.get('answer', '')}"
                        for r in resolved
                        if isinstance(r, dict)
                    ],
                ]
            )
        if remaining_open:
            context_parts.extend(
                [
                    "",
                    "**OPEN QUESTIONS (resolve with best-practice defaults):**",
                    *[f"- {q}" for q in remaining_open],
                ]
            )
        if input_data.assumptions:
            context_parts.extend(
                [
                    "",
                    "**Assumptions from Spec Intake:**",
                    *[f"- {a}" for a in input_data.assumptions],
                ]
            )

        if input_data.project_overview:
            po = input_data.project_overview
            context_parts.extend(
                [
                    "",
                    "**Project Overview:**",
                    f"- Primary goal: {po.get('primary_goal', '')}",
                    f"- Delivery strategy: {po.get('delivery_strategy', '')}",
                    "- Milestones: "
                    + ", ".join(m.get("name", "") for m in po.get("milestones", [])),
                ]
            )

        if features:
            context_parts.extend(
                [
                    "",
                    "**Features and Functionality:**",
                    "---",
                    features,
                    "---",
                ]
            )

        if input_data.repo_path:
            context_parts.extend(["", f"**Repo path:** {input_data.repo_path}"])

        if merged:
            context_parts.extend(
                [
                    "",
                    "**DEEP SPEC ANALYSIS:**",
                    "---",
                    merged,
                    "---",
                ]
            )

        if spec_trunc:
            context_parts.extend(
                [
                    "",
                    "**Truncated initial_spec.md (reference only):**",
                    "---",
                    spec_trunc,
                    "---",
                ]
            )

        if codebase:
            context_parts.extend(
                [
                    "",
                    "**CODEBASE ANALYSIS:**",
                    "---",
                    codebase,
                    "---",
                ]
            )

        if existing:
            context_parts.extend(
                [
                    "",
                    "**EXISTING CODE (sample):**",
                    "---",
                    existing,
                    "---",
                ]
            )

        if input_data.architecture:
            arch = input_data.architecture
            max_arch = compute_task_generator_arch_chars(self.llm)
            arch_doc = (arch.architecture_document or "")[:max_arch]
            context_parts.extend(
                [
                    "",
                    "**System Architecture:**",
                    arch.overview,
                    "",
                    "**Components:**",
                    *[f"- {c.name} ({c.type}): {c.description}" for c in arch.components],
                    "",
                    "**Architecture Document (excerpt):**",
                    arch_doc,
                ]
            )

        return tech_lead_prompt + "\n\n---\n\n" + "\n".join(context_parts)
