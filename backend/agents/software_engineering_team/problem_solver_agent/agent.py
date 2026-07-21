"""General Problem Solver specialist implementation."""

from __future__ import annotations

import logging

from llm_service import get_strands_model
from llm_service.strands_model import resolve_strands_model
from software_engineering_team.shared.llm import complete_json_with_continuation

from .models import ProblemSolverInput, ProblemSolverOutput
from .prompts import PROBLEM_SOLVER_PROMPT

logger = logging.getLogger(__name__)


class ProblemSolverAgent:
    """
    Specialist that provides plan/execute/review/test guidance for bug fixing.

    Preconditions:
        - llm_client, if provided, is a Strands Model, a raw LLMClient, or None

    Postconditions:
        - self._model holds a resolved Strands Model, ready to be passed to
          complete_json_with_continuation on every run() call

    Invariants:
        - The agent keeps no conversational state between run() calls; each
          call builds a fresh Strands Agent internally via
          complete_json_with_continuation
    """

    def __init__(self, llm_client=None) -> None:
        """
        Resolve the Strands Model used for every subsequent run() call.

        Postconditions:
            - self._model is set to the Strands Model resolved from llm_client
              for the "problem_solver" agent key
        """
        self._model = resolve_strands_model(
            llm_client, agent_key="problem_solver", get_strands_model_fn=get_strands_model
        )

    def run(self, input_data: ProblemSolverInput) -> ProblemSolverOutput:
        """
        Generate a bounded specialist recommendation for a bug-fix cycle.

        Preconditions:
            - input_data is a valid ProblemSolverInput

        Postconditions:
            - Returns a ProblemSolverOutput populated from the LLM's JSON response

        Raises:
            LLMJsonParseError: if the LLM response cannot be parsed as JSON even
                after markdown-fence recovery.
        """
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
                    input_data.current_code_snapshot,
                    "```",
                ]
            )

        prompt = "\n".join(context)
        data = complete_json_with_continuation(
            self._model, prompt, system_prompt=PROBLEM_SOLVER_PROMPT
        )
        return ProblemSolverOutput(
            plan=str(data.get("plan", "")),
            execution_steps=str(data.get("execution_steps", "")),
            review_checks=str(data.get("review_checks", "")),
            testing_strategy=str(data.get("testing_strategy", "")),
            fix_recommendation=str(data.get("fix_recommendation", "")),
        )
