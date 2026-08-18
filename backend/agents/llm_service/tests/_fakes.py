"""Shared test doubles for faking the Anthropic SDK and draining Strands
streams — used across ``test_claude_client.py``, ``test_strands_adapter.py``,
and ``test_cache_breakpoint_e2e.py`` so each doesn't maintain its own copy.

Leading underscore keeps this out of pytest's test collection.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

__all__ = [
    "_FakeStreamCtx",
    "_text_message",
    "_drain",
    "_SequentialFakeMessages",
    "_make_claude_client",
]


class _FakeStreamCtx:
    def __init__(self, message=None, exc=None):
        self._message = message
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get_final_message(self):
        if self._exc is not None:
            raise self._exc
        return self._message


def _text_message(
    text,
    *,
    stop_reason="end_turn",
    input_tokens=11,
    output_tokens=7,
    cache_read_input_tokens=None,
    cache_creation_input_tokens=None,
):
    usage_kwargs = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if cache_read_input_tokens is not None:
        usage_kwargs["cache_read_input_tokens"] = cache_read_input_tokens
    if cache_creation_input_tokens is not None:
        usage_kwargs["cache_creation_input_tokens"] = cache_creation_input_tokens
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(**usage_kwargs),
    )


def _drain(gen) -> List[Dict[str, Any]]:
    """Drain a Strands async stream into a list for easy assertions."""

    async def _run() -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        async for event in gen:
            out.append(event)
        return out

    return asyncio.run(_run())


class _SequentialFakeMessages:
    """Returns one queued response per ``stream()`` call, in order, capturing
    each call's outgoing kwargs so the test can compare wire payloads across
    repeated calls."""

    def __init__(self, messages: List[Any]) -> None:
        self._messages = list(messages)
        self.captured_calls: List[Dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _FakeStreamCtx:
        self.captured_calls.append(kwargs)
        return _FakeStreamCtx(message=self._messages.pop(0))


def _make_claude_client(messages: List[Any]):
    """Build a real ``ClaudeLLMClient`` wired to a scripted fake Anthropic SDK.

    Postconditions:
        Returns ``(client, fake_messages)``. ``client`` is a genuine
        ``ClaudeLLMClient`` whose private Anthropic SDK handle has been
        swapped for ``fake_messages`` (same private-seam injection as
        ``test_claude_client.py``'s own client-construction helper —
        ``ClaudeLLMClient`` has no public constructor arg for the underlying
        SDK client), so every real code path (``chat``, ``complete_json``,
        wire rendering, telemetry) runs unmodified against scripted replies.
    """
    from llm_service.clients.claude import ClaudeLLMClient

    fake_messages = _SequentialFakeMessages(messages)
    client = ClaudeLLMClient(model="claude-opus-4-8", api_key="sk-test")
    client._client = SimpleNamespace(messages=fake_messages)
    return client, fake_messages
