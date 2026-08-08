"""
Shared base for the code-v2 documentation tool agents.

``backend_code_v2_team`` and ``frontend_code_v2_team`` ship a documentation tool
agent that was, until this consolidation, copy-pasted between the two trees: a
``document_microtask`` pass plus a review/single-issue-fix loop over docs. The two
copies differed only in data — the documentation-file patterns, the language
conventions fed to the fix prompt, the plan recommendations, and the
(team-specific) prompts/parsers.

:class:`DocumentationToolAgentBase` captures the behavior; the per-stack concrete
modules stay declarative, setting those values as class attributes. As with the
review family, the LLM is invoked through
:meth:`~software_engineering_team.shared.tool_agent_base.BaseReviewToolAgent._run_agent`,
which resolves ``Agent`` from the *concrete subclass module* — so tests can still
``monkeypatch.setattr(<agent_module>, "Agent", ...)`` and the concrete modules keep
a top-level ``from strands import Agent``.

Unlike the review-lens tool agents (security, testing/QA, accessibility,
performance, UX — which only report findings; see
:class:`~software_engineering_team.shared.tool_agent_base.SingleIssueProblemSolveMixin`),
this class defines its own independent ``problem_solve`` below rather than
opting into that mixin. Documentation's fixes operate on the same class of
artifact (prose/docs) it authors itself in other phases, so it isn't a
reviewer second-guessing another agent's code — it's the same agent
maintaining its own output.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from software_engineering_team.shared.tool_agent_base import (
    BaseReviewToolAgent,
    relevant_code_for_issue,
)
from software_engineering_team.shared.v2_models import ReviewIssue, ToolAgentPhaseOutput


def extract_doc_files(files: Dict[str, str], patterns: Tuple[str, ...]) -> Dict[str, str]:
    """Return the subset of ``files`` whose (lowercased) path matches any pattern.

    Preconditions: ``files`` maps path -> content; ``patterns`` is a tuple of
    lowercase substrings.
    Postconditions: returns a dict containing exactly the entries of ``files``
    whose path contains at least one pattern (insertion order preserved).
    """
    doc_files: Dict[str, str] = {}
    for path, content in files.items():
        path_lower = path.lower()
        if any(pattern in path_lower for pattern in patterns):
            doc_files[path] = content
    return doc_files


class DocumentationToolAgentBase(BaseReviewToolAgent):
    """Documentation tool agent: reviews documentation completeness and updates docs.

    Concrete subclasses set the profile: :attr:`doc_patterns`,
    :attr:`conventions_by_language`, :attr:`plan_recommendations`, the prompts
    (:attr:`microtask_prompt`, :attr:`doc_review_prompt`,
    :attr:`doc_problem_solve_prompt`) and the parser hooks (``_parse_review`` /
    ``_parse_single_issue``).

    Invariants: instance state is limited to ``_model`` and ``llm`` (inherited
    ``__init__``) — so tests that build instances via ``__new__`` and set those
    attributes behave identically to constructed instances.
    """

    name = "Documentation"
    issue_source = "documentation"
    problem_solve_sources = ("documentation", "tool_documentation")
    default_recommendation = "Fix the documentation issue."
    plan_summary = "Documentation planning."

    # Profile (set by concrete subclass).
    doc_patterns: Tuple[str, ...] = ()
    max_doc_code_chars: int = 15_000
    max_relevant_code_chars: int = 10_000
    microtask_prompt: Optional[str] = None
    doc_review_prompt: Optional[str] = None
    doc_problem_solve_prompt: Optional[str] = None
    _parse_review: Optional[Callable[[str], Dict]] = None
    _parse_single_issue: Optional[Callable[[str], Dict]] = None

    def execute(self, inp):
        """Documentation has no direct execute step (docs are updated per phase).

        Postconditions: returns a no-op :class:`ToolAgentOutput` summary.
        """
        from software_engineering_team.shared.v2_models import ToolAgentOutput

        self._logger.info("Documentation: microtask %s (execute)", inp.microtask.id)
        return ToolAgentOutput(summary="Documentation execute — no direct changes applied.")

    def _extract_doc_files(self, files: Dict[str, str]) -> Dict[str, str]:
        """Documentation files within ``files`` per :attr:`doc_patterns`."""
        return extract_doc_files(files, self.doc_patterns)

    def document_microtask(self, microtask, files, task_description) -> ToolAgentPhaseOutput:
        """Update inline documentation for a single completed microtask.

        Called after each microtask passes review, to update inline documentation
        (docstrings/JSDoc, comments) for the code that was just added.

        Preconditions: ``microtask`` exposes ``id``/``title``/``description``;
        ``files`` maps path -> content; the profile prompt/parser hooks are set.
        Postconditions: returns a :class:`ToolAgentPhaseOutput`; ``files`` carries
        only the updated documents (empty when no LLM / no code / LLM error).
        """
        if not self._model:
            return ToolAgentPhaseOutput(summary="Documentation update skipped (no LLM).")

        code_text = "\n\n".join(f"--- {p} ---\n{c}" for p, c in list(files.items())[:15])[
            : self.max_doc_code_chars
        ]

        if not code_text.strip():
            return ToolAgentPhaseOutput(summary="Documentation update skipped (no code).")

        prompt = self.microtask_prompt.format(
            microtask_title=microtask.title or microtask.id,
            microtask_description=microtask.description or "N/A",
            task_description=task_description or "N/A",
            code=code_text,
        )

        try:
            raw = self._run_agent(self._model, prompt)
        except Exception as e:
            self._logger.warning("Documentation microtask LLM call failed: %s", e)
            return ToolAgentPhaseOutput(summary="Documentation update failed (LLM error).")

        parsed = self._parse_single_issue(raw)
        updated_files = parsed.get("files") or {}

        return ToolAgentPhaseOutput(
            files=updated_files,
            summary=f"Documentation: updated {len(updated_files)} file(s) for microtask {microtask.id}.",
        )

    def review(self, inp) -> ToolAgentPhaseOutput:
        """Review all documentation for completeness and consistency.

        Preconditions: ``inp`` exposes ``current_files``/``task_title``/
        ``task_description``; the profile prompt/parser hooks are set.
        Postconditions: returns a :class:`ToolAgentPhaseOutput` whose ``issues``
        each carry ``source == "documentation"`` (empty when no LLM / no code).
        """
        if not self._model:
            return ToolAgentPhaseOutput(summary="Documentation review skipped (no LLM).")

        doc_files = self._extract_doc_files(inp.current_files)
        code_text = "\n\n".join(
            f"--- {p} ---\n{c}" for p, c in list(inp.current_files.items())[:20]
        )[: self.max_doc_code_chars]

        doc_text = (
            "\n\n".join(f"--- {p} ---\n{c}" for p, c in doc_files.items())[
                : self.max_doc_code_chars // 2
            ]
            if doc_files
            else "(no documentation files found)"
        )

        if not code_text.strip():
            return ToolAgentPhaseOutput(summary="Documentation review skipped (no code).")

        prompt = self.doc_review_prompt.format(
            task_title=inp.task_title or "N/A",
            task_description=inp.task_description or "N/A",
            documentation=doc_text,
            code=code_text,
        )

        try:
            raw = self._run_agent(self._model, prompt)
        except Exception as e:
            self._logger.warning("Documentation review LLM call failed: %s", e)
            return ToolAgentPhaseOutput(summary="Documentation review failed (LLM error).")

        data = self._parse_review(raw)
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

    def problem_solve(self, inp) -> ToolAgentPhaseOutput:
        """Fix documentation issues one at a time.

        Only fixes issues whose source is in :attr:`problem_solve_sources`.

        Preconditions: ``inp`` exposes ``review_issues``/``current_files``/
        ``language``; the profile prompt/parser hooks are set.
        Postconditions: returns a :class:`ToolAgentPhaseOutput`. When there is
        no LLM or no matching documentation issues, ``files`` is empty (the
        default); otherwise ``files`` carries the merged file set after
        applying each successful single-issue fix.
        """
        if not self._model:
            return ToolAgentPhaseOutput(summary="Documentation problem_solve skipped (no LLM).")

        doc_issues = [
            i for i in inp.review_issues if (i.source or "").strip() in self.problem_solve_sources
        ]

        if not doc_issues:
            return ToolAgentPhaseOutput(summary="No documentation issues to fix.")

        extra = self._problem_solving_kwargs(inp)
        merged = dict(inp.current_files)
        fixed_count = 0

        for issue in doc_issues:
            relevant_code = relevant_code_for_issue(issue, merged, self.max_relevant_code_chars)

            prompt = self.doc_problem_solve_prompt.format(
                source=issue.source or "documentation",
                severity=issue.severity or "medium",
                description=issue.description or "",
                file_path=issue.file_path or "N/A",
                recommendation=issue.recommendation or self.default_recommendation,
                current_code=relevant_code,
                **extra,
            )

            try:
                raw = self._run_agent(self._model, prompt)
            except Exception as e:
                self._logger.warning(
                    "Documentation fix for issue %s failed: %s", (issue.description or "")[:50], e
                )
                continue

            parsed = self._parse_single_issue(raw)
            fixed_files = parsed.get("files") or {}
            if fixed_files:
                merged.update(fixed_files)
                fixed_count += 1

        return ToolAgentPhaseOutput(
            files=merged,
            summary=f"Documentation: fixed {fixed_count} of {len(doc_issues)} issue(s).",
        )

    def deliver(self, inp) -> ToolAgentPhaseOutput:
        """Documentation has no merge step.

        Postconditions: returns a static :class:`ToolAgentPhaseOutput` summary.
        """
        return ToolAgentPhaseOutput(summary="Documentation deliver.")
