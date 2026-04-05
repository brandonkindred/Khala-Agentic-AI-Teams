"""Git tool catalog — backed by agent_git_tools.GIT_TOOL_DEFINITIONS."""

from __future__ import annotations

from typing import Any, Dict, List

from agent_git_tools import GIT_TOOL_DEFINITIONS
from agent_llm_tools_service.models import (
    ExecutionHints,
    OperationDetail,
    ToolDetail,
    ToolDocumentation,
    ToolSummary,
)

_GIT_EXECUTION = ExecutionHints(
    kind="llm_function_call",
    package="agent_git_tools",
    handler="execute_git_tool",
    context_class="GitToolContext",
    tool_loop="llm_service.tool_loop.complete_json_with_tool_loop",
    notes=(
        "Construct GitToolContext(repo_path=..., default_base_branch=..., "
        "allow_merge_to_default_branch=...) from orchestrator state; never trust repo_path from the model. "
        "Use build_git_tool_handlers(ctx) with complete_json_with_tool_loop. "
        "Merge may be disabled via context during implement phases."
    ),
)

# Per-operation links to git-scm.com reference (stable public docs).
_GIT_OPERATION_DOC_LINKS: Dict[str, List[str]] = {
    "git_status": ["https://git-scm.com/docs/git-status"],
    "git_diff": ["https://git-scm.com/docs/git-diff"],
    "git_log": ["https://git-scm.com/docs/git-log"],
    "git_checkout_branch": ["https://git-scm.com/docs/git-checkout"],
    "git_create_feature_branch": [
        "https://git-scm.com/docs/git-branch",
        "https://git-scm.com/docs/git-checkout",
    ],
    "git_write_files_and_commit": [
        "https://git-scm.com/docs/git-add",
        "https://git-scm.com/docs/git-commit",
    ],
    "git_commit_working_tree": ["https://git-scm.com/docs/git-commit"],
    "git_merge_branch": ["https://git-scm.com/docs/git-merge"],
}


class GitToolAdapter:
    """Catalog adapter for repository Git operations exposed as LLM functions."""

    @property
    def tool_id(self) -> str:
        return "git"

    def summarize(self) -> ToolSummary:
        return ToolSummary(
            tool_id=self.tool_id,
            display_name="Git",
            summary="Local Git repository operations (status, diff, branches, commit, merge) via LLM function calls.",
            category="version_control",
        )

    def documentation(self) -> ToolDocumentation:
        return ToolDocumentation(
            primary_links=[
                "https://git-scm.com/doc",
                "https://git-scm.com/book/en/v2",
            ],
            reference_links=[
                "https://git-scm.com/docs",
            ],
            man_page_hints=[
                "man git",
                "man gittutorial",
                "man gitcli",
            ],
            inline_summary=(
                "Git is a distributed version control system. The LLM operations in this catalog map to "
                "common porcelain commands (status, diff, log, checkout, branch, add/commit, merge). "
                "Use per-operation documentation_links for exact flags and behavior; full manual pages "
                "are available locally via the man_page_hints when running in a shell."
            ),
        )

    def detail(self) -> ToolDetail:
        s = self.summarize()
        return ToolDetail(
            tool_id=s.tool_id,
            display_name=s.display_name,
            summary=s.summary,
            category=s.category,
            documentation=self.documentation(),
            openai_definitions=list(GIT_TOOL_DEFINITIONS),
        )

    def list_operations(self) -> List[OperationDetail]:
        out: List[OperationDetail] = []
        for entry in GIT_TOOL_DEFINITIONS:
            fn = entry.get("function") or {}
            name = str(fn.get("name") or "")
            desc = str(fn.get("description") or "")
            params = fn.get("parameters")
            if not isinstance(params, dict):
                params = {}
            doc_links = list(_GIT_OPERATION_DOC_LINKS.get(name, []))
            out.append(
                OperationDetail(
                    operation_id=name,
                    function_name=name,
                    description=desc,
                    parameters_schema=params,
                    execution=_GIT_EXECUTION,
                    documentation_links=doc_links,
                )
            )
        return out

    def openai_tool_definitions(self) -> List[dict[str, Any]]:
        return list(GIT_TOOL_DEFINITIONS)
