"""Parity/regression tests: both designer assistants against the shared kernel.

``AgentDesignerAgent`` (``agent_team_studio.agent_studio.assistant``) and
``ProcessDesignerAgent`` (``agent_team_studio.agentic_team_provisioning.assistant.agent``)
both call the kernel's ``parse_fenced_json``/``strip_fenced_blocks``
(:mod:`agent_team_studio.assistant_kernel.fenced_json``) instead of hand-rolled
parsers. The kernel primitives themselves are already unit-tested in isolation
(``test_fenced_json.py``, ``test_turn_lock.py``); the local test modules for
each assistant (``agent_studio/tests/test_assistant.py``,
``agentic_team_provisioning/tests/test_assistant_agent.py``) cover each
assistant's *own* merge/wiring logic. Neither proves the two assistants
actually converge on identical kernel behavior side by side. This module is
that missing side-by-side check: it drives each assistant through its public
``respond()`` (the same LLM-completion-stubbing seams the existing test
modules use) and asserts the parsing/stripping edge cases the kernel promises
hold equally for both.

Merge *policy* is intentionally NOT asserted to match between the two
assistants here: Agent Studio overlays fields onto the current draft
(``_merge_definition``), Process Designer rebuilds the draft wholesale
(``_dict_to_process``) — see ``fenced_json.py``'s module docstring. Parity is
about both assistants using the same kernel *parsing* contract, not producing
identical merge outputs.

Turn-lock is also asymmetric by design, not by omission: only Agent Studio's
``AgentStudioConversationStore`` wraps the kernel's ``InMemoryTurnLocks``.
``agentic_team_provisioning``'s ``AgenticTeamStore`` has no ``turn()`` at all
(see ``turn_lock.py``'s module docstring: "agentic_team_provisioning's
conversation routes have no equivalent lock at all today"). Rather than
silently skip "turn-lock for both assistants," this module asserts the
asymmetry explicitly, so the day someone wires up a Process Designer turn
lock, this test fails and points back here.
"""

from __future__ import annotations

import pytest
import strands

from agent_team_studio.agent_studio.assistant import AgentDesignerAgent
from agent_team_studio.agent_studio.models import AgentDefinition
from agent_team_studio.agent_studio.store import AgentStudioConversationStore
from agent_team_studio.agentic_team_provisioning.assistant import agent as process_agent_module
from agent_team_studio.agentic_team_provisioning.assistant.agent import ProcessDesignerAgent
from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.assistant_kernel import ConversationTurn, InMemoryTurnLocks

# ---------------------------------------------------------------------------
# LLM-completion stubbing (mirrors each assistant's own test module)
# ---------------------------------------------------------------------------


def _agent_studio_complete(text: str):
    """A CompleteFn for AgentDesignerAgent that always returns ``text``."""

    def complete(system_prompt: str, prompt: str) -> str:
        return text

    return complete


@pytest.fixture(autouse=True)
def _stub_process_designer_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    # ProcessDesignerAgent.respond() calls get_strands_model(...) before
    # constructing strands.Agent; stub both so no live LLM provider is needed.
    monkeypatch.setattr(process_agent_module, "get_strands_model", lambda *a, **k: object())


