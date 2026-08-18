"""Infrastructure Debug agent -- classifies IaC execution errors."""

from __future__ import annotations

from typing import Any, Dict

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

from .models import IaCDebugInput, IaCDebugOutput, IaCExecutionError
from .prompts import INFRA_DEBUG_PROMPT

_FIXABLE_TYPES = frozenset({"syntax", "validation"})

# Bound the artifacts inlined into the debug prompt so a large or numerous IaC
# file can't push the single-shot classify call past the model's context
# window (that would raise and abort the debug/patch retry loop entirely).
_MAX_ARTIFACTS = 5
_MAX_ARTIFACT_CHARS = 2_000


class InfraDebugAgent(DevOpsSingleShotAgent):
    """Classifies IaC (terraform/cdk/compose/helm) execution errors from CLI output.

    Given the raw output of a failed IaC command, this agent uses an LLM to
    identify and classify the individual errors, summarize the failure, and
    judge whether the errors are automatically fixable.

    Invariants: instance state is limited to ``llm`` and ``_model`` from the
    base. ``run`` is deterministic for identical inputs and the resolved
    model: repeated identical calls may return a cached result and skip the
    LLM. Cache reads/writes are fail-open and gated by ``CACHE_ENV_VAR``.
    """

    PROMPT = INFRA_DEBUG_PROMPT
    CACHE_NAMESPACE = "devops:infra_debug:v1"
    CACHE_ENV_VAR = "DEVOPS_INFRA_DEBUG_CACHE_SIZE"
    OUTPUT_MODEL = IaCDebugOutput

    def build_context(self, input_data: IaCDebugInput) -> str:
        """Build the debug prompt context from tool/command/output/artifacts.

        Preconditions: ``input_data`` is a valid ``IaCDebugInput``.
        Postconditions: returns the same context string shape the
        pre-migration agent appended after the prompt separator, bounded to
        the first ``_MAX_ARTIFACTS`` artifacts at ``_MAX_ARTIFACT_CHARS``
        characters each.
        """
        artifacts_snippet = "".join(
            f"\n### {fname} ###\n{content[:_MAX_ARTIFACT_CHARS]}\n"
            for fname, content in list(input_data.artifacts.items())[:_MAX_ARTIFACTS]
        )
        return (
            f"Tool: {input_data.tool_name}\n"
            f"Command: {input_data.command}\n\n"
            f"--- Execution Output ---\n{input_data.execution_output}\n\n"
            f"--- Artifacts ---\n{artifacts_snippet}\n"
        )

    def build_output(self, input_data: IaCDebugInput, data: Dict[str, Any]) -> IaCDebugOutput:
        """Map the LLM JSON dict onto ``IaCDebugOutput``.

        Preconditions: ``data`` is the dict from ``complete_json_with_continuation``.
        Postconditions: returns an ``IaCDebugOutput`` whose ``errors`` list
        holds one ``IaCExecutionError`` per classified error (each carrying
        the raw execution output), and whose ``fixable`` flag is true only
        when there is at least one error and every error's type is in
        {"syntax", "validation"} (or the LLM explicitly overrides this via
        its own "fixable" response field).
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


def clear_review_cache() -> None:
    """Drop every cached infra debug result. Intended for test teardown."""
    InfraDebugAgent.clear_cache()
