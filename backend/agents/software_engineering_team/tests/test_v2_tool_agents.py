"""Tests for the Frontend/Backend Code V2 tool-agent execute/plan/review/
problem_solve/deliver wrappers.

Each tool agent has a very thin shell — most methods are stubs that return
default messages, with ``plan()`` calling the LLM. We exercise both the
no-LLM path and the LLM-failure path; the LLM-success path requires a real
Strands ``Agent`` patch which we monkey-patch on a per-test basis.
"""

from __future__ import annotations

import json


def _microtask():
    from software_engineering_team.frontend_code_v2_team.models import (
        Microtask,
        ToolAgentKind,
    )

    return Microtask(
        id="mt-1",
        title="Task",
        description="Do thing",
        tool_agent=ToolAgentKind.GENERAL,
    )


def _phase_input(**kwargs):
    from software_engineering_team.frontend_code_v2_team.models import (
        Phase,
        ToolAgentPhaseInput,
    )

    base = dict(
        phase=Phase.PLANNING,
        repo_path="/tmp",
        current_files={},
        task_title="t",
        task_description="d",
        task_id="t1",
        language="typescript",
    )
    base.update(kwargs)
    return ToolAgentPhaseInput(**base)


def _tool_input():
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentInput

    return ToolAgentInput(
        microtask=_microtask(),
        task_title="t",
        task_description="d",
        spec_content="",
        repo_path="/tmp",
    )


# ---------------------------------------------------------------------------
# Branding / Theme tool agent
# ---------------------------------------------------------------------------


def test_branding_theme_execute_returns_stub() -> None:
    from software_engineering_team.frontend_code_v2_team.tool_agents.branding_theme.agent import (
        BrandingThemeToolAgent,
    )

    agent = BrandingThemeToolAgent.__new__(BrandingThemeToolAgent)
    agent._model = None
    agent.llm = None
    result = agent.execute(_tool_input())
    assert result.summary.startswith("Branding/Theme")


def test_branding_theme_plan_no_model_returns_default() -> None:
    from software_engineering_team.frontend_code_v2_team.tool_agents.branding_theme.agent import (
        BrandingThemeToolAgent,
    )

    agent = BrandingThemeToolAgent.__new__(BrandingThemeToolAgent)
    agent._model = None
    out = agent.plan(_phase_input())
    assert "stub" in out.summary.lower()
    assert out.recommendations


def test_branding_theme_plan_llm_failure(monkeypatch) -> None:
    from software_engineering_team.frontend_code_v2_team.tool_agents.branding_theme import (
        agent as mod,
    )

    class _BadAgent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt):
            raise RuntimeError("llm err")

    monkeypatch.setattr(mod, "Agent", _BadAgent)
    agent = mod.BrandingThemeToolAgent.__new__(mod.BrandingThemeToolAgent)
    agent._model = object()  # truthy so the LLM branch executes
    out = agent.plan(_phase_input())
    assert "failed" in out.summary.lower()


def test_branding_theme_plan_llm_success(monkeypatch) -> None:
    from software_engineering_team.frontend_code_v2_team.tool_agents.branding_theme import (
        agent as mod,
    )

    class _GoodAgent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt):
            return json.dumps(
                {
                    "component_library_plan": "lib",
                    "token_implementation_plan": "tokens",
                    "a11y_in_components": "aria",
                    "documentation_plan": "storybook",
                    "summary": "done",
                }
            )

    monkeypatch.setattr(mod, "Agent", _GoodAgent)
    agent = mod.BrandingThemeToolAgent.__new__(mod.BrandingThemeToolAgent)
    agent._model = object()
    out = agent.plan(_phase_input())
    assert "done" in out.summary
    assert any("Component Library" in r for r in out.recommendations)


