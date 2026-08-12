"""Unit tests for ``ProcessDesignerAgent`` and ``_dict_to_process``.

Fenced-block extraction/stripping is delegated to
``agent_team_studio.assistant_kernel.fenced_json`` and covered there directly
(see ``assistant_kernel/tests/test_fenced_json.py``). This module covers the
pieces still local to Process Designer: ``_dict_to_process`` and the
``ProcessDesignerAgent.respond`` wiring around the kernel calls. ``strands.Agent``
is a real third-party dependency invoked via a lazy ``from strands import Agent``
inside ``respond``, so it is stubbed directly (the same seam used elsewhere in
this codebase, e.g. ``software_engineering_team/tests/test_shared_modules.py``)
rather than run against a live model.
"""

from __future__ import annotations

import pytest
import strands

from agent_team_studio.agentic_team_provisioning.assistant import agent as agent_module
from agent_team_studio.agentic_team_provisioning.assistant.agent import (
    ProcessDesignerAgent,
    _dict_to_process,
)
from agent_team_studio.agentic_team_provisioning.models import ProcessStatus, StepType, TriggerType

_FULL_REPLY = """\
Here's a draft process for you.

```agents
[{"agent_name": "Triage Agent", "role": "Classifies tickets"}]
```

```process
{
  "name": "Support Triage",
  "description": "Classify and route tickets",
  "trigger": {"trigger_type": "message", "description": "A ticket arrives"},
  "steps": [
    {"step_id": "step_1", "name": "Triage", "description": "Classify", "step_type": "action",
     "agents": [{"agent_name": "Triage Agent", "role": "Classifies tickets"}], "next_steps": []}
  ],
  "output": {"description": "Routed ticket", "destination": "Queue"}
}
```

```suggestions
["What SLA applies?", "Should escalations page on-call?"]
```
"""


@pytest.fixture(autouse=True)
def _stub_llm_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # respond() calls get_strands_model(...) before constructing strands.Agent;
    # stub it so tests never need real LLM provider config.
    monkeypatch.setattr(agent_module, "get_strands_model", lambda *a, **k: object())


def _stub_agent_reply(monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    class _FixedReplyAgent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            return reply

    monkeypatch.setattr(strands, "Agent", _FixedReplyAgent)


# ---------------------------------------------------------------------------
# ProcessDesignerAgent.respond
# ---------------------------------------------------------------------------


def test_respond_parses_all_three_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_agent_reply(monkeypatch, _FULL_REPLY)

    reply, process, suggestions, agents_data = ProcessDesignerAgent().respond(
        conversation_history=[], current_process=None, user_message="hi"
    )

    assert process is not None
    assert process.name == "Support Triage"
    assert process.trigger.trigger_type == TriggerType.MESSAGE
    assert len(process.steps) == 1
    assert process.steps[0].step_type == StepType.ACTION
    assert process.status == ProcessStatus.DRAFT

    assert agents_data == [{"agent_name": "Triage Agent", "role": "Classifies tickets"}]
    assert suggestions == ["What SLA applies?", "Should escalations page on-call?"]

    assert "```" not in reply
    assert reply.startswith("Here's a draft process for you.")


def test_respond_reuses_existing_process_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_agent_reply(monkeypatch, _FULL_REPLY)
    current = _dict_to_process({"name": "old"}, existing_id="existing-id")

    _, process, _, _ = ProcessDesignerAgent().respond(
        conversation_history=[], current_process=current, user_message="continue"
    )

    assert process is not None
    assert process.process_id == "existing-id"


def test_respond_no_blocks_returns_no_update_and_default_suggestions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_agent_reply(monkeypatch, "Just a plain conversational reply, no blocks.")

    reply, process, suggestions, agents_data = ProcessDesignerAgent().respond(
        conversation_history=[], current_process=None, user_message="hi"
    )

    assert process is None
    assert agents_data is None
    assert suggestions == [
        "What is the team's purpose?",
        "What agents should be on this team?",
        "What processes will they run?",
    ]
    assert reply == "Just a plain conversational reply, no blocks."


def test_respond_no_default_suggestions_when_process_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_agent_reply(monkeypatch, "Just prose, no blocks.")
    current = _dict_to_process({"name": "existing"}, existing_id="p1")

    _, process, suggestions, _ = ProcessDesignerAgent().respond(
        conversation_history=[], current_process=current, user_message="hi"
    )

    assert process is None
    assert suggestions == []


def test_respond_malformed_process_block_degrades_to_no_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_agent_reply(monkeypatch, "Oops.\n```process\nnot valid json\n```\n")

    reply, process, _, _ = ProcessDesignerAgent().respond(
        conversation_history=[], current_process=None, user_message="hi"
    )

    assert process is None
    assert reply == "Oops."


# ---------------------------------------------------------------------------
# _dict_to_process
# ---------------------------------------------------------------------------


def test_dict_to_process_full_dict() -> None:
    data = {
        "name": "Support Triage",
        "description": "Classify and route",
        "trigger": {"trigger_type": "event", "description": "Ticket created"},
        "steps": [
            {
                "step_id": "s1",
                "name": "Triage",
                "description": "Classify",
                "step_type": "decision",
                "agents": [{"agent_name": "A1", "role": "Classifies"}],
                "next_steps": ["s2"],
                "condition": "priority",
            }
        ],
        "output": {"description": "Resolved", "destination": "Archive"},
    }
    process = _dict_to_process(data)

    assert process.name == "Support Triage"
    assert process.trigger.trigger_type == TriggerType.EVENT
    assert process.steps[0].step_type == StepType.DECISION
    assert process.steps[0].agents[0].agent_name == "A1"
    assert process.steps[0].condition == "priority"
    assert process.output.destination == "Archive"
    assert process.status == ProcessStatus.DRAFT


def test_dict_to_process_defaults_on_missing_fields() -> None:
    process = _dict_to_process({})

    assert process.name == ""
    assert process.trigger.trigger_type == TriggerType.MESSAGE
    assert process.steps == []
    assert process.output.description == ""


def test_dict_to_process_reuses_existing_id() -> None:
    process = _dict_to_process({"name": "x"}, existing_id="fixed-id")
    assert process.process_id == "fixed-id"


def test_dict_to_process_generates_id_when_absent() -> None:
    a = _dict_to_process({"name": "x"})
    b = _dict_to_process({"name": "x"})
    assert a.process_id != b.process_id
