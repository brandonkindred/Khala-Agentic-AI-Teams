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


# ── Incremental repo-context cache threading (#3) ──────────────────────────
# The team lead threads one RepoContextCache per repo into the development
# agent's run_workflow so the N tasks of a job re-read only the files each
# merge touched. These pin the two seams: the dev agent consults the cache when
# one is present, and the team lead builds/reuses the cache per resolved repo.


def _be_dev_agent():
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    return BackendDevelopmentAgent(llm_client=MagicMock())


def _fe_dev_agent():
    from software_engineering_team.frontend_code_v2_team.orchestrator import (
        FrontendDevelopmentAgent,
    )

    return FrontendDevelopmentAgent(llm_client=MagicMock())


def test_be_read_existing_code_uses_threaded_cache(tmp_path: Path):
    """With a cache threaded in, _read_existing_code delegates to it (no fresh walk)."""
    (tmp_path / "x.py").write_text("X = 1")
    a = _be_dev_agent()
    cache = MagicMock()
    a._repo_context_cache = cache

    out = a._read_existing_code(tmp_path)

    cache.read.assert_called_once_with(tmp_path)
    assert out is cache.read.return_value


def test_be_read_existing_code_fresh_walk_without_cache(tmp_path: Path):
    """With no cache, _read_existing_code falls back to the fresh budgeted walk."""
    (tmp_path / "x.py").write_text("X = 1")
    a = _be_dev_agent()
    assert a._repo_context_cache is None
    out = a._read_existing_code(tmp_path)
    assert "x.py" in out


def test_fe_read_existing_code_uses_threaded_cache(tmp_path: Path):
    (tmp_path / "x.ts").write_text("export const x = 1;")
    a = _fe_dev_agent()
    cache = MagicMock()
    a._repo_context_cache = cache

    out = a._read_existing_code(tmp_path)

    cache.read.assert_called_once_with(tmp_path)
    assert out is cache.read.return_value


def test_fe_read_existing_code_fresh_walk_without_cache(tmp_path: Path):
    (tmp_path / "x.ts").write_text("export const x = 1;")
    a = _fe_dev_agent()
    out = a._read_existing_code(tmp_path)
    assert "x.ts" in out


def test_be_team_lead_repo_context_cache_is_lazy_and_reused(tmp_path: Path):
    """The team lead builds one cache per resolved repo and reuses it across calls."""
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendCodeV2TeamLead,
    )

    lead = BackendCodeV2TeamLead(llm_client=MagicMock())
    first = lead._repo_context_cache_for(tmp_path)
    second = lead._repo_context_cache_for(tmp_path)
    assert first is second  # same resolved repo → reused, not rebuilt

    other = tmp_path / "other"
    other.mkdir()
    third = lead._repo_context_cache_for(other)
    assert third is not first  # distinct repo → distinct cache


def test_fe_team_lead_repo_context_cache_is_lazy_and_reused(tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.orchestrator import (
        FrontendCodeV2TeamLead,
    )

    lead = FrontendCodeV2TeamLead(llm_client=MagicMock())
    first = lead._repo_context_cache_for(tmp_path)
    second = lead._repo_context_cache_for(tmp_path)
    assert first is second

    other = tmp_path / "other"
    other.mkdir()
    third = lead._repo_context_cache_for(other)
    assert third is not first


def test_fe_detect_tooling_nothing_configured(tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.orchestrator import (
        FrontendDevelopmentAgent,
    )

    assert FrontendDevelopmentAgent._detect_tooling(tmp_path) == (False, False)


def test_fe_detect_tooling_angular_and_vitest(tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.orchestrator import (
        FrontendDevelopmentAgent,
    )

    (tmp_path / "angular.json").write_text("{}")
    (tmp_path / "vitest.config.js").write_text("")
    has_lint, has_test = FrontendDevelopmentAgent._detect_tooling(tmp_path)
    assert has_lint and has_test


def test_fe_detect_tooling_real_npm_test_script(tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.orchestrator import (
        FrontendDevelopmentAgent,
    )

    (tmp_path / "eslint.config.js").write_text("")
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    has_lint, has_test = FrontendDevelopmentAgent._detect_tooling(tmp_path)
    assert has_lint and has_test


def test_fe_detect_tooling_rejects_placeholder_and_unparseable(tmp_path: Path):
    """A 'no test'/placeholder script and an unparseable package.json both yield no test."""
    from software_engineering_team.frontend_code_v2_team.orchestrator import (
        FrontendDevelopmentAgent,
    )

    (tmp_path / "eslint.config.js").write_text("")
    (tmp_path / "package.json").write_text("not json {")
    has_lint, has_test = FrontendDevelopmentAgent._detect_tooling(tmp_path)
    assert has_lint and not has_test  # unparseable package.json → no test detected
