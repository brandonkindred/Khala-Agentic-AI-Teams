"""Tests for the Frontend/Backend Code V2 tool-agent execute/plan/review/
problem_solve/deliver wrappers.

Each tool agent has a very thin shell — most methods are stubs that return
default messages, with ``plan()`` calling the LLM. We exercise both the
no-LLM path and the LLM-failure path; the LLM-success path requires a real
Strands ``Agent`` patch which we monkey-patch on a per-test basis.
"""

from __future__ import annotations

import json

from llm_service import get_strands_model
from software_engineering_team.codegen_team.tool_agents.frontend._plan_base import (
    PlanGeneratorToolAgent,
)
from software_engineering_team.shared.llm_tool_agent_base import LlmToolAgentBase


def _microtask():
    from software_engineering_team.codegen_team.models import (
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
    from software_engineering_team.codegen_team.models import (
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
    from software_engineering_team.codegen_team.models import ToolAgentInput

    return ToolAgentInput(
        microtask=_microtask(),
        task_title="t",
        task_description="d",
        spec_content="",
        repo_path="/tmp",
    )


# ---------------------------------------------------------------------------
# Plan generator base (LlmToolAgentBase recipe)
# ---------------------------------------------------------------------------


def test_plan_generator_inherits_llm_tool_agent_base() -> None:
    assert issubclass(PlanGeneratorToolAgent, LlmToolAgentBase)


def test_plan_generator_selects_plan_recipe() -> None:
    assert PlanGeneratorToolAgent.resolve_models is True
    assert PlanGeneratorToolAgent.response_format == "json"
    assert PlanGeneratorToolAgent.get_strands_model_fn is get_strands_model
    assert PlanGeneratorToolAgent.use_run_strands_agent is False
    assert PlanGeneratorToolAgent.json_parse_strategy == "extract"


# ---------------------------------------------------------------------------
# Branding / Theme tool agent
# ---------------------------------------------------------------------------


def test_branding_theme_execute_returns_stub() -> None:
    from software_engineering_team.codegen_team.tool_agents.frontend.branding_theme.agent import (
        BrandingThemeToolAgent,
    )

    agent = BrandingThemeToolAgent.__new__(BrandingThemeToolAgent)
    agent._model = None
    agent.llm = None
    result = agent.execute(_tool_input())
    assert result.summary.startswith("Branding/Theme")


def test_branding_theme_plan_no_model_returns_default() -> None:
    from software_engineering_team.codegen_team.tool_agents.frontend.branding_theme.agent import (
        BrandingThemeToolAgent,
    )

    agent = BrandingThemeToolAgent.__new__(BrandingThemeToolAgent)
    agent._model = None
    out = agent.plan(_phase_input())
    assert "stub" in out.summary.lower()
    assert out.recommendations


def test_branding_theme_plan_llm_failure(monkeypatch) -> None:
    from software_engineering_team.codegen_team.tool_agents.frontend.branding_theme import (
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
    from software_engineering_team.codegen_team.tool_agents.frontend.branding_theme import (
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
    from software_engineering_team.codegen_team.tool_agents.frontend.branding_theme import (
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
    from software_engineering_team.codegen_team.tool_agents.frontend.branding_theme.agent import (
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
    from software_engineering_team.codegen_team.tool_agents.frontend.ui_design.agent import (
        UiDesignToolAgent,
    )

    agent = UiDesignToolAgent.__new__(UiDesignToolAgent)
    agent._model = None
    agent.llm = None
    out = agent.plan(_phase_input())
    assert out.summary


def test_ui_design_execute_stub() -> None:
    from software_engineering_team.codegen_team.tool_agents.frontend.ui_design.agent import (
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
    from software_engineering_team.codegen_team.tool_agents.frontend.architecture.agent import (
        ArchitectureToolAgent,
    )

    agent = ArchitectureToolAgent.__new__(ArchitectureToolAgent)
    agent._model = None
    agent.llm = None
    out = agent.plan(_phase_input())
    assert out.summary


def test_architecture_build_plan_prompt_caps_spec_content() -> None:
    """An oversized spec is capped at MAX_SPEC_CHARS so a single-shot planning
    call (no tool fallback) can't be pushed past the model's context window."""
    from software_engineering_team.codegen_team.tool_agents.frontend.architecture.agent import (
        MAX_SPEC_CHARS,
        ArchitectureToolAgent,
    )

    agent = ArchitectureToolAgent.__new__(ArchitectureToolAgent)
    big_spec = "S" * (MAX_SPEC_CHARS + 5000)
    prompt = agent._build_plan_prompt(_phase_input(spec_context=big_spec))
    assert big_spec not in prompt
    assert "S" * MAX_SPEC_CHARS in prompt


def test_architecture_plan_null_summary_uses_empty_summary_override(monkeypatch) -> None:
    from software_engineering_team.codegen_team.tool_agents.frontend.architecture import (
        agent as mod,
    )

    class _NullSummaryAgent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt):
            return json.dumps({"summary": None})

    monkeypatch.setattr(mod, "Agent", _NullSummaryAgent)
    agent = mod.ArchitectureToolAgent.__new__(mod.ArchitectureToolAgent)
    agent._model = object()
    out = agent.plan(_phase_input())
    assert out.summary == "Architecture planning complete."


# ---------------------------------------------------------------------------
# Trivial alias tool agents (Linter, Auth, ApiOpenapi, StateManagement) all
# share a constructor + 1-2 stub methods.
# ---------------------------------------------------------------------------


def test_linter_tool_agent_constructs() -> None:
    from software_engineering_team.codegen_team.tool_agents.frontend.linter.agent import (
        LinterToolAgent,
    )

    agent = LinterToolAgent.__new__(LinterToolAgent)
    agent._model = None
    agent.llm = None
    result = agent.execute(_tool_input())
    assert result is not None


class _GoodFileGeneratorAgent:
    """Fake Strands ``Agent`` returning a parseable ``## FILE ## / ## SUMMARY ##`` template."""

    def __init__(self, **kwargs):
        pass

    def __call__(self, prompt):
        return (
            "## FILE generated.ts ##\nexport const x = 1;\n"
            "## SUMMARY ##\ndone\n## END SUMMARY ##"
        )


def test_auth_tool_agent_constructs(monkeypatch) -> None:
    """Real (non-stub) AuthToolAgent: execute() drives an LLM call and
    parses its template output, so it needs a stubbed Agent, not a bare
    ``_model = None`` (that path is only for the static-lifecycle stubs)."""
    from software_engineering_team.codegen_team.tool_agents.frontend.auth import (
        agent as mod,
    )

    monkeypatch.setattr(mod, "Agent", _GoodFileGeneratorAgent)
    agent = mod.AuthToolAgent.__new__(mod.AuthToolAgent)
    agent._model = object()
    result = agent.execute(_tool_input())
    assert result is not None
    assert "generated.ts" in result.files


def test_api_openapi_tool_agent_constructs(monkeypatch) -> None:
    from software_engineering_team.codegen_team.tool_agents.frontend.api_openapi import (
        agent as mod,
    )

    monkeypatch.setattr(mod, "Agent", _GoodFileGeneratorAgent)
    agent = mod.ApiOpenApiToolAgent.__new__(mod.ApiOpenApiToolAgent)
    agent._model = object()
    result = agent.execute(_tool_input())
    assert result is not None
    assert "generated.ts" in result.files


def test_state_management_tool_agent_constructs(monkeypatch) -> None:
    from software_engineering_team.codegen_team.tool_agents.frontend.state_management import (
        agent as mod,
    )

    monkeypatch.setattr(mod, "Agent", _GoodFileGeneratorAgent)
    agent = mod.StateManagementToolAgent.__new__(mod.StateManagementToolAgent)
    agent._model = object()
    result = agent.execute(_tool_input())
    assert result is not None
    assert "generated.ts" in result.files
