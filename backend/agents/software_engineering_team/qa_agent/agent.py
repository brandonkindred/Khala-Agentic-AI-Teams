"""QA Expert agent: bug detection, integration tests, live testing.

Built on the AWS Strands Agents SDK via ``llm_service.get_strands_model``. The
model returned by ``get_strands_model`` is passed to a Strands ``Agent`` so the
agent inherits retries, per-agent model routing, telemetry, and the
dummy-client path for tests.

The agent supports four request modes — ``default``, ``fix_build``,
``write_tests``, and ``acceptance_evidence`` — each with a distinct system
prompt. Because Strands ``Agent`` fixes its ``system_prompt`` at construction
time, we hold one system prompt per mode and build a fresh ``Agent`` with the
selected persona at call time.

The ``acceptance_evidence`` mode absorbs the former DevOps test-validation
surface: it maps tool/test results to acceptance criteria and emits
``quality_gates``/``acceptance_trace``/``validation_evidence``. It runs through
the same ``structured_output_model`` path as the other modes (the previous
raw-JSON + ``think=True`` implementation is an internal detail, not part of the
output contract).
"""

from __future__ import annotations

import logging
from typing import Dict

from strands import Agent

from llm_service import get_strands_model

from .models import QAInput, QAOutput
from .prompts import (
    QA_PROMPT,
    QA_PROMPT_ACCEPTANCE_EVIDENCE,
    QA_PROMPT_FIX_BUILD,
    QA_PROMPT_WRITE_TESTS,
)

logger = logging.getLogger(__name__)


class QAExpertAgent:
    """
    QA expert that reviews code for bugs, fixes them, runs live testing,
    and ensures adequate integration tests.
    """

    def __init__(self, llm_client=None) -> None:
        from strands.models.model import Model as _StrandsModel

        if llm_client is not None and isinstance(llm_client, _StrandsModel):
            self._model = llm_client
        else:
            self._model = get_strands_model("qa")
        # One system prompt per request mode. A fresh Strands Agent is
        # constructed per ``run()`` call in :meth:`run` using the selected
        # persona; see the note there for why agents are not cached.
        self._system_prompts: Dict[str, str] = {
            "default": QA_PROMPT,
            "fix_build": QA_PROMPT + "\n\n" + QA_PROMPT_FIX_BUILD,
            "write_tests": QA_PROMPT + "\n\n" + QA_PROMPT_WRITE_TESTS,
            "acceptance_evidence": QA_PROMPT + "\n\n" + QA_PROMPT_ACCEPTANCE_EVIDENCE,
        }

    def run(self, input_data: QAInput) -> QAOutput:
        """Review code, fix bugs, and produce integration tests."""
        mode = self._select_mode(input_data)
        logger.info(
            "QA: reviewing %s chars of code, mode=%s",
            len(input_data.code or ""),
            mode,
        )

        user_prompt = self._build_user_prompt(input_data)

        # A fresh Strands Agent per call. Strands' Agent accumulates
        # message history across invocations; reusing the same instance
        # breaks the forced-tool-choice mechanism used by
        # ``structured_output_model`` on the second call. Construction is
        # cheap — it just wraps the cached model + system_prompt.
        agent = Agent(model=self._model, system_prompt=self._system_prompts[mode])

        try:
            agent_result = agent(user_prompt, structured_output_model=QAOutput)
            result = agent_result.structured_output
            if not isinstance(result, QAOutput):
                raise TypeError(
                    f"Expected QAOutput, got {type(result).__name__ if result else 'None'}"
                )
        except Exception as exc:  # noqa: BLE001 — LLM/validation errors must not crash the run
            logger.warning("QA: structured_output failed (%s); returning fallback", exc)
            return QAOutput(
                bugs_found=[],
                approved=False,
                summary=f"QA could not parse model response: {exc}",
                integration_tests="",
                unit_tests="",
                test_plan="",
                live_test_notes="",
                readme_content="",
                suggested_commit_message="",
            )

        # Re-derive ``approved``. The rule differs by mode and the two must not
        # be unified: in acceptance_evidence mode a failing quality gate is the
        # blocking signal (mirroring the former DevOpsTestValidationAgent),
        # whereas the bug-review modes block on critical/high bug severities.
        if mode == "acceptance_evidence":
            result.approved = bool(result.approved) and not any(
                (v or "").lower() == "fail" for v in result.quality_gates.values()
            )
        else:
            critical_or_high = [b for b in result.bugs_found if b.severity in ("critical", "high")]
            result.approved = len(critical_or_high) == 0

        logger.info(
            "QA: done, %s issues found, approved=%s",
            len(result.bugs_found),
            result.approved,
        )
        return result

    @staticmethod
    def _select_mode(input_data: QAInput) -> str:
        if input_data.request_mode == "fix_build" and input_data.build_errors:
            return "fix_build"
        if input_data.request_mode == "write_tests":
            return "write_tests"
        if input_data.request_mode == "acceptance_evidence":
            return "acceptance_evidence"
        return "default"

    @staticmethod
    def _build_user_prompt(input_data: QAInput) -> str:
        """Assemble the user-facing prompt.

        The persona (``QA_PROMPT`` and its mode-specific addendum) lives on
        the Strands ``Agent``'s system prompt, so the user prompt only
        carries the code under review and its context. An explicit schema
        hint (``bugs_found``, ``test_plan``, ...) makes the expected output
        shape unambiguous for the LLM.

        Preconditions: ``input_data`` is a valid :class:`QAInput`.
        Postconditions: returns a non-empty str. In ``acceptance_evidence`` mode
        the prompt names the acceptance-evidence output fields
        (``quality_gates``/``acceptance_trace``/``validation_evidence``) and
        carries the criteria and tool results instead of the code under review.
        """
        if input_data.request_mode == "acceptance_evidence":
            return (
                "Interpret the tool/test results below and map the evidence back to the "
                "acceptance criteria. Produce structured JSON with fields: approved, "
                "quality_gates, acceptance_trace, validation_evidence, summary.\n\n"
                f"**Acceptance criteria:** {input_data.acceptance_criteria}\n"
                f"**Tool results:** {input_data.tool_results}"
            )

        parts = [
            "Review the following code for bugs and produce structured JSON with "
            "fields: bugs_found, test_plan, unit_tests, integration_tests, "
            "readme_content, summary, live_test_notes, suggested_commit_message.",
            "",
            f"**Language:** {input_data.language}",
        ]
        if input_data.task_description:
            parts.append(f"**Task:** {input_data.task_description}")
        parts.extend(
            [
                "**Code to review:**",
                "```",
                input_data.code,
                "```",
            ]
        )
        if input_data.architecture:
            parts.append(f"**Architecture:** {input_data.architecture.overview}")
        if input_data.run_instructions:
            parts.append(f"**Run instructions:** {input_data.run_instructions}")
        if input_data.build_errors:
            parts.append(f"**Build/compiler errors:**\n```\n{input_data.build_errors}\n```")

        return "\n".join(parts)
