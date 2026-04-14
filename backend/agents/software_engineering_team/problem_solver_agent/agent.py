"""General Problem Solver specialist implementation."""

from __future__ import annotations

import json
import logging

from strands import Agent

from llm_service import get_strands_model

from .models import ProblemSolverInput, ProblemSolverOutput
from .prompts import PROBLEM_SOLVER_PROMPT

logger = logging.getLogger(__name__)


class ProblemSolverAgent:
    """Specialist that provides plan/execute/review/test guidance for bug fixing."""

    def __init__(self, llm_client=None) -> None:
        from strands.models.model import Model as _StrandsModel

        if llm_client is not None and isinstance(llm_client, _StrandsModel):
            _model = llm_client
        else:
            _model = get_strands_model("problem_solver")
        self._agent = Agent(model=_model, system_prompt=PROBLEM_SOLVER_PROMPT)

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
            context.extend(
                [
                    "",
                    "**Current code snapshot (truncated):**",
                    "```",
                    input_data.current_code_snapshot[:6000],
                    "```",
                ]
            )

        prompt = "\n".join(context)
        result = self._agent(prompt)
        raw = str(result).strip()
        data = json.loads(raw)
        return ProblemSolverOutput(
            plan=str(data.get("plan", "")),
            execution_steps=str(data.get("execution_steps", "")),
            review_checks=str(data.get("review_checks", "")),
            testing_strategy=str(data.get("testing_strategy", "")),
            fix_recommendation=str(data.get("fix_recommendation", "")),
        )
