"""Cross-assistant parity/regression tests pinning both design-assistant call
sites to the shared ``assistant_kernel`` contract.

Both ``AgentDesignerAgent`` (Agent Studio) and ``ProcessDesignerAgent`` (the
agentic Process Designer) call into this package's fenced-JSON helpers, and
Agent Studio additionally composes with the turn-lock helpers via
``agent_studio.store.AgentStudioConversationStore``. The kernel's own test
suite (``test_fenced_json.py``, ``test_envelope.py``, ``test_turn_lock.py``)
already exhaustively covers those primitives in isolation, and each
assistant's own test module (``agent_studio/tests/test_assistant.py``,
``agentic_team_provisioning/tests/test_assistant_agent.py``) already covers
its own behavior end-to-end. What neither covers: whether the *same*
kernel-relevant edge case degrades the *same* way at both call sites, and
whether the two assistants' deliberate divergences (keyed-overlay merge +
turn-lock for Agent Studio; wholesale-rebuild merge and no turn-lock for
Process Designer) are pinned as intentional facts rather than implicit ones.
This module is that suite.
"""

from __future__ import annotations

import pytest
import strands

from agent_team_studio.agent_studio.assistant import AgentDesignerAgent
from agent_team_studio.agent_studio.models import AgentDefinition
from agent_team_studio.agent_studio.store import AgentStudioConversationStore
from agent_team_studio.agentic_team_provisioning.assistant import agent as agent_module
from agent_team_studio.agentic_team_provisioning.assistant.agent import (
    ProcessDesignerAgent,
    _dict_to_process,
)


def _completion(text: str):
    """A ``CompleteFn`` that always returns ``text`` (for ``AgentDesignerAgent``)."""

    def complete(system_prompt: str, prompt: str) -> str:
        return text

    return complete


