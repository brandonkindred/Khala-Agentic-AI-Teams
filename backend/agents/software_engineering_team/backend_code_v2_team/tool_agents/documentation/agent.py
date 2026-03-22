"""Documentation tool agent for backend-code-v2: reviews and updates documentation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, List, Optional

from ...models import (
    Microtask,
    ReviewIssue,
    ToolAgentInput,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)
from ...output_templates import parse_problem_solving_single_issue_template, parse_review_template
from ...prompts import (
    DOCUMENTATION_MICROTASK_PROMPT,
    DOCUMENTATION_PROBLEM_SOLVE_PROMPT,
    DOCUMENTATION_REVIEW_PROMPT,
    JAVA_CONVENTIONS,
    PYTHON_CONVENTIONS,
)

if TYPE_CHECKING:
    from llm_service import LLMClient

logger = logging.getLogger(__name__)

MAX_DOC_CODE_CHARS = 15_000
MAX_RELEVANT_CODE_CHARS = 10_000


def _relevant_code_for_issue(issue: ReviewIssue, current_files: Dict[str, str]) -> str:
    """Return code context for a single issue: prefer issue's file, else first files."""
    if issue.file_path and issue.file_path in current_files:
        content = current_files[issue.file_path]
        if len(content) <= MAX_RELEVANT_CODE_CHARS:
            return f"--- {issue.file_path} ---\n{content}"
        return f"--- {issue.file_path} ---\n{content[:MAX_RELEVANT_CODE_CHARS]}\n... [truncated]"
    parts: List[str] = []
    total = 0
    for path, content in list(current_files.items())[:10]:
        chunk = f"--- {path} ---\n{content}\n"
        if total + len(chunk) > MAX_RELEVANT_CODE_CHARS:
            remaining = MAX_RELEVANT_CODE_CHARS - total
            if remaining > 200:
                chunk = f"--- {path} ---\n{content[:remaining]}\n... [truncated]"
                parts.append(chunk)
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts) if parts else "(no code)"


def _extract_doc_files(files: Dict[str, str]) -> Dict[str, str]:
    """Extract documentation-related files (README, docs, etc.)."""
    doc_files: Dict[str, str] = {}
    doc_patterns = (
        "readme", "contributing", "changelog", "license", "docs/", 
        "documentation", ".md", "api.md", "usage.md"
    )
    for path, content in files.items():
        path_lower = path.lower()
        if any(pattern in path_lower for pattern in doc_patterns):
            doc_files[path] = content
    return doc_files


