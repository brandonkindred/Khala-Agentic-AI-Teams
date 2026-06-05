"""Tests for complete_json_with_tool_loop."""

from __future__ import annotations

import json

import pytest

from llm_service.clients.dummy import DummyLLMClient
from llm_service.interface import LLMPermanentError
from llm_service.tool_loop import _normalize_tool_calls_for_api, complete_json_with_tool_loop


def test_normalize_tool_calls_serializes_dict_arguments() -> None:
    raw = [
        {
            "id": "1",
            "type": "function",
            "function": {"name": "git_status", "arguments": {"staged": True}},
        }
    ]
    norm = _normalize_tool_calls_for_api(raw)
    assert isinstance(norm[0]["function"]["arguments"], str)
    assert json.loads(norm[0]["function"]["arguments"]) == {"staged": True}


def test_tool_loop_dummy_runs_git_handler_then_returns_json() -> None:
    llm = DummyLLMClient()
    calls: list[str] = []

    def git_status(_args: dict) -> dict:
        calls.append("git_status")
        return {"success": True, "stdout": ""}

    handlers = {"git_status": git_status}
    tools = [
        {
            "type": "function",
            "function": {
                "name": "git_status",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    out = complete_json_with_tool_loop(
        llm,
        user_prompt="Task: **Task:** demo",
        system_prompt="You are a Senior Software Engineer. Output JSON with files_to_create_or_edit.",
        tools=tools,
        tool_handlers=handlers,
        max_rounds=8,
        temperature=0.0,
        think=False,
    )
    assert calls == ["git_status"]
    assert out.get("summary")
    assert isinstance(out.get("files_to_create_or_edit"), list)


def test_tool_loop_max_rounds() -> None:
    class LoopLLM(DummyLLMClient):
        def chat(self, messages, **kwargs):  # type: ignore[override]
            return {"__tool_calls__": [{"id": "x", "function": {"name": "a", "arguments": {}}}]}

    with pytest.raises(LLMPermanentError, match="max_rounds"):
        complete_json_with_tool_loop(
            LoopLLM(),
            user_prompt="u",
            system_prompt="You are a Senior Software Engineer. files_to_create_or_edit.",
            tools=[
                {
                    "type": "function",
                    "function": {"name": "a", "parameters": {"type": "object"}},
                }
            ],
            tool_handlers={"a": lambda _: {}},
            max_rounds=2,
            think=False,
        )


def test_tool_loop_echoes_reasoning_content_on_assistant_turn() -> None:
    """DeepSeek thinking mode 400s when a tool-call turn's reasoning_content
    is not passed back; the loop must attach it to the assistant message."""
    from llm_service.clients.dummy import DummyLLMClient
    from llm_service.tool_loop import complete_json_with_tool_loop

    class ReasoningLLM(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.seen_messages: list = []
            self.round = 0

        def chat(self, messages, **kwargs):  # type: ignore[override]
            self.round += 1
            self.seen_messages.append([dict(m) for m in messages])
            if self.round == 1:
                return {
                    "__tool_calls__": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "noop", "arguments": {}},
                        }
                    ],
                    "__reasoning_content__": "thought about it",
                }
            return {"done": True}

    llm = ReasoningLLM()
    result = complete_json_with_tool_loop(
        llm,
        system_prompt="s",
        user_prompt="u",
        tools=[{"type": "function", "function": {"name": "noop", "parameters": {}}}],
        tool_handlers={"noop": lambda args: {"ok": True}},
    )
    assert result == {"done": True}
    second_round = llm.seen_messages[1]
    assistant = next(
        m for m in second_round if m.get("role") == "assistant" and m.get("tool_calls")
    )
    # Both dialects must be present: Ollama-compat reads `reasoning`,
    # DeepSeek-native reads `reasoning_content`.
    assert assistant.get("reasoning") == "thought about it"
    assert assistant.get("reasoning_content") == "thought about it"
