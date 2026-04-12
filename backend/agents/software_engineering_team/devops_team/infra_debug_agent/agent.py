"""Infrastructure Debug agent -- classifies IaC execution errors."""

from __future__ import annotations

import json
import logging

from strands import Agent

from llm_service import LLMClient

from .models import IaCDebugInput, IaCDebugOutput, IaCExecutionError
from .prompts import INFRA_DEBUG_PROMPT

logger = logging.getLogger(__name__)

_FIXABLE_TYPES = frozenset({"syntax", "validation"})


class InfraDebugAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = llm_client

    def run(self, input_data: IaCDebugInput) -> IaCDebugOutput:
        artifacts_snippet = ""
        for fname, content in list(input_data.artifacts.items())[:5]:
            artifacts_snippet += f"\n### {fname} ###\n{content[:2000]}\n"

        context = (
            f"Tool: {input_data.tool_name}\n"
            f"Command: {input_data.command}\n\n"
            f"--- Execution Output ---\n{input_data.execution_output[:4000]}\n\n"
            f"--- Artifacts ---\n{artifacts_snippet}\n"
        )

        data = json.loads(str(Agent(model=self._model)(
            INFRA_DEBUG_PROMPT + "\n\n---\n\n" + context,
            temperature=0.1,
            think=True,
        )).strip())

        errors = []
        for err_data in data.get("errors") or []:
            errors.append(
                IaCExecutionError(
                    error_type=err_data.get("error_type", "unknown"),
                    tool=err_data.get("tool", input_data.tool_name),
                    file_path=err_data.get("file_path"),
                    line_number=err_data.get("line_number"),
                    error_message=err_data.get("error_message", ""),
                    raw_output=input_data.execution_output[:500],
                )
            )

        fixable = bool(errors) and all(e.error_type in _FIXABLE_TYPES for e in errors)

        return IaCDebugOutput(
            errors=errors,
            summary=data.get("summary", ""),
            fixable=data.get("fixable", fixable),
        )
