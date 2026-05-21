"""Tests for backend_code_v2_team and frontend_code_v2_team orchestrator helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


def test_be_build_tool_agents():
    from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.backend_code_v2_team.orchestrator import _build_tool_agents

    mock_llm = MagicMock()
    agents = _build_tool_agents(mock_llm)
    assert ToolAgentKind.DOCUMENTATION in agents
    assert ToolAgentKind.SECURITY in agents
    assert ToolAgentKind.TESTING_QA in agents
    assert ToolAgentKind.BUILD_SPECIALIST in agents


def test_be_development_agent_init():
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    a = BackendDevelopmentAgent(llm_client=MagicMock())
    assert a.llm is not None


def test_be_development_agent_build_tool_runners():
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    a = BackendDevelopmentAgent(llm_client=MagicMock())

    class _WithRun:
        def run(self, inp):
            return "ran"

    class _WithExecute:
        def execute(self, inp):
            return "executed"

    class _Bare:
        pass

    runners = a._build_tool_runners(
        {
            "k1": _WithRun(),
            "k2": _WithExecute(),
            "k3": _Bare(),  # filtered out
        }
    )
    assert "k1" in runners
    assert "k2" in runners
    assert "k3" not in runners


def test_be_development_agent_read_repo_code(tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    (tmp_path / "x.py").write_text("print('x')")
    (tmp_path / "y.txt").write_text("plain")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "skip.py").write_text("# do not include")
    out = BackendDevelopmentAgent._read_repo_code(tmp_path)
    assert "x.py" in out
    assert "print('x')" in out
    assert "skip.py" not in out


def test_be_development_agent_read_repo_code_empty(tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    out = BackendDevelopmentAgent._read_repo_code(tmp_path)
    assert "No code files" in out


def test_be_development_agent_read_repo_code_max_chars(tmp_path: Path):
    """When total exceeds max_chars, we stop reading."""
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    for i in range(20):
        (tmp_path / f"f{i}.py").write_text("x" * 2000)
    out = BackendDevelopmentAgent._read_repo_code(tmp_path, max_chars=1000)
    # Should be truncated below 20*2000
    assert len(out) < 40000


def test_fe_build_tool_agents():
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.frontend_code_v2_team.orchestrator import _build_tool_agents

    mock_llm = MagicMock()
    agents = _build_tool_agents(mock_llm)
    assert ToolAgentKind.DOCUMENTATION in agents
    assert ToolAgentKind.SECURITY in agents
    assert ToolAgentKind.TESTING_QA in agents
    assert ToolAgentKind.ACCESSIBILITY in agents
    assert ToolAgentKind.PERFORMANCE in agents
    assert ToolAgentKind.UX_USABILITY in agents


def test_fe_development_agent_init():
    from software_engineering_team.frontend_code_v2_team.orchestrator import (
        FrontendDevelopmentAgent,
    )

    a = FrontendDevelopmentAgent(llm_client=MagicMock())
    assert a.llm is not None
