"""Regression coverage for the Story 5b Phase 2 own-fields refactor.

Every other branding_team test either mocks ``build_branding_graph`` entirely
(``test_orchestrator.py``) or drives a single agent in isolation
(``test_dummy_structured_output_contract.py``). Neither observes what Strands
actually injects into a node's turn as "Inputs from previous nodes" -- that
content is assembled by the Strands Graph engine at invocation time and isn't
constructible from this repo's code. This module runs the real Phase 2 graph
end-to-end under the ``dummy`` provider (the same real-event-loop mechanism
``test_real_agent_event_loop_routes_deterministically_despite_misleading_prompt``
already proves works under CI) and inspects the exact ``messages`` the
VoicePrinciplesDrafter turn received, to lock in that it carries only
read-only context from its immediate predecessor -- not the full upstream
narrative history a pre-refactor cumulative-inheritance schema would have
forced it to re-emit.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from branding_team.graphs.phase2_narrative import build_phase2_graph
from branding_team.graphs.shared import serialize_mission
from branding_team.shared.coro_runner import run_coroutine
from branding_team.tests.conftest import make_mission

# These four stub builders are private (``_``-prefixed) implementation
# details of DummyLLMClient's Phase 2 routing, not a published test API.
# They're imported anyway -- deliberately -- because this test's whole point
# is to compare Voice's real captured input against the *exact* deterministic
# content the other five specialists' dummy turns produce; duplicating that
# content by hand here would silently drift from the real stubs and defeat
# the regression check. A future refactor of the dummy client's internals is
# expected to require updating these imports.
from llm_service.clients.dummy import (
    DummyLLMClient,
    _branding_phase2_archetypes_stub,
    _branding_phase2_messaging_stub,
    _branding_phase2_personas_stub,
    _branding_phase2_story_stub,
    _branding_phase2_tagline_stub,
)


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


def _find_voice_call(captured_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the one captured call whose tools name the ``WritingGuidelinesOutput``
    structured-output tool -- Strands names that tool after the model class,
    the same fact ``DummyLLMClient`` itself routes on."""
    voice_calls = [
        call
        for call in captured_calls
        if any(
            ((tool or {}).get("function") or {}).get("name") == "WritingGuidelinesOutput"
            for tool in (call["tools"] or [])
        )
    ]
    assert len(voice_calls) == 1, (
        f"expected exactly one VoicePrinciplesDrafter turn, found {len(voice_calls)} "
        f"among {len(captured_calls)} total chat() calls"
    )
    return voice_calls[0]


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


def test_voice_step_input_excludes_full_upstream_narrative_payload() -> None:
    """Locks in Story 5b: Voice's real per-invocation input carries only its
    immediate predecessor's (PersonaBuilder's) read-only context, not the
    full five-specialist narrative history a cumulative-inheritance schema
    would have forced it to re-emit.

    Fails if Phase 2 reverts to cumulative carry-forward (the excluded
    specialists' content would reappear in Voice's turn) or if the
    single-predecessor edge chain is broken (e.g. a fan-in that hands every
    upstream node's output to Voice at once).
    """
    captured_calls, task = _run_phase2_graph_capturing_chat_calls()
    assert len(captured_calls) == 6, (
        f"expected one chat() call per Phase 2 specialist, got {len(captured_calls)}"
    )

    voice_messages = _find_voice_call(captured_calls)["messages"]
    voice_text = _plain_text(voice_messages)
    user_messages = [m for m in voice_messages if m.get("role") == "user"]
    assert len(user_messages) == 1, "expected exactly one user message in Voice's turn"
    user_content = user_messages[0]["content"]

    story_stub = _branding_phase2_story_stub()
    archetypes_stub = _branding_phase2_archetypes_stub()
    tagline_stub = _branding_phase2_tagline_stub()
    messaging_stub = _branding_phase2_messaging_stub()

    # Non-immediate-predecessor specialists' content must not leak into
    # Voice's turn -- this is the "no full upstream payload" assertion.
    assert story_stub["brand_story"] not in voice_text
    assert story_stub["hero_narrative"] not in voice_text
    assert archetypes_stub["brand_archetypes"][0]["rationale"] not in voice_text
    assert tagline_stub["tagline"] not in voice_text
    assert messaging_stub["messaging_framework"][0]["key_message"] not in voice_text

    # Sanity check: the capture is genuinely Voice's turn and genuinely
    # carries its immediate predecessor's (PersonaBuilder's) read-only
    # context -- otherwise the absence assertions above would pass vacuously
    # on an empty/wrong capture.
    personas_stub = _branding_phase2_personas_stub()
    assert personas_stub["persona_profiles"][0]["name"] in voice_text

    # Size assertion, relative to a concrete pre-refactor baseline proxy.
    # Voice's user-turn content is "Original Task: <task>" plus Strands'
    # "Inputs from previous nodes" section for its one predecessor
    # (PersonaBuilder) -- i.e. the task (the exact string the graph was
    # invoked with, returned above rather than recomputed here), PersonaBuilder's
    # own dumped fragment, and a small fixed label overhead. A
    # cumulative-inheritance regression would instead add whole extra
    # fragments (171-648 chars each, per story/archetypes/tagline/messaging
    # above) on top of that -- far bigger than the label-formatting margin
    # allowed here.
    persona_fragment_size = len(json.dumps(personas_stub, separators=(",", ":")))
    # Observed overhead is ~78 chars; margin is well under the smallest excluded fragment (171).
    label_formatting_margin = 150
    expected_upper_bound = len(task) + persona_fragment_size + label_formatting_margin
    assert len(user_content) <= expected_upper_bound
