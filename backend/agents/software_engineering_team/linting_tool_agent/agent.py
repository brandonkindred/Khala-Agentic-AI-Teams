"""Linting Tool Agent: detects, runs, and fixes lint violations in three phases."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from build_fix_specialist.models import CodeEdit

from llm_service import LLMClient

from .linter_runner import detect_linter, execute_linter
from .models import LintIssue, LintToolInput, LintToolOutput
from .prompts import LINT_FIX_PROMPT

logger = logging.getLogger(__name__)

MAX_AFFECTED_FILES = 15
MAX_AFFECTED_CODE_CHARS = 12_000
MAX_ISSUES_FOR_LLM = 30


class LintingToolAgent:
    """Tool agent that performs code linting review and cleanup.

    Operates in three phases:
        1. **Planning** -- detect which linter the project uses and scope the check.
        2. **Execution** -- run the linter subprocess and parse structured issues.
        3. **Review** -- use an LLM to produce minimal ``CodeEdit`` fixes for violations.

    Invariants:
        - ``self.llm`` is always a valid ``LLMClient``.
        - ``run()`` never modifies the repository; callers apply returned edits.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: LintToolInput) -> LintToolOutput:
        """Execute the full lint pipeline: plan -> execute -> review.

        Preconditions:
            - ``input_data.repo_path`` is a valid directory.
        Postconditions:
            - Returns ``LintToolOutput`` with ``execution_result.success == True`` when
              no violations are found (review phase is skipped).
            - When violations exist, ``edits`` contains concrete fixes and/or
              ``linter_issues`` lists the raw issues for the caller to act on.
        """
        repo_path = Path(input_data.repo_path).resolve()

        # Phase 1: Planning
        plan = detect_linter(repo_path, input_data.agent_type)
        logger.info(
            "[%s] Lint planning: linter=%s, command=%s, config=%s",
            input_data.task_id or "lint",
            plan.linter_name,
            " ".join(plan.linter_command),
            plan.config_file or "(default)",
        )

        # Phase 2: Execution
        execution_result = execute_linter(plan, repo_path, input_data.agent_type)
        logger.info(
            "[%s] Lint execution: success=%s, issues=%d",
            input_data.task_id or "lint",
            execution_result.success,
            execution_result.issue_count,
        )

        if execution_result.success:
            return LintToolOutput(
                plan=plan,
                execution_result=execution_result,
                summary="Lint passed -- no violations found",
            )

        # Phase 3: Review (LLM fix generation)
        affected_code = self._read_affected_files(repo_path, execution_result.issues)
        edits = self._generate_fixes(execution_result.issues, affected_code)

        summary = (
            f"Found {execution_result.issue_count} lint issue(s), "
            f"produced {len(edits)} fix edit(s)"
        )
        logger.info("[%s] Lint review: %s", input_data.task_id or "lint", summary)

        return LintToolOutput(
            plan=plan,
            execution_result=execution_result,
            edits=edits,
            linter_issues=execution_result.issues,
            summary=summary,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_affected_files(repo_path: Path, issues: List[LintIssue]) -> str:
        """Read and concatenate content of files mentioned in lint issues.

        De-duplicates file paths and truncates total output to stay within
        context-window limits.
        """
        seen_files: Dict[str, str] = {}
        total_chars = 0
        for issue in issues:
            if issue.file_path in seen_files:
                continue
            if len(seen_files) >= MAX_AFFECTED_FILES:
                break
            file_abs = repo_path / issue.file_path
            if not file_abs.is_file():
                continue
            try:
                content = file_abs.read_text(encoding="utf-8", errors="replace")
                if total_chars + len(content) > MAX_AFFECTED_CODE_CHARS:
                    remaining = MAX_AFFECTED_CODE_CHARS - total_chars
                    if remaining > 200:
                        content = content[:remaining] + "\n... [truncated]"
                    else:
                        break
                seen_files[issue.file_path] = content
                total_chars += len(content)
            except Exception:
                continue

        parts: List[str] = []
        for fpath, content in seen_files.items():
            parts.append(f"### {fpath}\n```\n{content}\n```")
        return "\n\n".join(parts)

    def _generate_fixes(
        self,
        issues: List[LintIssue],
        affected_code: str,
    ) -> List[CodeEdit]:
        """Use the LLM to produce ``CodeEdit`` objects that fix lint violations."""
        issues_block = "\n".join(
            f"- {i.file_path}:{i.line}:{i.column} [{i.rule}] {i.message}"
            for i in issues[:MAX_ISSUES_FOR_LLM]
        )

        prompt = (
            LINT_FIX_PROMPT
            + "\n\n---\n\n"
            + "**Lint violations to fix:**\n"
            + issues_block
            + "\n\n**Affected files (current code):**\n"
            + affected_code
        )

        try:
            data: Dict[str, Any] = self.llm.complete_json(prompt, temperature=0.0)
        except Exception as err:
            logger.warning("Lint fix LLM call failed (non-blocking): %s", err)
            return []

        edits: List[CodeEdit] = []
        for entry in data.get("edits") or []:
            if (
                isinstance(entry, dict)
                and entry.get("file_path")
                and "old_text" in entry
                and "new_text" in entry
            ):
                edits.append(
                    CodeEdit(
                        file_path=entry["file_path"],
                        line_start=entry.get("line_start"),
                        line_end=entry.get("line_end"),
                        old_text=entry["old_text"],
                        new_text=entry["new_text"],
                    )
                )

        logger.info("Lint fix LLM produced %d edit(s)", len(edits))
        return edits