def _stub_agent_reply(monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    """Stub ``ProcessDesignerAgent``'s ``strands.Agent`` seam to return ``reply``.

    Mirrors ``agentic_team_provisioning/tests/test_assistant_agent.py``'s helper
    of the same name — the standard seam for this assistant in this codebase.
    """
    monkeypatch.setattr(agent_module, "get_strands_model", lambda *a, **k: object())

    class _FixedReplyAgent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            return reply

    monkeypatch.setattr(strands, "Agent", _FixedReplyAgent)


def _fenced(tag: str, body: str) -> str:
    return f"Here's what I've got.\n\n```{tag}\n{body}\n```\n"


# ---------------------------------------------------------------------------
# Fenced-JSON parity: both assistants must degrade the same way on the same
# shape of malformed/absent/ambiguous kernel input.
# ---------------------------------------------------------------------------

_MALFORMED_BODIES = [
    ("malformed_json", "not valid json"),
    ("wrong_top_level_type", '["a", "b"]'),
]


@pytest.mark.parametrize("case_id, body", _MALFORMED_BODIES)
def test_both_assistants_return_no_update_on_a_bad_primary_block(
    monkeypatch: pytest.MonkeyPatch, case_id: str, body: str
) -> None:
    agent_reply = _fenced("agent", body)
    _, updated_definition, _ = AgentDesignerAgent(complete=_completion(agent_reply)).respond(
        [], AgentDefinition(), "go"
    )
    assert updated_definition is None

    _stub_agent_reply(monkeypatch, _fenced("process", body))
    _, updated_process, _, _ = ProcessDesignerAgent().respond(
        conversation_history=[], current_process=None, user_message="go"
    )
    assert updated_process is None


def test_both_assistants_return_no_update_and_original_prose_when_no_block_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prose = "Just a clarifying question, no draft yet."

    reply, updated_definition, _ = AgentDesignerAgent(complete=_completion(prose)).respond(
        [], AgentDefinition(), "go"
    )
    assert updated_definition is None
    assert reply == prose

    _stub_agent_reply(monkeypatch, prose)
    reply, updated_process, _, agents_data = ProcessDesignerAgent().respond(
        conversation_history=[], current_process=None, user_message="go"
    )
    assert updated_process is None
    assert agents_data is None
    assert reply == prose


@pytest.mark.parametrize(
    "extend",
    [
        pytest.param(lambda tag: f"{tag}s", id="word_extended_tag"),
        pytest.param(lambda tag: f"{tag}-v2", id="punctuation_extended_tag"),
    ],
)
def test_both_assistants_ignore_a_block_whose_tag_only_extends_their_own(
    monkeypatch: pytest.MonkeyPatch, extend
) -> None:
    # A block tagged e.g. "agents"/"agent-v2" must never be mistaken for a real
    # "agent" block — the kernel's tag-boundary matching, exercised through each
    # assistant's own tag ("agent" / "process") rather than in the abstract.
    agent_reply = _fenced(extend("agent"), '{"name": "should not parse"}')
    _, updated_definition, _ = AgentDesignerAgent(complete=_completion(agent_reply)).respond(
        [], AgentDefinition(), "go"
    )
    assert updated_definition is None

    _stub_agent_reply(monkeypatch, _fenced(extend("process"), '{"name": "should not parse"}'))
    _, updated_process, _, _ = ProcessDesignerAgent().respond(
        conversation_history=[], current_process=None, user_message="go"
    )
    assert updated_process is None


def test_both_assistants_tolerate_crlf_line_endings_around_the_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_reply = 'Draft below.\r\n\r\n```agent\r\n{"name": "crlf-agent", "role": "r"}\r\n```\r\n'
    reply, updated_definition, _ = AgentDesignerAgent(complete=_completion(agent_reply)).respond(
        [], AgentDefinition(), "go"
    )
    assert updated_definition is not None
    assert updated_definition.name == "crlf-agent"
    assert "```" not in reply

    process_reply = 'Draft below.\r\n\r\n```process\r\n{"name": "crlf-process"}\r\n```\r\n'
    _stub_agent_reply(monkeypatch, process_reply)
    reply, updated_process, _, _ = ProcessDesignerAgent().respond(
        conversation_history=[], current_process=None, user_message="go"
    )
    assert updated_process is not None
    assert updated_process.name == "crlf-process"
    assert "```" not in reply


# ---------------------------------------------------------------------------
# Turn-lock parity: Agent Studio composes AgentDesignerAgent with the kernel's
# turn-lock via its store; Process Designer has no turn-lock at all today
# (assistant_kernel/turn_lock.py's module docstring: wiring one up is a
# follow-up, not part of the kernel migration this issue verifies).
# ---------------------------------------------------------------------------

_FULL_AGENT_REPLY = """\
Here's a draft for you.

```agent
{"name": "blogging.planner", "role": "Plans outlines"}
```
"""


def test_agent_studio_turn_composes_respond_with_kernel_serialization() -> None:
    store = AgentStudioConversationStore()
    cid = store.create("new", None, AgentDefinition())
    designer = AgentDesignerAgent(complete=_completion(_FULL_AGENT_REPLY))

    with store.turn(cid) as turn:
        reply, updated, _ = designer.respond(list(turn.history), turn.draft, "Build a planner")
        turn.append_message("user", "Build a planner")
        turn.append_message("assistant", reply)
        if updated is not None:
            turn.set_draft(updated)

    record = store.get(cid)
    assert record is not None
    assert record.definition.name == "blogging.planner"
    assert [m.content for m in record.messages] == ["Build a planner", reply]


def test_agent_studio_turn_rolls_back_when_respond_raises_mid_turn() -> None:
    store = AgentStudioConversationStore()
    cid = store.create("new", None, AgentDefinition(name="before"))
    designer = AgentDesignerAgent(complete=_completion("noop"))

    with pytest.raises(ValueError):
        with store.turn(cid) as turn:
            turn.append_message("user", "")
            designer.respond([], turn.draft, "")  # raises: empty user_message

    record = store.get(cid)
    assert record is not None
    assert record.definition.name == "before"
    assert record.messages == []


def test_process_designer_has_no_turn_lock_surface() -> None:
    # Pins the current, intentional asymmetry so a future turn-lock migration
    # for Process Designer fails this assertion loudly, prompting an update to
    # this suite's turn-lock parity scope rather than silently drifting.
    assert not hasattr(ProcessDesignerAgent, "turn")
    assert "InMemoryTurnLocks" not in vars(agent_module)


# ---------------------------------------------------------------------------
# Merge semantics: each assistant's strategy is a deliberate, documented
# divergence (assistant_kernel/fenced_json.py's module docstring), not a bug —
# pin Process Designer's wholesale-rebuild side of that contrast here. Agent
# Studio's keyed-overlay side is already covered in
# agent_studio/tests/test_assistant.py::test_merge_definition_partial_states_preserve_prior_edits.
# ---------------------------------------------------------------------------


def test_process_designer_rebuilds_wholesale_unlike_agent_studios_keyed_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _dict_to_process(
        {
            "name": "Support Triage",
            "steps": [
                {
                    "step_id": "step_1",
                    "name": "Triage",
                    "description": "Classify",
                    "step_type": "action",
                    "agents": [],
                    "next_steps": [],
                }
            ],
        },
        existing_id="p1",
    )
    partial_reply = 'Updating the name only.\n\n```process\n{"name": "Renamed Triage"}\n```\n'
    _stub_agent_reply(monkeypatch, partial_reply)

    _, updated, _, _ = ProcessDesignerAgent().respond(
        conversation_history=[], current_process=current, user_message="rename it"
    )

    assert updated is not None
    assert updated.name == "Renamed Triage"
    assert updated.process_id == "p1"  # the id is the one thing carried forward
    assert updated.steps == []  # wholesale rebuild drops the untouched steps
