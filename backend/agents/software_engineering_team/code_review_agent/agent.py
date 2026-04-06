"""Code Review agent: reviews code against spec, standards, and conventions."""

from __future__ import annotations

import logging

from llm_service import LLMClient, compact_text
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
        self.llm = llm_client

    def run(self, input_data: CodeReviewInput) -> CodeReviewOutput:
        """Review code and return approval or issues."""
        code = input_data.code or ""
        single_call_limit = compute_code_review_chunk_chars(self.llm)
        if len(code) > single_call_limit:
            logger.info(
                "CodeReview: code size %s exceeds single-call limit %s (model context), using coordinator",
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
            "CodeReview: reviewing %s chars of %s code | task=%s | has_spec=%s | has_architecture=%s | acceptance_criteria=%s",
            len(code),
            input_data.language,
            input_data.task_description[:80] if input_data.task_description else "",
            bool(input_data.spec_content),
            input_data.architecture is not None,
            len(input_data.acceptance_criteria),
        )

        context_parts = [
            f"**Language:** {input_data.language}",
            f"**Task description:** {input_data.task_description}",
        ]

        if input_data.task_requirements:
            context_parts.extend(["", "**Task requirements:**", input_data.task_requirements])

        if input_data.acceptance_criteria:
            context_parts.extend(
                [
                    "",
                    "**Acceptance criteria (code MUST meet all of these):**",
                    *[f"- {c}" for c in input_data.acceptance_criteria],
                ]
            )

        if input_data.spec_content:
            context_parts.extend(
                [
                    "",
                    "**Project specification (source of truth for the application):**",
                    "---",
                    input_data.spec_content,
                    "---",
                ]
            )

        if input_data.architecture:
            context_parts.extend(
                [
                    "",
                    "**Architecture:**",
                    input_data.architecture.overview,
                ]
            )

        if input_data.existing_codebase:
            context_parts.extend(
                [
                    "",
                    "**Existing codebase (before the agent's changes):**",
                    input_data.existing_codebase,
                ]
            )

        context_parts.extend(
            [
                "",
                "**Code to review:**",
                "```",
                code,
                "```",
            ]
        )

        prompt = CODE_REVIEW_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.1, think=True)

        # Parse issues
        issues = []
        for issue_data in data.get("issues") or []:
            if isinstance(issue_data, dict) and issue_data.get("description"):
                issues.append(
                    CodeReviewIssue(
                        severity=issue_data.get("severity", "high"),
                        category=issue_data.get("category", "general"),
                        file_path=issue_data.get("file_path", ""),
                        description=issue_data.get("description", ""),
                        suggestion=issue_data.get("suggestion", ""),
                    )
                )

        # Determine approval based on issue severity
        critical_or_high = [i for i in issues if i.severity in ("critical", "high")]
        raw_approved = bool(data.get("approved", False))
        approved = raw_approved and len(critical_or_high) == 0

        # Safety net: handle rejected-with-no-actionable-issues (prevents unresolvable loops)
        if not approved and not critical_or_high:
            summary_text = data.get("summary", "")
            if issues:
                # Has only minor/nit issues -- auto-approve since nothing blocking
                logger.info(
                    "CodeReview: overriding to approved=True (only %s minor/nit issues, no critical/high)",
                    len(issues),
                )
                approved = True
            elif summary_text and summary_text.strip():
                # Rejected with zero issues but has a summary -- synthesize a major issue
                # so the coding agent has something actionable to fix
                logger.warning(
                    "CodeReview: LLM returned approved=False with 0 issues -- "
                    "synthesizing issue from summary: %s",
                    summary_text[:200],
                )
                synthesized = CodeReviewIssue(
                    severity="high",
                    category="general",
                    file_path="",
                    description=f"Code review rejected: {summary_text}",
                    suggestion="Address the concerns described in the review summary. "
                    "Ensure the code meets all acceptance criteria and follows project conventions.",
                )
                issues.append(synthesized)
                critical_or_high.append(synthesized)
            else:
                # No issues AND no summary -- LLM gave no useful feedback, auto-approve
                logger.warning(
                    "CodeReview: LLM returned approved=False with no issues and no summary -- "
                    "auto-approving (no actionable feedback to give coding agent)"
                )
                approved = True

        logger.info(
            "CodeReview: done, approved=%s (raw_llm=%s), issues=%s (critical/high=%s)",
            approved,
            raw_approved,
            len(issues),
            len(critical_or_high),
        )

        return CodeReviewOutput(
            approved=approved,
            issues=issues,
            summary=data.get("summary", ""),
            spec_compliance_notes=data.get("spec_compliance_notes", ""),
            suggested_commit_message=data.get("suggested_commit_message", ""),
        )
