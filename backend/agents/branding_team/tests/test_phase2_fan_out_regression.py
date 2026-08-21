"""Regression coverage for Phase 2's pure fan-out topology.

Every other branding_team test either mocks ``build_branding_graph`` entirely
(``test_orchestrator.py``) or drives a single agent in isolation
(``test_dummy_structured_output_contract.py``). Neither observes what Strands
actually injects into each node's turn -- that content is assembled by the
Strands Graph engine at invocation time and isn't constructible from this
repo's code. This module runs the real Phase 2 graph end-to-end under the
``dummy`` provider (the same real-event-loop mechanism
``test_real_agent_event_loop_routes_deterministically_despite_misleading_prompt``
already proves works under CI) and inspects the exact ``messages`` every one
of the six specialist turns received, to lock in that Phase 2's zero-edge
fan-out means no specialist ever receives another specialist's output --
unlike a sequential chain (or any fan-in), where a downstream node's turn
would carry an upstream sibling's fragment via Strands' injected "Inputs
from previous nodes" section.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from pydantic import BaseModel

from branding_team.graphs.phase2_narrative import build_phase2_graph
from branding_team.graphs.shared import serialize_mission
from branding_team.models import (
    BrandArchetypesOutput,
    BrandStoryOutput,
    MessagingFrameworkOutput,
    PersonaProfilesOutput,
    TaglineOutput,
    WritingGuidelinesOutput,
)
from branding_team.shared.coro_runner import run_coroutine
from branding_team.tests.conftest import make_mission
from llm_service import DummyLLMClient

# Node id -> the structured-output tool name Strands names after its model
# class -- the same fact ``DummyLLMClient`` itself routes on.
_NODE_TOOL_NAMES = {
    "Storyteller": "BrandStoryOutput",
    "ArchetypeAnalyst": "BrandArchetypesOutput",
    "TaglineWriter": "TaglineOutput",
    "MessageMapper": "MessagingFrameworkOutput",
    "PersonaBuilder": "PersonaProfilesOutput",
    "VoicePrinciplesDrafter": "WritingGuidelinesOutput",
}


def _dummy_stub(output_model: type[BaseModel]) -> dict[str, Any]:
    """Deterministic dummy payload for ``output_model``, via the public
    ``DummyLLMClient`` contract -- ``complete_json`` routes by
    ``structured_output_model``'s class name, independent of prompt text
    (see ``test_dummy_stub_alignment.py``), so the prompt here is a
    placeholder."""
    return DummyLLMClient().complete_json("unused", structured_output_model=output_model)


def _run_phase2_graph_capturing_chat_calls() -> tuple[list[dict[str, Any]], str]:
    """Run the real Phase 2 graph under the dummy provider, recording every
    ``DummyLLMClient.chat`` call's ``messages``/``tools`` in invocation order.

    Preconditions:
        ``LLM_PROVIDER=dummy`` (set by ``tests/conftest.py`` for the whole
        suite).
    Postconditions:
        Returns ``(calls, task)``: one entry per real ``chat()`` call Strands
        made while running ``build_phase2_graph().invoke_async(task)`` to
        completion, each a ``{"messages": ..., "tools": ...}`` dict -- the
        exact arguments that call received, captured before delegating to
        the real implementation so the run's own output is unaffected -- and
        the exact ``task`` string the graph was invoked with, so callers
        never need to recompute an equivalent one themselves.
    """
    captured_calls: list[dict[str, Any]] = []
    original_chat = DummyLLMClient.chat

    def spy_chat(self: DummyLLMClient, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        captured_calls.append({"messages": messages, "tools": kwargs.get("tools")})
        return original_chat(self, messages, **kwargs)

    mission = make_mission()
    task = (
        f"Create a comprehensive brand strategy for the following company.\n\n"
        f"Branding Mission:\n{serialize_mission(mission)}"
    )
    with patch.object(DummyLLMClient, "chat", spy_chat):
        run_coroutine(build_phase2_graph().invoke_async(task))
    return captured_calls, task


def _find_call_for(captured_calls: list[dict[str, Any]], tool_name: str) -> dict[str, Any]:
    """Return the one captured call whose tools name *tool_name*."""
    matches = [
        call
        for call in captured_calls
        if any(
            ((tool or {}).get("function") or {}).get("name") == tool_name
            for tool in (call["tools"] or [])
        )
    ]
    assert len(matches) == 1, (
        f"expected exactly one {tool_name} turn, found {len(matches)} "
        f"among {len(captured_calls)} total chat() calls"
    )
    return matches[0]


def _plain_text(messages: list[dict[str, Any]]) -> str:
    """Concatenate every message's raw ``content`` string, without re-encoding
    as JSON -- ``json.dumps(messages)`` would backslash-escape the quotes
    already inside each message's own JSON-shaped content, inflating length
    comparisons with encoding noise rather than real payload size.

    Preconditions:
        Every message's ``content`` is a plain string -- true for every real
        ``DummyLLMClient.chat()`` call this test observes. Asserted rather
        than silently ``str()``-coerced, so a future Strands message-shape
        change (e.g. structured content blocks) fails loudly here instead of
        comparing against a meaningless Python repr.
    """
    parts: list[str] = []
    for m in messages:
        content = m.get("content", "")
        assert isinstance(content, str), (
            f"expected str message content, got {type(content).__name__}"
        )
        parts.append(content)
    return "\n".join(parts)


def _user_content(messages: list[dict[str, Any]]) -> str:
    user_messages = [m for m in messages if m.get("role") == "user"]
    assert len(user_messages) == 1, "expected exactly one user message per specialist turn"
    return user_messages[0]["content"]


# One distinguishing (node_id, distinguishing value) pair per specialist,
# derived from its own dummy stub -- used to prove that value never leaks
# into any *other* specialist's turn.
def _distinguishing_values() -> dict[str, str]:
    story_stub = _dummy_stub(BrandStoryOutput)
    archetypes_stub = _dummy_stub(BrandArchetypesOutput)
    tagline_stub = _dummy_stub(TaglineOutput)
    messaging_stub = _dummy_stub(MessagingFrameworkOutput)
    personas_stub = _dummy_stub(PersonaProfilesOutput)
    voice_stub = _dummy_stub(WritingGuidelinesOutput)
    return {
        "Storyteller": story_stub["brand_story"],
        "ArchetypeAnalyst": archetypes_stub["brand_archetypes"][0]["rationale"],
        "TaglineWriter": tagline_stub["tagline"],
        "MessageMapper": messaging_stub["messaging_framework"][0]["key_message"],
        "PersonaBuilder": personas_stub["persona_profiles"][0]["name"],
        "VoicePrinciplesDrafter": voice_stub["writing_guidelines"]["voice_principles"][0],
    }


def test_phase2_pure_fan_out_never_leaks_a_sibling_specialists_output() -> None:
    """Locks in Phase 2's zero-edge fan-out: no specialist's turn ever carries
    another specialist's output, in either direction.

    Fails if Phase 2 reverts to a sequential chain (a downstream specialist's
    turn would then carry an upstream sibling's fragment) or gains any fan-in
    (any specialist's turn would carry more than one sibling's fragment).
    """
    captured_calls, _task = _run_phase2_graph_capturing_chat_calls()
    assert len(captured_calls) == 6, (
        f"expected one chat() call per Phase 2 specialist, got {len(captured_calls)}"
    )

    distinguishing = _distinguishing_values()

    for node_id, tool_name in _NODE_TOOL_NAMES.items():
        call = _find_call_for(captured_calls, tool_name)
        text = _plain_text(call["messages"])
        for other_node_id, value in distinguishing.items():
            if other_node_id == node_id:
                continue
            assert value not in text, (
                f"{other_node_id}'s output leaked into {node_id}'s turn -- Phase 2 "
                "must remain a zero-edge fan-out"
            )


def test_phase2_specialist_turn_carries_only_the_original_task() -> None:
    """Every specialist is an entry point with no predecessor, so its turn's
    user content must be (approximately) just the task Strands was invoked
    with -- no injected "Inputs from previous nodes" section from any
    sibling.

    The margin below is a generous allowance for Strands' own task-framing
    overhead (e.g. an "Original Task:" label), not for any injected fragment:
    the smallest excluded fragment observed across the six dummy stubs is
    well over 100 chars, so a real leak still fails this bound.
    """
    captured_calls, task = _run_phase2_graph_capturing_chat_calls()
    formatting_margin = 150

    for tool_name in _NODE_TOOL_NAMES.values():
        call = _find_call_for(captured_calls, tool_name)
        user_content = _user_content(call["messages"])
        assert task in user_content
        assert len(user_content) <= len(task) + formatting_margin, (
            f"{tool_name}'s turn is larger than a bare task allows -- "
            f"got {len(user_content)} chars, expected <= {len(task) + formatting_margin}"
        )


def test_dummy_stub_json_encoding_sanity() -> None:
    """Sanity check that the dummy stubs used above actually serialize to
    non-trivial JSON -- otherwise the leak assertions above would pass
    vacuously against empty/degenerate fragments."""
    for value in _distinguishing_values().values():
        assert isinstance(value, str) and value.strip()
        assert len(json.dumps(value)) > 2