def test_branding_theme_plan_llm_bad_json_recovers(monkeypatch) -> None:
    from software_engineering_team.frontend_code_v2_team.tool_agents.branding_theme import (
        agent as mod,
    )

    class _BadJsonAgent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt):
            return 'junk junk {"summary":"ok"} junk'

    monkeypatch.setattr(mod, "Agent", _BadJsonAgent)
    agent = mod.BrandingThemeToolAgent.__new__(mod.BrandingThemeToolAgent)
    agent._model = object()
    out = agent.plan(_phase_input())
    assert "ok" in out.summary


def test_branding_theme_remaining_methods_stub_summaries() -> None:
    from software_engineering_team.frontend_code_v2_team.tool_agents.branding_theme.agent import (
        BrandingThemeToolAgent,
    )

    agent = BrandingThemeToolAgent.__new__(BrandingThemeToolAgent)
    agent._model = None
    agent.llm = None
    inp = _phase_input()
    assert "review" in agent.review(inp).summary.lower()
    assert "problem" in agent.problem_solve(inp).summary.lower()
    assert "deliver" in agent.deliver(inp).summary.lower()


# ---------------------------------------------------------------------------
# UI Design tool agent
# ---------------------------------------------------------------------------


def test_ui_design_plan_no_model() -> None:
    from software_engineering_team.frontend_code_v2_team.tool_agents.ui_design.agent import (
        UiDesignToolAgent,
    )

    agent = UiDesignToolAgent.__new__(UiDesignToolAgent)
    agent._model = None
    agent.llm = None
    out = agent.plan(_phase_input())
    assert out.summary


def test_ui_design_execute_stub() -> None:
    from software_engineering_team.frontend_code_v2_team.tool_agents.ui_design.agent import (
        UiDesignToolAgent,
    )

    agent = UiDesignToolAgent.__new__(UiDesignToolAgent)
    agent._model = None
    agent.llm = None
    result = agent.execute(_tool_input())
    assert result.summary


# ---------------------------------------------------------------------------
# Architecture tool agent
# ---------------------------------------------------------------------------


def test_architecture_plan_no_model() -> None:
    from software_engineering_team.frontend_code_v2_team.tool_agents.architecture.agent import (
        ArchitectureToolAgent,
    )

    agent = ArchitectureToolAgent.__new__(ArchitectureToolAgent)
    agent._model = None
    agent.llm = None
    out = agent.plan(_phase_input())
    assert out.summary


# ---------------------------------------------------------------------------
# Trivial alias tool agents (Linter, Auth, ApiOpenapi, StateManagement) all
# share a constructor + 1-2 stub methods.
# ---------------------------------------------------------------------------


def test_linter_tool_agent_constructs() -> None:
    from software_engineering_team.frontend_code_v2_team.tool_agents.linter.agent import (
        LinterToolAgent,
    )

    agent = LinterToolAgent.__new__(LinterToolAgent)
    agent._model = None
    agent.llm = None
    result = agent.execute(_tool_input())
    assert result is not None


def test_auth_tool_agent_constructs() -> None:
    from software_engineering_team.frontend_code_v2_team.tool_agents.auth.agent import (
        AuthToolAgent,
    )

    agent = AuthToolAgent.__new__(AuthToolAgent)
    agent._model = None
    agent.llm = None
    result = agent.execute(_tool_input())
    assert result is not None


def test_api_openapi_tool_agent_constructs() -> None:
    from software_engineering_team.frontend_code_v2_team.tool_agents.api_openapi.agent import (
        ApiOpenApiToolAgent,
    )

    agent = ApiOpenApiToolAgent.__new__(ApiOpenApiToolAgent)
    agent._model = None
    agent.llm = None
    result = agent.execute(_tool_input())
    assert result is not None


def test_state_management_tool_agent_constructs() -> None:
    from software_engineering_team.frontend_code_v2_team.tool_agents.state_management.agent import (
        StateManagementToolAgent,
    )

    agent = StateManagementToolAgent.__new__(StateManagementToolAgent)
    agent._model = None
    agent.llm = None
    result = agent.execute(_tool_input())
    assert result is not None
