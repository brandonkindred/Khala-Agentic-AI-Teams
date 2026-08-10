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
from llm_service.strands_model import resolve_strands_model
from software_engineering_team.shared.persona_agent_base import run_structured_persona
from software_engineering_team.shared.security_service import derive_approved

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
    QA expert that reviews code for bugs, runs live testing, and ensures
    adequate integration tests. Reports bugs for the coding agent to fix —
    never patches them itself (see ``QAOutput.bugs_found``).

    Approval semantics (note for consumers of ``QAOutput.approved``): the agent
    re-derives ``approved`` rather than trusting the LLM's raw flag, and the rule
    is mode-dependent. In the bug-review modes (``default``/``fix_build``/
    ``write_tests``) ``approved`` means **"no critical/high bugs"** — a
    long-standing behavior, not a holistic LLM verdict, so code with only
    medium/low issues can still be approved. In ``acceptance_evidence`` mode
    ``approved`` is the LLM verdict AND the absence of any failing quality gate.
    """

    def __init__(self, llm_client=None) -> None:
        """Resolve the review model and cache one system prompt per request mode.

        Preconditions: ``llm_client`` is ``None``, an ``LLMClient``, or a
        Strands ``Model``.
        Postconditions: ``self._model`` is a usable Strands model, and
        ``self._system_prompts`` maps each of the four supported request
        modes (``default``, ``fix_build``, ``write_tests``,
        ``acceptance_evidence``) to a persona string.
        """
        self._model = resolve_strands_model(
            llm_client, agent_key="qa", get_strands_model_fn=get_strands_model
        )
        # One system prompt per request mode. A fresh Strands Agent is
        # constructed per ``run()`` call in :meth:`run` using the selected
        # persona; see the note there for why agents are not cached.
        self._system_prompts: Dict[str, str] = {
            "default": QA_PROMPT,
            "fix_build": QA_PROMPT + "\n\n" + QA_PROMPT_FIX_BUILD,
            "write_tests": QA_PROMPT + "\n\n" + QA_PROMPT_WRITE_TESTS,
            # Standalone persona: acceptance_evidence is release validation, not
            # bug review, so it must NOT inherit QA_PROMPT's bug-review directions
            # (which would contradict "do NOT review source code for bugs").
            "acceptance_evidence": QA_PROMPT_ACCEPTANCE_EVIDENCE,
        }

    def run(self, input_data: QAInput) -> QAOutput:
        """Review code for bugs and produce integration tests. Reports bugs; does not fix them."""
        mode = self._select_mode(input_data)
        logger.info(
            "QA: reviewing %s chars of code, mode=%s",
            len(input_data.code or ""),
            mode,
        )

        user_prompt = self._build_user_prompt(input_data)

        def _fallback(exc: Exception) -> QAOutput:
            logger.warning("QA: structured_output failed (%s); returning fallback", exc)
            # In acceptance_evidence mode a parse failure must still fail closed with
            # an explicit failing gate, since this mode's consumers block on gate
            # values rather than reading ``approved`` directly.
            quality_gates = {"acceptance_evidence": "fail"} if mode == "acceptance_evidence" else {}
            return QAOutput(
                bugs_found=[],
                approved=False,
                quality_gates=quality_gates,
                summary=f"QA could not parse model response: {exc}",
                integration_tests="",
                unit_tests="",
                test_plan="",
                live_test_notes="",
                readme_content="",
                suggested_commit_message="",
            )

        def _finalize(result: QAOutput) -> QAOutput:
            # Re-derive ``approved``. The rule differs by mode and the two must not
            # be unified: in acceptance_evidence mode a failing quality gate is the
            # blocking signal (mirroring the former DevOpsTestValidationAgent),
            # whereas the bug-review modes block on critical/high bug severities.
            # Only applied to a genuine model result — the fallback above is
            # already a final, safe ``approved=False``.
            if mode == "acceptance_evidence":
                gates_lower = {
                    k: (v or "").strip().lower() for k, v in result.quality_gates.items()
                }
                result.approved = bool(result.approved) and not any(
                    v == "fail" for v in gates_lower.values()
                )
                # Fail closed: an unapproved verdict with no explicit failing gate
                # (e.g. an LLM verdict of false but an all-pass/empty gate map) must
                # still surface a blocking gate, since consumers key off gate values.
                if not result.approved and not any(v == "fail" for v in gates_lower.values()):
                    result.quality_gates["acceptance_evidence"] = "fail"
            else:
                result.approved = derive_approved(result.bugs_found, llm_approved=None)
            return result

        # A fresh Strands Agent per call. Strands' Agent accumulates
        # message history across invocations; reusing the same instance
        # breaks the forced-tool-choice mechanism used by
        # ``structured_output_model`` on the second call. Construction is
        # cheap — it just wraps the cached model + system_prompt.
        result = run_structured_persona(
            model=self._model,
            system_prompt=self._system_prompts[mode],
            user_prompt=user_prompt,
            output_model=QAOutput,
            fallback_factory=_fallback,
            agent_factory=Agent,
            on_success=_finalize,
        )

        logger.info(
            "QA: done, %s issues found, approved=%s",
            len(result.bugs_found),
            result.approved,
        )
        return result

    @staticmethod
    def _select_mode(input_data: QAInput) -> str:
        """Choose the request mode for this run.

        Preconditions: ``input_data`` is a valid :class:`QAInput`.
        Postconditions: returns one of ``"default"``, ``"fix_build"``,
        ``"write_tests"``, or ``"acceptance_evidence"``. ``"fix_build"`` is
        returned only when ``request_mode`` is ``"fix_build"`` AND
        ``build_errors`` is non-empty; a ``"fix_build"`` request with no
        build errors falls back to ``"default"``.
        """
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
            # Render the criteria/results as readable structured text rather than
            # Python's list/dict ``repr`` so the model parses them cleanly.
            criteria_text = (
                "\n".join(f"{i + 1}. {c}" for i, c in enumerate(input_data.acceptance_criteria))
                or "(none provided)"
            )
            tool_results_text = (
                "\n".join(
                    f"- {group}: " + ", ".join(f"{k}={v}" for k, v in results.items())
                    for group, results in input_data.tool_results.items()
                )
                or "(none provided)"
            )
            return (
                "Interpret the tool/test results below and map the evidence back to the "
                "acceptance criteria. Produce structured JSON with fields: approved, "
                "quality_gates, acceptance_trace, validation_evidence, summary.\n\n"
                f"**Acceptance criteria:**\n{criteria_text}\n\n"
                f"**Tool results:**\n{tool_results_text}"
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
