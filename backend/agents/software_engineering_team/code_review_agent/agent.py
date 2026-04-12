"""Code Review agent: reviews code against spec, standards, and conventions.

Built on the AWS Strands Agents SDK via ``llm_service.LLMClientModel``. The
``LLMClient`` passed in at construction time is wrapped into a Strands
``Model`` so the agent inherits retries, per-agent model routing, telemetry,
and the dummy-client path for tests.

Two code paths remain:

1. **Small code** (fits inside a single model context) — one Strands Agent
   call with ``structured_output_model=CodeReviewOutput``.
2. **Large code** — compaction, then delegation to ``run_coordinator`` which
   splits the code into chunks and dispatches each to ``ChunkReviewAgent``
   (itself a Strands-backed agent).
"""

from __future__ import annotations

import logging

from strands import Agent

from llm_service import LLMClient, LLMClientModel, compact_text
from software_engineering_team.shared.context_sizing import (
    compute_code_review_chunk_chars,
    compute_code_review_total_chars,
)

from .coordinator import run_coordinator
from .models import CodeReviewInput, CodeReviewIssue, CodeReviewOutput
from .prompts import CODE_REVIEW_PROMPT

logger = logging.getLogger(__name__)


class CodeReviewAgent:
    """
    Code review agent that reviews code produced by coding agents
    against the project specification, coding standards, and conventions.

    Returns approval or a list of issues that must be resolved.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        # ``self.llm`` is retained for ``compact_text`` calls and for
        # ``run_coordinator`` (which still needs raw LLMClient for
        # context-sizing and compaction of spec/architecture excerpts).
        self.llm = llm_client
        self._model = LLMClientModel(
            llm_client,
            agent_key="code_review",
            temperature=0.1,
            think=True,
        )

    def run(self, input_data: CodeReviewInput) -> CodeReviewOutput:
        """Review code and return approval or issues."""
        code = input_data.code or ""
        single_call_limit = compute_code_review_chunk_chars(self.llm)
        if len(code) > single_call_limit:
            logger.info(
                "CodeReview: code size %s exceeds single-call limit %s "
                "(model context), using coordinator",
                len(code),
                single_call_limit,
            )
            max_total = compute_code_review_total_chars(self.llm)
            code = compact_text(code, max_total, self.llm, "code for review")
            if code != input_data.code:
                input_data = CodeReviewInput(
                    code=code,
                    spec_content=input_data.spec_content,
                    task_description=input_data.task_description,
                    task_requirements=input_data.task_requirements,
                    acceptance_criteria=input_data.acceptance_criteria,
                    language=input_data.language,
                    architecture=input_data.architecture,
                    existing_codebase=input_data.existing_codebase,
                )
            return run_coordinator(self.llm, input_data)

        max_total = compute_code_review_total_chars(self.llm)
        code = compact_text(code, max_total, self.llm, "code for review")

        logger.info(
            "CodeReview: reviewing %s chars of %s code | task=%s | has_spec=%s "
            "| has_architecture=%s | acceptance_criteria=%s",
            len(code),
            input_data.language,
            input_data.task_description[:80] if input_data.task_description else "",
            bool(input_data.spec_content),
            input_data.architecture is not None,
            len(input_data.acceptance_criteria),
        )

        user_prompt = self._build_user_prompt(input_data, code)

        # A fresh Strands Agent per call — reusing the same instance across
        # calls breaks structured_output forced-tool-choice on the second
        # call (Strands accumulates message history).
        agent = Agent(model=self._model, system_prompt=CODE_REVIEW_PROMPT)

        try:
            agent_result = agent(user_prompt, structured_output_model=CodeReviewOutput)
            result = agent_result.structured_output
            if not isinstance(result, CodeReviewOutput):
                raise TypeError(
                    f"Expected CodeReviewOutput, got {type(result).__name__ if result else 'None'}"
                )
        except Exception as exc:  # noqa: BLE001 — LLM/validation errors must not crash the run
            logger.warning("CodeReview: structured_output failed (%s); returning fallback", exc)
            return CodeReviewOutput(
                approved=False,
                issues=[],
                summary=f"Code review failed: {exc}",
                spec_compliance_notes="",
                suggested_commit_message="",
            )

        result = self._reconcile_approval(result)

        logger.info(
            "CodeReview: done, approved=%s, issues=%s",
            result.approved,
            len(result.issues),
        )
        return result

    @staticmethod
    def _reconcile_approval(result: CodeReviewOutput) -> CodeReviewOutput:
        """Apply the safety-net policy.

        The contract is: **never** return ``approved=False`` with no
        actionable issues, because the coding agent would have nothing to
        fix and we'd loop forever. Rules:

        1. Only critical/high severities are truly blocking. If the LLM
           flagged approved=False but all issues are minor/nit, auto-approve.
        2. If the LLM said approved=False with zero issues but a non-empty
           summary, synthesize a high-severity issue so the fix is actionable.
        3. If the LLM said approved=False with zero issues AND zero summary,
           auto-approve (nothing to act on).
        """
        critical_or_high = [i for i in result.issues if i.severity in ("critical", "high")]
        raw_approved = result.approved
        result.approved = raw_approved and len(critical_or_high) == 0

        if not result.approved and not critical_or_high:
            if result.issues:
                logger.info(
                    "CodeReview: overriding to approved=True "
                    "(only %s minor/nit issues, no critical/high)",
                    len(result.issues),
                )
                result.approved = True
            elif result.summary and result.summary.strip():
                logger.warning(
                    "CodeReview: LLM returned approved=False with 0 issues -- "
                    "synthesizing issue from summary: %s",
                    result.summary[:200],
                )
                synthesized = CodeReviewIssue(
                    severity="high",
                    category="general",
                    file_path="",
                    description=f"Code review rejected: {result.summary}",
                    suggestion=(
                        "Address the concerns described in the review summary. "
                        "Ensure the code meets all acceptance criteria and follows project conventions."
                    ),
                )
                result.issues.append(synthesized)
                # approved stays False — caller sees the synthesized issue and can act
            else:
                logger.warning(
                    "CodeReview: LLM returned approved=False with no issues and no summary -- "
                    "auto-approving (no actionable feedback to give coding agent)"
                )
                result.approved = True

        return result

    @staticmethod
    def _build_user_prompt(input_data: CodeReviewInput, code: str) -> str:
        """Assemble the user-facing prompt.

        The persona (``CODE_REVIEW_PROMPT``) lives on the Strands ``Agent``'s
        system prompt. The user prompt carries the code under review plus
        a schema hint that includes "senior code reviewer" and "issues" /
        "approved" — both required by ``DummyLLMClient.complete_json`` to
        route to the code-review stub in tests. See
        ``llm_service/README.md`` "Migration rule: keep pattern anchors in
        the user prompt".
        """
        parts = [
            "Acting as a senior code reviewer, review the code below and produce "
            "structured JSON with fields: approved, issues, summary, "
            "spec_compliance_notes, suggested_commit_message. Each issue must "
            "include severity, category, file_path, description, and suggestion.",
            "",
            f"**Language:** {input_data.language}",
            f"**Task description:** {input_data.task_description}",
        ]

        if input_data.task_requirements:
            parts.extend(["", "**Task requirements:**", input_data.task_requirements])

        if input_data.acceptance_criteria:
            parts.extend(
                [
                    "",
                    "**Acceptance criteria (code MUST meet all of these):**",
                    *[f"- {c}" for c in input_data.acceptance_criteria],
                ]
            )

        if input_data.spec_content:
            parts.extend(
                [
                    "",
                    "**Project specification (source of truth for the application):**",
                    "---",
                    input_data.spec_content,
                    "---",
                ]
            )

        if input_data.architecture:
            parts.extend(
                [
                    "",
                    "**Architecture:**",
                    input_data.architecture.overview,
                ]
            )

        if input_data.existing_codebase:
            parts.extend(
                [
                    "",
                    "**Existing codebase (before the agent's changes):**",
                    input_data.existing_codebase,
                ]
            )

        parts.extend(
            [
                "",
                "**Code to review:**",
                "```",
                code,
                "```",
            ]
        )

        return "\n".join(parts)