class DocumentationToolAgent:
    """Documentation tool agent: reviews documentation completeness and updates docs."""

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("Documentation: microtask %s (execute)", inp.microtask.id)
        return ToolAgentOutput(summary="Documentation execute — no direct changes applied.")

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=[
                "Include README updates for new features.",
                "Document API changes and new endpoints.",
                "Add docstrings for all public functions, classes, and methods.",
                "Update CONTRIBUTORS.md if applicable.",
            ],
            summary="Documentation planning.",
        )

    def document_microtask(
        self,
        microtask: Microtask,
        files: Dict[str, str],
        task_description: str,
    ) -> ToolAgentPhaseOutput:
        """Update documentation for a single completed microtask.
        
        This method is called after each microtask passes review, to update
        inline documentation (docstrings, comments) for the code that was just added.
        """
        if not self.llm:
            return ToolAgentPhaseOutput(summary="Documentation update skipped (no LLM).")
        
        code_text = "\n\n".join(
            f"--- {p} ---\n{c}" for p, c in list(files.items())[:15]
        )[:MAX_DOC_CODE_CHARS]
        
        if not code_text.strip():
            return ToolAgentPhaseOutput(summary="Documentation update skipped (no code).")
        
        prompt = DOCUMENTATION_MICROTASK_PROMPT.format(
            microtask_title=microtask.title or microtask.id,
            microtask_description=microtask.description or "N/A",
            task_description=task_description or "N/A",
            code=code_text,
        )
        
        try:
            raw = self.llm.complete_text(prompt)
        except Exception as e:
            logger.warning("Documentation microtask LLM call failed: %s", e)
            return ToolAgentPhaseOutput(summary="Documentation update failed (LLM error).")
        
        parsed = parse_problem_solving_single_issue_template(raw)
        updated_files = parsed.get("files") or {}
        
        return ToolAgentPhaseOutput(
            files=updated_files,
            summary=f"Documentation: updated {len(updated_files)} file(s) for microtask {microtask.id}.",
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Review all documentation for completeness and consistency.
        
        Checks:
        - README.md is up-to-date with features and usage
        - All public functions/classes have docstrings
        - API documentation matches implementation
        - CONTRIBUTORS.md exists if multiple contributors
        - Code comments explain non-obvious logic
        """
        if not self.llm:
            return ToolAgentPhaseOutput(summary="Documentation review skipped (no LLM).")
        
        doc_files = _extract_doc_files(inp.current_files)
        code_text = "\n\n".join(
            f"--- {p} ---\n{c}" for p, c in list(inp.current_files.items())[:20]
        )[:MAX_DOC_CODE_CHARS]
        
        doc_text = "\n\n".join(
            f"--- {p} ---\n{c}" for p, c in doc_files.items()
        )[:MAX_DOC_CODE_CHARS // 2] if doc_files else "(no documentation files found)"
        
        if not code_text.strip():
            return ToolAgentPhaseOutput(summary="Documentation review skipped (no code).")
        
        prompt = DOCUMENTATION_REVIEW_PROMPT.format(
            task_title=inp.task_title or "N/A",
            task_description=inp.task_description or "N/A",
            documentation=doc_text,
            code=code_text,
        )
        
        try:
            raw = self.llm.complete_text(prompt)
        except Exception as e:
            logger.warning("Documentation review LLM call failed: %s", e)
            return ToolAgentPhaseOutput(summary="Documentation review failed (LLM error).")
        
        data = parse_review_template(raw)
        issues: List[ReviewIssue] = []
        for item in data.get("issues") or []:
            if isinstance(item, dict):
                issues.append(
                    ReviewIssue(
                        source="documentation",
                        severity=item.get("severity", "medium"),
                        description=item.get("description", ""),
                        file_path=item.get("file_path", ""),
                        recommendation=item.get("recommendation", ""),
                    )
                )
        
        return ToolAgentPhaseOutput(
            issues=issues,
            summary=f"Documentation review: {len(issues)} issue(s) found.",
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix documentation issues one at a time.
        
        Only fixes issues with source 'documentation' or 'tool_documentation'.
        """
        if not self.llm:
            return ToolAgentPhaseOutput(summary="Documentation problem_solve skipped (no LLM).")
        
        doc_issues = [
            i for i in inp.review_issues
            if (i.source or "").strip() in ("documentation", "tool_documentation")
        ]
        
        if not doc_issues:
            return ToolAgentPhaseOutput(summary="No documentation issues to fix.")
        
        lang = (inp.language or "python").strip().lower()
        language_conventions = JAVA_CONVENTIONS if lang == "java" else PYTHON_CONVENTIONS
        
        merged = dict(inp.current_files)
        fixed_count = 0
        
        for issue in doc_issues:
            relevant_code = _relevant_code_for_issue(issue, merged)
            
            prompt = DOCUMENTATION_PROBLEM_SOLVE_PROMPT.format(
                language_conventions=language_conventions,
                source=issue.source or "documentation",
                severity=issue.severity or "medium",
                description=issue.description or "",
                file_path=issue.file_path or "N/A",
                recommendation=issue.recommendation or "Fix the documentation issue.",
                current_code=relevant_code,
            )
            
            try:
                raw = self.llm.complete_text(prompt)
            except Exception as e:
                logger.warning("Documentation fix for issue %s failed: %s", (issue.description or "")[:50], e)
                continue
            
            parsed = parse_problem_solving_single_issue_template(raw)
            fixed_files = parsed.get("files") or {}
            if fixed_files:
                merged.update(fixed_files)
                fixed_count += 1
        
        return ToolAgentPhaseOutput(
            files=merged,
            summary=f"Documentation: fixed {fixed_count} of {len(doc_issues)} issue(s).",
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Documentation deliver.")
