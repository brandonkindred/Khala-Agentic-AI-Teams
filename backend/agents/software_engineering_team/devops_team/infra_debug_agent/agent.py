"""Infrastructure Debug agent -- classifies IaC execution errors."""

from __future__ import annotations

import logging

from llm_service import LLMClient, get_strands_model
from llm_service.strands_model import model_fingerprint, resolve_strands_model
from software_engineering_team.shared.llm import complete_json_with_continuation
from software_engineering_team.shared.llm_response_cache import (
    build_cache_key,
    cache_capacity,
    clear_cache,
    get_cached_result,
    set_cached_result,
)

from .models import IaCDebugInput, IaCDebugOutput, IaCExecutionError
from .prompts import INFRA_DEBUG_PROMPT

logger = logging.getLogger(__name__)

_FIXABLE_TYPES = frozenset({"syntax", "validation"})

# Shared review-result cache: keyed on the whole IaCDebugInput content plus
# the resolved model. See ``shared.llm_response_cache`` for the shared
# implementation.
_CACHE_NAMESPACE = "devops:infra_debug:v1"
_CACHE_ENV_VAR = "DEVOPS_INFRA_DEBUG_CACHE_SIZE"
_CACHE_DEFAULT_SIZE = 128


def clear_infra_debug_cache() -> None:
    """Drop every cached infra debug result. Intended for test teardown."""
    clear_cache(_CACHE_NAMESPACE, log_prefix="InfraDebugAgent")


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
        artifacts_snippet = "".join(
            f"\n### {fname} ###\n{content[:_MAX_ARTIFACT_CHARS]}\n"
            for fname, content in list(input_data.artifacts.items())[:_MAX_ARTIFACTS]
        )

        context = (
            f"Tool: {input_data.tool_name}\n"
            f"Command: {input_data.command}\n\n"
            f"--- Execution Output ---\n{input_data.execution_output}\n\n"
            f"--- Artifacts ---\n{artifacts_snippet}\n"
        )

        capacity = cache_capacity(_CACHE_ENV_VAR, _CACHE_DEFAULT_SIZE)
        cache_key = None
        if capacity > 0:
            cache_key = build_cache_key(input_data, model_fingerprint(self._model))
            cached = get_cached_result(
                _CACHE_NAMESPACE, cache_key, IaCDebugOutput, log_prefix="InfraDebugAgent"
            )
            if cached is not None:
                return cached

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

        result = IaCDebugOutput(
            errors=errors,
            summary=data.get("summary", ""),
            fixable=data.get("fixable", fixable),
        )

        if cache_key is not None:
            set_cached_result(
                _CACHE_NAMESPACE, cache_key, result, capacity, log_prefix="InfraDebugAgent"
            )

        return result
