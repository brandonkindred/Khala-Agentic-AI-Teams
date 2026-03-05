"""Infrastructure Patch agent -- produces minimal IaC artifact patches."""

from __future__ import annotations

import logging

from software_engineering_team.shared.llm import LLMClient

from .models import IaCPatchInput, IaCPatchOutput
from .prompts import INFRA_PATCH_PROMPT

logger = logging.getLogger(__name__)


class InfraPatchAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: IaCPatchInput) -> IaCPatchOutput:
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

        context = (
            f"--- Errors ---\n{errors_text}\n\n"
            f"--- Current Artifacts ---\n{artifacts_text}\n"
        )

        data = self.llm.complete_json(
            INFRA_PATCH_PROMPT + "\n\n---\n\n" + context,
            temperature=0.1,
        )

        patched = data.get("patched_artifacts") or {}
        patched = {k: v for k, v in patched.items() if v and v.strip()}

        return IaCPatchOutput(
            patched_artifacts=patched,
            summary=data.get("summary", ""),
            edits_applied=data.get("edits_applied", len(patched)),
        )
