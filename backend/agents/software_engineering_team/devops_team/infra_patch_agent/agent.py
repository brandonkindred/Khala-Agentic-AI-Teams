"""Infrastructure Patch agent -- produces minimal IaC artifact patches."""

from __future__ import annotations

from typing import Any, Dict, Optional

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

from .models import IaCPatchInput, IaCPatchOutput
from .prompts import INFRA_PATCH_PROMPT


class InfraPatchAgent(DevOpsSingleShotAgent):
    """Produces minimal IaC artifact patches for fixable debug errors.

    Invariants: instance state is limited to ``llm`` and ``_model`` from the
    base. ``run`` is deterministic for identical inputs and the resolved
    model: repeated identical calls may return a cached result and skip the
    LLM (unless the ``not fixable`` short-circuit fires first). Cache
    reads/writes are fail-open and gated by ``CACHE_ENV_VAR``.
    """

    PROMPT = INFRA_PATCH_PROMPT
    CACHE_NAMESPACE = "devops:infra_patch:v1"
    CACHE_ENV_VAR = "DEVOPS_INFRA_PATCH_CACHE_SIZE"
    OUTPUT_MODEL = IaCPatchOutput

    def pre_call(self, input_data: IaCPatchInput) -> Optional[IaCPatchOutput]:
        """Short-circuit when the debug result says the errors aren't fixable.

        Preconditions: ``input_data`` is a valid ``IaCPatchInput``.
        Postconditions: returns a short-circuit ``IaCPatchOutput`` with
        ``summary="Errors are not fixable via code changes"`` and empty
        ``patched_artifacts`` when ``input_data.debug_output.fixable`` is
        ``False`` (no LLM call, no cache lookup); otherwise returns ``None``
        to continue.
        """
        if not input_data.debug_output.fixable:
            return IaCPatchOutput(summary="Errors are not fixable via code changes")
        return None

    def build_context(self, input_data: IaCPatchInput) -> str:
        """Build the patch prompt context from the debug errors and artifacts.

        Preconditions: ``input_data`` is a valid ``IaCPatchInput``.
        Postconditions: returns the same context string shape the
        pre-migration agent appended after the prompt separator.
        """
        errors_text = "\n".join(
            f"- [{e.error_type}] {e.file_path or '?'}:{e.line_number or '?'} — {e.error_message}"
            for e in input_data.debug_output.errors
        )

        artifacts_text = ""
        for fname, content in input_data.original_artifacts.items():
            artifacts_text += f"\n### {fname} ###\n{content}\n"

        return f"--- Errors ---\n{errors_text}\n\n--- Current Artifacts ---\n{artifacts_text}\n"

    def build_output(self, input_data: IaCPatchInput, data: Dict[str, Any]) -> IaCPatchOutput:
        """Map the LLM JSON dict onto ``IaCPatchOutput``.

        Preconditions: ``data`` is the dict from ``complete_json_with_continuation``.
        Postconditions: returns an ``IaCPatchOutput`` whose ``patched_artifacts``
        drops any blank/whitespace-only entries.
        """
        patched = data.get("patched_artifacts") or {}
        patched = {k: v for k, v in patched.items() if v and v.strip()}

        return IaCPatchOutput(
            patched_artifacts=patched,
            summary=data.get("summary", ""),
            edits_applied=data.get("edits_applied", len(patched)),
        )


def clear_review_cache() -> None:
    """Drop every cached infra patch result. Intended for test teardown."""
    InfraPatchAgent.clear_cache()
