"""Tests that ``run_implement`` wires the repo-inspection tools into the Senior SWE agent.

The Strands-registration / schema contract for the inspection definitions lives in
``agent_repo_tools/tests/test_strands_registration.py`` (so it runs in that module's CI
job). Here we pin the coding-team-side integration: the tools reach the model's toolset and
the system prompt tells the agent to use them.
"""

from __future__ import annotations

from typing import Any, Dict

from agent_repo_tools import REPO_INSPECT_TOOL_DEFINITIONS
from coding_team.models import StackSpec, Task
from coding_team.senior_software_engineer_agent import agent as swe_mod
from coding_team.senior_software_engineer_agent.agent import SeniorSWEAgent

_INSPECT_TOOL_NAMES = {d["function"]["name"] for d in REPO_INSPECT_TOOL_DEFINITIONS}


def test_run_implement_wires_inspection_tools_into_agent(tmp_path, monkeypatch) -> None:
    captured: Dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured["tools"] = kw.get("tools", [])

        def __call__(self, prompt):
            return '{"summary":"ok","files_to_create_or_edit":[],"commands_run":[],"ready_for_review":true}'

    # The git tools need a real .git dir to build their context cleanly; the inspection
    # tools only need the workspace path.
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(swe_mod, "Agent", FakeAgent)

    swe = SeniorSWEAgent(agent_id="a1", stack_spec=StackSpec(name="backend"), llm=object())
    swe.run_implement(Task(id="t1", title="T1", description="do it"), tmp_path, use_git_tools=True)

    tool_names = {t.tool_name for t in captured["tools"]}
    assert _INSPECT_TOOL_NAMES.issubset(tool_names)


def test_run_implement_prompt_mentions_inspection_tools(tmp_path, monkeypatch) -> None:
    captured: Dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured["system"] = kw.get("system_prompt", "")

        def __call__(self, prompt):
            return '{"summary":"ok","files_to_create_or_edit":[],"commands_run":[],"ready_for_review":true}'

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(swe_mod, "Agent", FakeAgent)

    swe = SeniorSWEAgent(agent_id="a1", stack_spec=StackSpec(name="backend"), llm=object())
    swe.run_implement(Task(id="t1", title="T1", description="do it"), tmp_path, use_git_tools=True)

    assert "list_files" in captured["system"]
    assert "read_file" in captured["system"]
