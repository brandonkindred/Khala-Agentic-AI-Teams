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

from typing import Any, Dict, List

import pytest

from llm_client_fakes import _make_claude_client, _text_message
from llm_service import telemetry
from llm_service.cache_breakpoint import CacheBreakpoint
from llm_service.interface import reset_complete_json_observer_state
from llm_service.strands_adapter import LLMClientModel
from llm_service.tests._fakes import _drain


@pytest.fixture(autouse=True)
def _reset_observer_turns() -> None:
    reset_complete_json_observer_state()
    yield
    reset_complete_json_observer_state()


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
            _text_message('{"ok": 1}', cache_read_input_tokens=0, cache_creation_input_tokens=180),
            _text_message('{"ok": 1}', cache_read_input_tokens=180, cache_creation_input_tokens=0),
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

    assert len(fake_messages.captured_calls) == 2
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