def _stub_process_designer_reply(monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    class _FixedReplyAgent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            return reply

    monkeypatch.setattr(strands, "Agent", _FixedReplyAgent)


# ---------------------------------------------------------------------------
# Fenced-JSON parsing parity
# ---------------------------------------------------------------------------


def test_no_primary_block_yields_no_update_for_both_assistants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Neither assistant fabricates an update from prose with no fenced block
    # for its primary tag — both degrade to "no parseable update", matching
    # the kernel's parse_fenced_json contract of returning None rather than
    # raising or guessing.
    prose_only = "Sure, tell me more about what you want first."

    reply, updated, suggestions = AgentDesignerAgent(
        complete=_agent_studio_complete(prose_only)
    ).respond([], AgentDefinition(name="x", role="r"), "hi")
    assert updated is None
    assert reply == prose_only

    _stub_process_designer_reply(monkeypatch, prose_only)
    reply2, process, suggestions2, agents_data = ProcessDesignerAgent().respond(
        conversation_history=[], current_process=None, user_message="hi"
    )
    assert process is None
    assert agents_data is None
    assert reply2 == prose_only


def test_malformed_json_in_primary_block_never_raises_for_either_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A malformed JSON body inside an otherwise-correct fence must degrade to
    # "no update", not raise json.JSONDecodeError up through respond() — the
    # kernel's parse_fenced_json swallows ValueError/RecursionError, and both
    # assistants must not reintroduce a raising path around it.
    agent_studio_reply = "Here you go.\n\n```agent\n{not valid json}\n```\n"
    reply, updated, _ = AgentDesignerAgent(
        complete=_agent_studio_complete(agent_studio_reply)
    ).respond([], AgentDefinition(name="x", role="r"), "hi")
    assert updated is None

    process_reply = "Here you go.\n\n```process\n{not valid json}\n```\n"
    _stub_process_designer_reply(monkeypatch, process_reply)
    _, process, _, _ = ProcessDesignerAgent().respond(
        conversation_history=[], current_process=None, user_message="hi"
    )
    assert process is None


def test_sibling_tag_never_cross_matches_primary_tag_for_either_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A block for a longer tag that merely starts with the assistant's real
    # tag -- Agent Studio's "agent" vs. a decoy "agent-v2", Process Designer's
    # "process" vs. a decoy "process-v2" -- must never be read as the primary
    # block. This is the kernel's whitespace-boundary regex guarantee,
    # asserted here at the assistant boundary; a decoy tag that shares no
    # prefix with the real tag (e.g. Process Designer's own "agents" block)
    # would pass even if the boundary defense regressed, so both decoys below
    # are word-extensions of the real tag.
    agent_studio_reply = (
        'Here\'s a variant, not the real update.\n\n```agent-v2\n{"name": "decoy"}\n```\n'
    )
    reply, updated, _ = AgentDesignerAgent(
        complete=_agent_studio_complete(agent_studio_reply)
    ).respond([], AgentDefinition(name="x", role="r"), "hi")
    assert updated is None
    # The decoy block is for an unrecognized tag, so it is left in the reply
    # untouched -- strip_fenced_blocks only strips the assistant's own tags.
    assert "agent-v2" in reply

    process_reply = (
        'Here\'s a variant, not the real update.\n\n```process-v2\n{"name": "decoy"}\n```\n'
    )
    _stub_process_designer_reply(monkeypatch, process_reply)
    reply2, process, _, _ = ProcessDesignerAgent().respond(
        conversation_history=[], current_process=None, user_message="hi"
    )
    assert process is None
    assert "process-v2" in reply2


def test_suggestions_block_normalizes_identically_for_both_assistants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both assistants extract the "suggestions" tag via the same kernel call
    # and normalize it to list[str] identically -- including str()-coercing a
    # non-string element, so a regression that dropped the str() call (and
    # just returned the parsed list unchanged) would be caught here.
    suggestions_fence = '```suggestions\n["Add a word_count input?", 3]\n```\n'
    expected = ["Add a word_count input?", "3"]

    agent_studio_reply = f"Some prose.\n\n{suggestions_fence}"
    _, _, suggestions = AgentDesignerAgent(
        complete=_agent_studio_complete(agent_studio_reply)
    ).respond([], AgentDefinition(name="x", role="r"), "hi")
    assert suggestions == expected

    process_reply = f"Some prose.\n\n{suggestions_fence}"
    _stub_process_designer_reply(monkeypatch, process_reply)
    _, _, suggestions2, _ = ProcessDesignerAgent().respond(
        conversation_history=[], current_process=None, user_message="hi"
    )
    assert suggestions2 == expected


def test_stripped_reply_never_leaks_fenced_blocks_for_either_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both assistants must strip every one of their own tags from the visible
    # reply -- a partial strip_fenced_blocks tag list would leak a raw fence
    # into the user-facing prose.
    agent_studio_reply = (
        "Here's a draft.\n\n"
        '```agent\n{"name": "n", "role": "r"}\n```\n\n'
        '```suggestions\n["one?"]\n```\n'
    )
    reply, _, _ = AgentDesignerAgent(complete=_agent_studio_complete(agent_studio_reply)).respond(
        [], AgentDefinition(name="x", role="r"), "hi"
    )
    assert "```agent" not in reply
    assert "```suggestions" not in reply
    assert reply == "Here's a draft."

    process_reply = (
        "Here's a draft.\n\n"
        '```agents\n[{"agent_name": "A", "role": "R"}]\n```\n\n'
        '```process\n{"name": "n", "description": "d", '
        '"trigger": {"trigger_type": "message", "description": "t"}, '
        '"steps": [], "output": {"description": "o", "destination": "dest"}}\n```\n\n'
        '```suggestions\n["one?"]\n```\n'
    )
    _stub_process_designer_reply(monkeypatch, process_reply)
    reply2, _, _, _ = ProcessDesignerAgent().respond(
        conversation_history=[], current_process=None, user_message="hi"
    )
    assert "```agents" not in reply2
    assert "```process" not in reply2
    assert "```suggestions" not in reply2
    assert reply2 == "Here's a draft."


# ---------------------------------------------------------------------------
# Turn-lock parity/asymmetry
# ---------------------------------------------------------------------------


def test_agent_studio_store_wraps_the_shared_turn_lock() -> None:
    # Agent Studio's conversation store is turn-lock-conformant via the
    # kernel's InMemoryTurnLocks/ConversationTurn, not a local reimplementation.
    store = AgentStudioConversationStore()
    assert isinstance(store._turn_locks, InMemoryTurnLocks)
    cid = store.create("new", None, AgentDefinition(name="x", role="r"))
    with store.turn(cid) as t:
        assert isinstance(t, ConversationTurn)
        t.append_message("user", "hi")
    assert [m.content for m in store.get(cid).messages] == ["hi"]


def test_process_designer_store_has_no_turn_lock() -> None:
    """Process Designer's store intentionally has no turn-lock (yet).

    Per ``assistant_kernel.turn_lock``'s module docstring, wiring a turn lock
    up for ``agentic_team_provisioning`` "remains a follow-up" and was never
    part of the kernel-extraction migration. This test pins that current,
    intentional gap down explicitly rather than leaving "turn-lock parity for
    both assistants" silently unchecked: if a turn lock is ever added to
    ``AgenticTeamStore``, this assertion starts failing and should be updated
    (and ideally replaced with a real turn-lock parity test) rather than left
    broken.
    """
    assert not hasattr(AgenticTeamStore, "turn")
