"""Tests for LlmToolsService and Git adapter."""

from __future__ import annotations

import sys
from pathlib import Path

_agents = Path(__file__).resolve().parent.parent.parent
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

import pytest  # noqa: E402

from agent_git_tools import GIT_TOOL_DEFINITIONS  # noqa: E402
from agent_llm_tools_service import LlmToolNotFoundError, LlmToolsService  # noqa: E402


def test_list_tools_includes_git() -> None:
    svc = LlmToolsService()
    tools = svc.list_tools()
    ids = [t.tool_id for t in tools]
    assert "git" in ids
    g = next(t for t in tools if t.tool_id == "git")
    assert g.display_name == "Git"
    assert g.category == "version_control"


def test_list_operations_matches_git_definitions() -> None:
    svc = LlmToolsService()
    ops = svc.list_operations("git")
    assert len(ops) == len(GIT_TOOL_DEFINITIONS)
    names = {o.function_name for o in ops}
    assert "git_status" in names
    assert "git_merge_branch" in names
    for op in ops:
        assert op.operation_id == op.function_name
        assert isinstance(op.parameters_schema, dict)
        assert op.execution.package == "agent_git_tools"
        assert op.execution.handler == "execute_git_tool"


def test_get_tool_has_openai_definitions() -> None:
    svc = LlmToolsService()
    d = svc.get_tool("git")
    assert d.tool_id == "git"
    assert len(d.openai_definitions) == len(GIT_TOOL_DEFINITIONS)
    assert d.documentation.primary_links
    assert "git-scm.com" in d.documentation.primary_links[0]


def test_get_documentation_matches_embedded() -> None:
    svc = LlmToolsService()
    doc = svc.get_documentation("git")
    detail_doc = svc.get_tool("git").documentation
    assert doc.model_dump() == detail_doc.model_dump()
    assert doc.man_page_hints


def test_git_status_operation_has_doc_links() -> None:
    svc = LlmToolsService()
    ops = svc.list_operations("git")
    st = next(o for o in ops if o.function_name == "git_status")
    assert st.documentation_links
    assert any("git-status" in u for u in st.documentation_links)


def test_unknown_tool_raises() -> None:
    svc = LlmToolsService()
    with pytest.raises(LlmToolNotFoundError):
        svc.get_tool("nonexistent")


def test_unknown_documentation_raises() -> None:
    svc = LlmToolsService()
    with pytest.raises(LlmToolNotFoundError):
        svc.get_documentation("nonexistent")
