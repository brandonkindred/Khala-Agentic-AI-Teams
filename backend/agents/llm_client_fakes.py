"""Shared test doubles for faking the Anthropic SDK behind a real ``ClaudeLLMClient``.

Why a separate top-level module? ``llm_service``'s own Claude-client tests
originally kept this scaffolding in ``llm_service/tests/_fakes.py``, but some
teams (e.g. ``software_engineering_team``) override pytest's rootdir via
their own ``pyproject.toml`` and construct a real, Claude-backed
``ClaudeLLMClient`` in their own e2e tests (to exercise telemetry that only a
real client populates) rather than reaching into another team's private
``tests/`` package. By shipping these doubles as an importable module on the
standard agents ``pythonpath`` -- mirroring ``job_service_client_fake.py``'s
established pattern -- any team's tests can pull them in with a single
one-liner::

    from llm_client_fakes import _make_claude_client, _text_message

Leading underscores on the symbols mark them as test-only doubles, not a
public API; the module itself has no leading underscore so it is importable
by name from any team's ``tests/`` package.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List


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


def _build_claude_client(
    messages_obj: Any, *, model: str = "claude-opus-4-8", api_key: str = "sk-test"
):
    """Build a real ``ClaudeLLMClient`` wired to a fake Anthropic SDK.

    The single place every fake-Claude-client test double constructs its
    client: ``ClaudeLLMClient`` has no public constructor arg for the
    underlying Anthropic SDK client, so callers inject one through the
    private ``_client`` seam instead.

    Preconditions:
        ``messages_obj`` exposes a ``stream(**kwargs)`` method returning a
        context manager whose ``get_final_message()`` yields the canned
        Anthropic SDK response (e.g. ``_SequentialFakeMessages`` here, or
        ``llm_service/tests/test_claude_client.py``'s ``_FakeMessages``) --
        i.e. the shape of the real SDK client's ``.messages`` attribute.

    Postconditions:
        Returns a genuine ``ClaudeLLMClient`` whose private Anthropic SDK
        handle is an object exposing ``messages=messages_obj``, so every
        real code path (``chat``, ``complete_json``, wire rendering,
        telemetry) runs unmodified against ``messages_obj``'s scripted
        replies.
    """
    from llm_service.clients.claude import ClaudeLLMClient

    client = ClaudeLLMClient(model=model, api_key=api_key)
    client._client = SimpleNamespace(messages=messages_obj)
    return client


def _make_claude_client(messages: List[Any]):
    """Build a real ``ClaudeLLMClient`` wired to a scripted, sequential fake
    Anthropic SDK -- one queued response per call, in order.

    Postconditions:
        Returns ``(client, fake_messages)`` via :func:`_build_claude_client`
        (``client``) and ``_SequentialFakeMessages(messages)``
        (``fake_messages``), so every real code path runs unmodified against
        scripted replies and ``fake_messages.captured_calls`` records every
        call's outgoing kwargs, in order.
    """
    fake_messages = _SequentialFakeMessages(messages)
    client = _build_claude_client(fake_messages)
    return client, fake_messages


__all__ = [
    "_FakeStreamCtx",
    "_text_message",
    "_SequentialFakeMessages",
    "_build_claude_client",
    "_make_claude_client",
]
