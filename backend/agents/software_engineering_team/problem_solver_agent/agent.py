"""General Problem Solver specialist implementation."""

from __future__ import annotations

import logging

from software_engineering_team.shared.llm import LLMClient

from .models import ProblemSolverInput, ProblemSolverOutput
from .prompts import PROBLEM_SOLVER_PROMPT

logger = logging.getLogger(__name__)


class ProblemSolverAgent:
    """Specialist that provides plan/execute/review/test guidance for bug fixing."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: ProblemSolverInput) -> ProblemSolverOutput:
        """Generate a bounded specialist recommendation for a bug-fix cycle."""
        context = [
            f"**Cycle:** {input_data.cycle}",
            f"**Specialty:** {input_data.specialty}",
            f"**Task:** {input_data.task_description}",
            "**Bug:**",
            "```",
            input_data.bug_description,
            "```",
        ]
        if input_data.current_code_snapshot:
            context.extend([
                "",
                "**Current code snapshot (truncated):**",
                "```",
                input_data.current_code_snapshot[:6000],
                "```",
            ])

        prompt = PROBLEM_SOLVER_PROMPT + "\n\n---\n\n" + "\n".join(context)
        data = self.llm.complete_json(prompt, temperature=0.1)
        return ProblemSolverOutput(
            plan=str(data.get("plan", "")),
            execution_steps=str(data.get("execution_steps", "")),
            review_checks=str(data.get("review_checks", "")),
            testing_strategy=str(data.get("testing_strategy", "")),
            fix_recommendation=str(data.get("fix_recommendation", "")),
        )
