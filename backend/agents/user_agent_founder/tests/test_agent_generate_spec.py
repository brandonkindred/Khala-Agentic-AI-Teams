"""Regression test for the Phase 1 spec-generation routing fix.

The "Startup Founder Testing Persona" run previously failed with
``LLMJsonParseError`` because ``FounderAgent.generate_spec`` sent a Markdown
prompt through the Strands ``Agent`` → ``chat_json_round`` path, which forces
``response_format=json_object`` on the underlying Ollama client. After the fix,
``generate_spec`` routes through ``FounderAgent._call_text`` which calls
``LLMClient.complete`` directly (a text-only path — no JSON mode).

This test pins that contract by:

1. Asserting the Markdown preview from the failing run is returned unchanged.
2. Asserting the Strands agent (and therefore ``chat_json_round``) is never
   invoked for the spec-generation call.
"""

from __future__ import annotations

from unittest.mock import MagicMock

FAILING_RUN_PREVIEW = (
    "# TaskFlow MVP Specification v0.1\n"
    "**Author:** Alex Chen  \n"
    "**Status:** Approved for Build  \n"
    "**Target Ship Date:** 14 Days from Now  \n\n"
    "---\n\n"
    "## 1. Product Overview\n"
    "TaskFlow is a real-time task management tool for small teams (2-10 people) "
    "who are drowning in configuration overhead.\n"
)


def _make_agent_without_init(strands_agent_stub: object) -> object:
    """Construct a FounderAgent without running ``__init__``.

    Bypasses the real Strands ``Agent`` construction (which would need the
    ``strands`` package and a live model). We still assign a canary object to
    ``self._agent`` so the test can detect if anything accidentally routes
    through the Strands path.
    """
    from user_agent_founder import agent as agent_module

    founder = agent_module.FounderAgent.__new__(agent_module.FounderAgent)
    founder._agent = strands_agent_stub
    return founder


def test_generate_spec_returns_markdown_unchanged(monkeypatch):
    """generate_spec must return the raw Markdown reply verbatim."""
    fake_client = MagicMock()
    fake_client.complete.return_value = FAILING_RUN_PREVIEW
    fake_client.chat_json_round = MagicMock(
        side_effect=AssertionError("chat_json_round must not be called by generate_spec"),
    )

    # ``_call_text`` performs ``from llm_service import get_client`` at call
    # time — patch the module attribute so the stub is returned.
    import llm_service

    monkeypatch.setattr(llm_service, "get_client", lambda agent_key=None: fake_client)

    strands_agent_spy = MagicMock(
        side_effect=AssertionError("Strands Agent must not be invoked by generate_spec"),
    )
    founder = _make_agent_without_init(strands_agent_spy)

    result = founder.generate_spec()

    assert result == FAILING_RUN_PREVIEW.strip()


def test_generate_spec_bypasses_strands_json_transport(monkeypatch):
    """The Strands agent (and hence chat_json_round) must never fire for spec generation."""
    fake_client = MagicMock()
    fake_client.complete.return_value = "# Some spec\n\nWith markdown body."
    fake_client.chat_json_round = MagicMock(
        side_effect=AssertionError("chat_json_round must not be called by generate_spec"),
    )

    import llm_service

    monkeypatch.setattr(llm_service, "get_client", lambda agent_key=None: fake_client)

    strands_agent_spy = MagicMock(
        side_effect=AssertionError("Strands Agent must not be invoked by generate_spec"),
    )
    founder = _make_agent_without_init(strands_agent_spy)

    founder.generate_spec()

    # client.complete is the text-only path — must be hit exactly once.
    fake_client.complete.assert_called_once()
    call_kwargs = fake_client.complete.call_args.kwargs
    # System prompt must be plumbed through so the founder persona is preserved
    # now that we no longer rely on Strands' system_prompt wiring.
    assert "system_prompt" in call_kwargs
    assert call_kwargs["system_prompt"] is not None

    # chat_json_round and the Strands agent must be untouched.
    fake_client.chat_json_round.assert_not_called()
    strands_agent_spy.assert_not_called()


def test_generate_spec_uses_founder_system_prompt(monkeypatch):
    """The founder persona's system prompt must be forwarded to client.complete."""
    from user_agent_founder import agent as agent_module

    fake_client = MagicMock()
    fake_client.complete.return_value = "ok"

    import llm_service

    monkeypatch.setattr(llm_service, "get_client", lambda agent_key=None: fake_client)

    founder = _make_agent_without_init(MagicMock())
    founder.generate_spec()

    call_kwargs = fake_client.complete.call_args.kwargs
    assert call_kwargs["system_prompt"] == agent_module.FOUNDER_SYSTEM_PROMPT
    # The user-facing prompt must still be the spec generation prompt.
    sent_prompt = fake_client.complete.call_args.args[0]
    assert sent_prompt == agent_module.SPEC_GENERATION_PROMPT
