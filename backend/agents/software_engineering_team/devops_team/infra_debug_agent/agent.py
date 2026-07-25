"""Infrastructure Debug agent -- classifies IaC execution errors."""

from __future__ import annotations

import logging

from llm_service import LLMClient, get_strands_model
from llm_service.strands_model import resolve_strands_model
from software_engineering_team.shared.llm import complete_json_with_continuation

from .models import IaCDebugInput, IaCDebugOutput, IaCExecutionError
from .prompts import INFRA_DEBUG_PROMPT

logger = logging.getLogger(__name__)

_FIXABLE_TYPES = frozenset({"syntax", "validation"})

# Bound the artifacts inlined into the debug prompt so a large or numerous IaC
# file can't push the single-shot classify call past the model's context
# window (that would raise and abort the debug/patch retry loop entirely).
_MAX_ARTIFACTS = 5
_MAX_ARTIFACT_CHARS = 2_000


class InfraDebugAgent:
    """Classifies IaC (terraform/cdk/compose/helm) execution errors from CLI output.

    Given the raw output of a failed IaC command, this agent uses an LLM to
    identify and classify the individual errors, summarize the failure, and
    judge whether the errors are automatically fixable.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        """Initialize the agent with an LLM client.

        Preconditions:
            llm_client must not be None.
        Postconditions:
            self.llm holds the given client; self._model holds the strands
            model resolved for the "devops" agent key.
        """
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = resolve_strands_model(
            llm_client, agent_key="devops", get_strands_model_fn=get_strands_model
        )

    def run(self, input_data: IaCDebugInput) -> IaCDebugOutput:
        """Classify the errors in a failed IaC execution and judge fixability.

        Builds a context prompt from the tool name, command, execution output,
        and up to 5 IaC artifact files, then asks the LLM to classify the
        errors present.

        Preconditions:
            input_data is a valid IaCDebugInput.
        Postconditions:
            Returns an IaCDebugOutput whose errors list holds one
            IaCExecutionError per classified error (each carrying the raw
            execution output), whose summary describes the failure, and
            whose fixable flag is true only when there is at least one error
            and every error's type is in {"syntax", "validation"} (or the LLM
            explicitly overrides this via its own "fixable" response field).
        """
        artifacts_snippet = ""
        for fname, content in list(input_data.artifacts.items())[:_MAX_ARTIFACTS]:
            artifacts_snippet += f"\n### {fname} ###\n{content[:_MAX_ARTIFACT_CHARS]}\n"

        context = (
            f"Tool: {input_data.tool_name}\n"
            f"Command: {input_data.command}\n\n"
            f"--- Execution Output ---\n{input_data.execution_output}\n\n"
            f"--- Artifacts ---\n{artifacts_snippet}\n"
        )

        data = complete_json_with_continuation(
            self._model,
            INFRA_DEBUG_PROMPT + "\n\n---\n\n" + context,
            temperature=0.1,
            think=True,
        )

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
