"""Infrastructure Patch agent — produces minimal IaC artifact patches.

Built on the AWS Strands Agents SDK via ``llm_service.LLMClientModel``.
"""

from __future__ import annotations

import logging

from strands import Agent

from llm_service import LLMClient, LLMClientModel

from .models import IaCPatchInput, IaCPatchOutput
from .prompts import INFRA_PATCH_PROMPT

logger = logging.getLogger(__name__)


class InfraPatchAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self._model = LLMClientModel(
            llm_client,
            agent_key="infra_patch",
            temperature=0.1,
            think=True,
        )

    def run(self, input_data: IaCPatchInput) -> IaCPatchOutput:
        # Short-circuit when the debug agent already said the errors can't
        # be fixed in-code. Avoids a wasted LLM call.
        if not input_data.debug_output.fixable:
            return IaCPatchOutput(
                summary="Errors are not fixable via code changes",
            )

        errors_text = "\n".join(
            f"- [{e.error_type}] {e.file_path or '?'}:{e.line_number or '?'} — {e.error_message}"
            for e in input_data.debug_output.errors
        )

        artifacts_text = ""
        for fname, content in input_data.original_artifacts.items():
            artifacts_text += f"\n### {fname} ###\n{content}\n"

        user_prompt = (
            "Acting as an infrastructure patch specialist, produce the "
            "minimal IaC artifact edits needed to fix the classified errors "
            "below. Produce structured JSON with fields: patched_artifacts "
            "(dict of file path to updated content), summary, edits_applied.\n\n"
            f"--- Errors ---\n{errors_text}\n\n"
            f"--- Current Artifacts ---\n{artifacts_text}\n"
        )

        agent = Agent(model=self._model, system_prompt=INFRA_PATCH_PROMPT)

        try:
            agent_result = agent(user_prompt, structured_output_model=IaCPatchOutput)
            result = agent_result.structured_output
            if not isinstance(result, IaCPatchOutput):
                raise TypeError(
                    f"Expected IaCPatchOutput, got {type(result).__name__ if result else 'None'}"
                )
        except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
            logger.warning("InfraPatch: structured_output failed (%s); returning empty output", exc)
            return IaCPatchOutput(
                patched_artifacts={},
                summary=f"Infrastructure patch failed: {exc}",
                edits_applied=0,
            )

        # Drop any empty/whitespace-only patches (the legacy code did this).
        result.patched_artifacts = {
            k: v for k, v in result.patched_artifacts.items() if v and v.strip()
        }
        # Keep edits_applied honest if the LLM omitted it.
        if not result.edits_applied:
            result.edits_applied = len(result.patched_artifacts)
        return result
