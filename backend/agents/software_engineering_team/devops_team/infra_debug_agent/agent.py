"""Infrastructure Debug agent -- classifies IaC execution errors."""

from __future__ import annotations

from typing import Any, Dict

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

from .models import IaCDebugInput, IaCDebugOutput, IaCExecutionError
from .prompts import INFRA_DEBUG_PROMPT

_FIXABLE_TYPES = frozenset({"syntax", "validation"})


class InfraDebugAgent(DevOpsSingleShotAgent):
    """Classify IaC execution errors and derive whether they are fixable.

    Invariants: instance state is limited to ``llm`` and ``_model`` from the
    base; ``run`` is stateless across calls. ``_FIXABLE_TYPES`` remains a
    module-level frozenset used by ``build_output``.
    """

    PROMPT = INFRA_DEBUG_PROMPT

    def build_context(self, input_data: IaCDebugInput) -> str:
        """Build the debug prompt context from execution output and artifacts.

        Preconditions: ``input_data`` is a valid ``IaCDebugInput``.
        Postconditions: returns the same context string shape the pre-migration
        agent appended after the prompt separator (first five artifacts,
        2000-char snippets).
        """
        artifacts_snippet = ""
        for fname, content in list(input_data.artifacts.items())[:5]:
            artifacts_snippet += f"\n### {fname} ###\n{content[:2000]}\n"

        return (
            f"Tool: {input_data.tool_name}\n"
            f"Command: {input_data.command}\n\n"
            f"--- Execution Output ---\n{input_data.execution_output}\n\n"
            f"--- Artifacts ---\n{artifacts_snippet}\n"
        )

    def build_output(self, input_data: IaCDebugInput, data: Dict[str, Any]) -> IaCDebugOutput:
        """Map the LLM JSON dict onto ``IaCDebugOutput`` with derived fixable.

        Preconditions: ``data`` is the dict from ``complete_json_with_continuation``.
        Postconditions: returns ``IaCDebugOutput`` with the same
        ``IaCExecutionError`` field defaults, ``raw_output`` from the input,
        and ``fixable=data.get("fixable", derived)`` where derived is true
        iff every error type is in ``_FIXABLE_TYPES`` and the list is non-empty.
        """
        errors = []
        for err_data in data.get("errors") or []:
            errors.append(
                IaCExecutionError(
                    error_type=err_data.get("error_type", "unknown"),
                    tool=err_data.get("tool", input_data.tool_name),
                    file_path=err_data.get("file_path"),
                    line_number=err_data.get("line_number"),
                    error_message=err_data.get("error_message", ""),
                    raw_output=input_data.execution_output,
                )
            )

        fixable = bool(errors) and all(e.error_type in _FIXABLE_TYPES for e in errors)

        return IaCDebugOutput(
            errors=errors,
            summary=data.get("summary", ""),
            fixable=data.get("fixable", fixable),
        )
