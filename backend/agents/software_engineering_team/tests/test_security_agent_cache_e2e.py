"""End-to-end regression proof that ``security_agent`` never gets Anthropic
wire-level prompt caching.

Unlike ``qa_agent`` (see ``test_qa_agent_cache_e2e.py``),
``CybersecurityExpertAgent.run`` calls ``run_single_shot_review`` ->
``LLMClient.complete_json`` directly rather than going through the
Strands/``LLMClientModel`` adapter -- and every ``complete_json``
implementation (Claude/Ollama/RunPod/Dummy) has no ``system_prompt_content``
kwarg to elevate trusted content into. So ``task_description``, ``context``,
and ``architecture`` all stay in the user message (see
``security_agent.agent._build_security_role_instructions`` /
``CybersecurityExpertAgent._build_user_prompt``'s docstring), and no
``CacheBreakpoint`` is ever constructed for Security.

Mirrors ``test_tool_agent_base_shared_review_cache_e2e.py``'s real-
``ClaudeLLMClient`` wire-level regression-proof pattern (rather than
``test_qa_agent_cache_e2e.py``'s positive proof, since there is nothing to
cache-hit here) -- a tripwire so a future change that starts elevating any of
Security's trusted fields into ``system_prompt_content`` gets caught here
rather than shipping quietly.

Security also carries its own whole-input ``ReviewResultCache`` (unrelated to
Anthropic wire-level prompt caching): disabled here via
``SECURITY_REVIEW_CACHE_SIZE=0`` so both calls are genuine LLM calls.
"""

from __future__ import annotations

import json

import pytest
from security_agent.agent import CybersecurityExpertAgent
from security_agent.models import SecurityInput

from llm_client_fakes import _make_claude_client, _text_message
from llm_service import telemetry
from shared.dev_models.models import SystemArchitecture

pytestmark = [pytest.mark.usefixtures("_reset_llm_telemetry_state")]

_TASK_MARKER = "Add pagination to the users endpoint"


def _security_input(code: str) -> SecurityInput:
    return SecurityInput(
        code=code,
        language="python",
        task_description=_TASK_MARKER,
        context="Internal microservice, no external exposure.",
        architecture=SystemArchitecture(
            overview="Architecture overview shared across this microtask.",
            components=[],
            architecture_document="",
        ),
    )


def _clean_reply() -> str:
    return json.dumps({"vulnerabilities": [], "summary": "clean", "remediations": []})


def test_security_calls_never_get_wire_level_cache_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two Security calls in one microtask never produce a cache read/write on
    the wire, and the trusted task description never leaves the user turn for
    the system prompt -- there is no ``CacheBreakpoint`` for Security to have
    marked."""
    monkeypatch.setenv("SECURITY_REVIEW_CACHE_SIZE", "0")
    client, fake_messages = _make_claude_client(
        [
            _text_message(_clean_reply(), cache_read_input_tokens=0, cache_creation_input_tokens=0),
            _text_message(_clean_reply(), cache_read_input_tokens=0, cache_creation_input_tokens=0),
        ]
    )
    agent = CybersecurityExpertAgent(client)

    agent.run(_security_input("def list_users(): ..."))
    agent.run(_security_input("def paginate(items): ..."))

    calls = telemetry.get_recent_calls()
    assert len(calls) == 2
    for call in calls:
        assert call["cache_read_tokens"] == 0
        assert call["cache_creation_tokens"] == 0

    assert len(fake_messages.captured_calls) == 2
    for call in fake_messages.captured_calls:
        system_content = call.get("system")
        rendered_system = json.dumps(system_content) if system_content else ""
        assert '"cache_control"' not in rendered_system, (
            f"unexpected cache_control block in system content: {system_content}"
        )
        assert _TASK_MARKER not in rendered_system, f"task leaked into system: {system_content}"
        rendered_messages = json.dumps(call["messages"])
        assert _TASK_MARKER in rendered_messages
