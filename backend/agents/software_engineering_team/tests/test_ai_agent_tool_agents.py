"""Unit tests for the AI-agent-development one-shot JSON tool agents.

These lock in the shared :class:`JsonGeneratorToolAgent` behavior — in
particular that model output which is *not* a clean JSON object (fenced or
prose-wrapped) degrades to an empty result instead of raising
:class:`json.JSONDecodeError`, which the previous per-agent bare ``json.loads``
did not do. Also covers the no-model guard and call-exception fallback adopted
from :class:`~software_engineering_team.shared.llm_tool_agent_base.LlmToolAgentBase`.
"""

from __future__ import annotations

import json

import pytest

from llm_service import get_strands_model
from software_engineering_team.ai_agent_development_team.models import (
    Microtask,
    ToolAgentInput,
    ToolAgentOutput,
)
from software_engineering_team.ai_agent_development_team.tool_agents._base import (
    JsonGeneratorToolAgent,
)
from software_engineering_team.shared.llm_tool_agent_base import LlmToolAgentBase

# (module import path, class name) for every concrete one-shot tool agent.
AGENTS = [
    ("agent_runtime", "AgentRuntimeToolAgent"),
    ("safety_governance", "SafetyGovernanceToolAgent"),
    ("evaluation_harness", "EvaluationHarnessToolAgent"),
    ("memory_rag", "MemoryRagToolAgent"),
    ("prompt_engineering", "PromptEngineeringToolAgent"),
    ("mcp_server_connectivity", "MCPServerConnectivityToolAgent"),
]


def _tool_input() -> ToolAgentInput:
    return ToolAgentInput(
        microtask=Microtask(id="m1", title="Wire the runtime", description="Do the thing"),
        spec_context="spec details" * 1000,  # exercises the spec truncation
    )


def _load(module_name: str):
    import importlib

    return importlib.import_module(
        f"software_engineering_team.ai_agent_development_team.tool_agents.{module_name}.agent"
    )


def test_json_generator_uses_llm_tool_agent_base_plan_json_recipe() -> None:
    """JsonGeneratorToolAgent is a thin Plan/Json specialization of LlmToolAgentBase."""
    assert issubclass(JsonGeneratorToolAgent, LlmToolAgentBase)
    assert JsonGeneratorToolAgent.resolve_models is True
    assert JsonGeneratorToolAgent.response_format == "json"
    assert JsonGeneratorToolAgent.get_strands_model_fn is get_strands_model
    assert JsonGeneratorToolAgent.use_run_strands_agent is False
    assert JsonGeneratorToolAgent.json_parse_strategy == "extract"


@pytest.mark.parametrize("module_name,class_name", AGENTS)
def test_run_parses_valid_json(monkeypatch, module_name: str, class_name: str) -> None:
    captured = {}

    class _GoodAgent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt):
            captured["prompt"] = prompt
            return json.dumps(
                {
                    "files": {"a.py": "print('hi')"},
                    "recommendations": ["do x"],
                    "summary": "done",
                }
            )

    mod = _load(module_name)
    monkeypatch.setattr(mod, "Agent", _GoodAgent)
    inst = getattr(mod, class_name).__new__(getattr(mod, class_name))
    inst._model = object()
    out = inst.run(_tool_input())
    assert isinstance(out, ToolAgentOutput)
    assert out.files == {"a.py": "print('hi')"}
    assert out.recommendations == ["do x"]
    assert out.summary == "done"
    # Spec context was truncated to MAX_SPEC_CHARS before prompting.
    assert "spec details" in captured["prompt"]


def test_run_recovers_from_fenced_output(monkeypatch) -> None:
    """A fenced ```json block used to raise; it must now parse via salvage."""

    class _FencedAgent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt):
            return '```json\n{"files": {}, "summary": "ok"}\n```'

    mod = _load("agent_runtime")
    monkeypatch.setattr(mod, "Agent", _FencedAgent)
    inst = mod.AgentRuntimeToolAgent.__new__(mod.AgentRuntimeToolAgent)
    inst._model = object()
    out = inst.run(_tool_input())
    assert out.summary == "ok"
    assert out.files == {}


def test_run_degrades_on_non_json(monkeypatch) -> None:
    """Prose with no JSON object degrades to an empty output instead of raising."""

    class _ProseAgent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt):
            return "I could not complete this request."

    mod = _load("agent_runtime")
    monkeypatch.setattr(mod, "Agent", _ProseAgent)
    inst = mod.AgentRuntimeToolAgent.__new__(mod.AgentRuntimeToolAgent)
    inst._model = object()
    out = inst.run(_tool_input())
    assert out.files == {}
    assert out.recommendations == []
    assert out.summary == ""


@pytest.mark.parametrize("module_name,class_name", AGENTS)
def test_run_no_model_returns_empty(module_name: str, class_name: str) -> None:
    """Missing model returns empty files/recommendations/summary without calling the LLM."""
    mod = _load(module_name)
    inst = getattr(mod, class_name).__new__(getattr(mod, class_name))
    inst._model = None
    out = inst.run(_tool_input())
    assert isinstance(out, ToolAgentOutput)
    assert out.files == {}
    assert out.recommendations == []
    assert out.summary == ""


@pytest.mark.parametrize("module_name,class_name", AGENTS)
def test_run_llm_exception_returns_empty(monkeypatch, module_name: str, class_name: str) -> None:
    """An exception from the LLM call degrades to empty output instead of propagating."""

    class _BoomAgent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt):
            raise RuntimeError("upstream LLM blew up")

    mod = _load(module_name)
    monkeypatch.setattr(mod, "Agent", _BoomAgent)
    inst = getattr(mod, class_name).__new__(getattr(mod, class_name))
    inst._model = object()
    out = inst.run(_tool_input())
    assert isinstance(out, ToolAgentOutput)
    assert out.files == {}
    assert out.recommendations == []
    assert out.summary == ""
