"""Infrastructure Patch agent -- produces minimal IaC artifact patches."""

from __future__ import annotations

import logging

from llm_service import LLMClient, get_strands_model
from llm_service.strands_model import resolve_strands_model
from software_engineering_team.shared.llm import complete_json_with_continuation

from .models import IaCPatchInput, IaCPatchOutput
from .prompts import INFRA_PATCH_PROMPT

logger = logging.getLogger(__name__)


class InfraPatchAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = resolve_strands_model(
            llm_client, agent_key="devops", get_strands_model_fn=get_strands_model
        )

    def run(self, input_data: IaCPatchInput) -> IaCPatchOutput:
        """Produce a minimal patch to the IaC artifacts named in a fixable debug result.

        Preconditions:
            input_data is a valid IaCPatchInput.
        Postconditions:
            When input_data.debug_output.fixable is False, returns
            immediately with summary="Errors are not fixable via code
            changes" and no patched_artifacts; no LLM call is made.
            Otherwise builds a context from the debug errors and
            input_data.original_artifacts, calls the LLM to produce patched
            artifact contents, drops any blank/whitespace-only entries, and
            returns an IaCPatchOutput whose patched_artifacts holds the
            remaining entries, whose summary is the LLM's summary, and whose
            edits_applied is the LLM's reported count or, if absent, the
            number of patched artifacts.
        """
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

        context = f"--- Errors ---\n{errors_text}\n\n--- Current Artifacts ---\n{artifacts_text}\n"

        data = complete_json_with_continuation(
            self._model,
            INFRA_PATCH_PROMPT + "\n\n---\n\n" + context,
            temperature=0.1,
            think=True,
        )

        patched = data.get("patched_artifacts") or {}
        patched = {k: v for k, v in patched.items() if v and v.strip()}

        return IaCPatchOutput(
            patched_artifacts=patched,
            summary=data.get("summary", ""),
            edits_applied=data.get("edits_applied", len(patched)),
        )
