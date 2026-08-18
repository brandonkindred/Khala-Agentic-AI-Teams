"""End-to-end coverage for the prompt-caching primitive: a ``CacheBreakpoint``
placed in ``system_prompt_content``, plumbed through the Strands model wrapper,
observed as cache-hit telemetry on a real repeated call.

Existing suites each cover one piece in isolation: ``test_cache_breakpoint.py``
covers the marker's own contract; ``test_strands_adapter.py`` covers the
wrapper's preserve/flatten branch with a canned single response;
``test_claude_client.py`` covers wire-format translation and telemetry with a
single mocked Anthropic response carrying a canned ``cache_read_input_tokens``.
None of them drive *two* calls with the identical marked prefix through the
full client -> wrapper -> provider-client -> telemetry path, so none actually
prove that a repeated prefix reads back a non-zero cache hit on the second
call while the wire payload and output stay identical between the two calls.
That is what this module asserts.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from llm_service import telemetry
from llm_service.cache_breakpoint import CacheBreakpoint
from llm_service.clients.claude import ClaudeLLMClient
from llm_service.interface import reset_complete_json_observer_state
from llm_service.strands_adapter import LLMClientModel


@pytest.fixture(autouse=True)
def _reset_observer_turns() -> None:
    reset_complete_json_observer_state()
    yield
    reset_complete_json_observer_state()


def _drain(gen) -> List[Dict[str, Any]]:
    """Drain a Strands async stream into a list for easy assertions."""

    async def _run() -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        async for event in gen:
            out.append(event)
        return out

    return asyncio.run(_run())


def _cache_message(text: str, *, cache_read_input_tokens: int, cache_creation_input_tokens: int):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=11,
            output_tokens=7,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
        ),
    )


class _FakeStreamCtx:
    def __init__(self, message: Any) -> None:
        self._message = message

    def __enter__(self) -> "_FakeStreamCtx":
        return self

    def __exit__(self, *_a: Any) -> bool:
        return False

    def get_final_message(self) -> Any:
        return self._message


class _SequentialFakeMessages:
    """Returns one queued response per ``stream()`` call, in order, capturing
    each call's outgoing kwargs so the test can compare wire payloads across
    repeated calls."""

    def __init__(self, messages: List[Any]) -> None:
        self._messages = list(messages)
        self.captured_calls: List[Dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _FakeStreamCtx:
        self.captured_calls.append(kwargs)
        return _FakeStreamCtx(self._messages.pop(0))


def _make_claude_client(messages: List[Any]) -> tuple[ClaudeLLMClient, _SequentialFakeMessages]:
    fake_messages = _SequentialFakeMessages(messages)
    client = ClaudeLLMClient(model="claude-opus-4-8", api_key="sk-test")
    client._client = SimpleNamespace(messages=fake_messages)
    return client, fake_messages


def test_repeated_cache_breakpoint_prefix_yields_cache_hit_with_stable_output() -> None:
    """The same ``CacheBreakpoint``-marked prefix sent twice through
    ``LLMClientModel.stream`` (the Strands wrapper): identical output both
    times, an identical wire-level ``cache_control`` block both times, and a
    non-zero ``cache_read_tokens`` telemetry record on the second (repeat)
    call."""
    telemetry.clear_call_log()
    stable_prefix = CacheBreakpoint("stable spec excerpt shared across calls")

    client, fake_messages = _make_claude_client(
        [
            _cache_message('{"ok": 1}', cache_read_input_tokens=0, cache_creation_input_tokens=180),
            _cache_message('{"ok": 1}', cache_read_input_tokens=180, cache_creation_input_tokens=0),
        ]
    )
    model = LLMClientModel(client)

    def _one_turn() -> List[Dict[str, Any]]:
        return _drain(
            model.stream(
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
                system_prompt_content=[stable_prefix, {"text": "trailer"}],
            )
        )

    first_events = _one_turn()
    second_events = _one_turn()

    assert first_events == second_events  # no output change for identical input

    first_system = fake_messages.captured_calls[0]["system"]
    second_system = fake_messages.captured_calls[1]["system"]
    assert first_system == second_system
    assert first_system[0] == {
        "type": "text",
        "text": "stable spec excerpt shared across calls",
        "cache_control": {"type": "ephemeral"},
    }

    calls = telemetry.get_recent_calls()
    assert len(calls) >= 2
    first_call, second_call = calls[-2], calls[-1]
    assert first_call["cache_read_tokens"] == 0
    assert first_call["cache_creation_tokens"] == 180
    assert second_call["cache_read_tokens"] == 180  # the cache hit this module exists to prove
    assert second_call["cache_creation_tokens"] == 0
