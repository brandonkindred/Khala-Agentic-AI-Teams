"""Unit tests for the AI-agent-development one-shot JSON tool agents.

These lock in the shared :class:`JsonGeneratorToolAgent` behavior — in
particular that model output which is *not* a clean JSON object (fenced or
prose-wrapped) degrades to an empty result instead of raising
:class:`json.JSONDecodeError`, which the previous per-agent bare ``json.loads``
did not do.
"""

from __future__ import annotations

import json

import pytest

from software_engineering_team.ai_agent_development_team.models import (
    Microtask,
    ToolAgentInput,
    ToolAgentOutput,
)

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
