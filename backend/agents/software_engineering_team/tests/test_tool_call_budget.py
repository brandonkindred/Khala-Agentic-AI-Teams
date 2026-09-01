"""Tests for the hard per-run agent tool-call cap.

The regression these guard is a production hang: a code-review agent whose
model answers every turn with the same tool call loops forever, because the
tools' own budget only *asks* the model to stop and the Strands event loop
has no iteration limit of its own.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from software_engineering_team.code_review_agent.tool_call_budget import (
    DEFAULT_AGENT_TOOL_CALL_CAP,
    ToolCallBudgetModel,
    resolve_agent_tool_call_cap,
)


def _tool_use_events(tool_use_id: str, name: str, tool_input: Dict[str, Any]) -> List[Dict]:
    return [
        {"messageStart": {"role": "assistant"}},
        {
            "contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {"toolUse": {"toolUseId": tool_use_id, "name": name}},
            },
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"toolUse": {"input": json.dumps(tool_input)}},
            },
        },
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {
            "messageStop": {"stopReason": "tool_use"},
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            },
        },
    ]


def _text_events(text: str) -> List[Dict]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {
            "messageStop": {"stopReason": "end_turn"},
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            },
        },
    ]


class _AlwaysToolCallsModel:
    """A model that never stops asking for the same tool — the production bug."""

    def __init__(self, *, response_format: str = "text", final_text: str | None = None) -> None:
        self.config: Dict[str, Any] = {"response_format": response_format}
        self.calls: List[Dict[str, Any]] = []
        self._final_text = final_text

    # -- strands Model surface -------------------------------------------

    # Strands reads this off every model it is handed directly (the formatting
    # pass gets this double unwrapped).
    stateful = False

    def get_config(self) -> Dict[str, Any]:
        return self.config

    def update_config(self, **overrides: Any) -> None:
        self.config.update(overrides)

    def clone(self, **overrides: Any) -> "_AlwaysToolCallsModel":
        cloned = _AlwaysToolCallsModel(final_text=self._final_text)
        cloned.config = {**self.config, **overrides}
        cloned.calls = self.calls
        return cloned

    async def stream(
        self,
        messages: Any,
        tool_specs: Optional[List[Any]] = None,
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ):
        self.calls.append(
            {"messages": messages, "tool_specs": tool_specs, "system_prompt": system_prompt}
        )
        if self.config.get("response_format") == "json":
            for event in _text_events(json.dumps({"verdict": "kept"})):
                yield event
            return
        if self._final_text is not None and not tool_specs:
            for event in _text_events(self._final_text):
                yield event
            return
        for event in _tool_use_events("call_0", "read_file", {"path": "app/main.py"}):
            yield event


def _drain(model: Any, **kwargs: Any) -> List[Dict]:
    import asyncio

    async def _run() -> List[Dict]:
        return [event async for event in model.stream(**kwargs)]

    return asyncio.run(_run())


def test_resolve_cap_defaults_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODE_REVIEW_AGENT_TOOL_CALL_CAP", raising=False)
    assert resolve_agent_tool_call_cap() == DEFAULT_AGENT_TOOL_CALL_CAP
    monkeypatch.setenv("CODE_REVIEW_AGENT_TOOL_CALL_CAP", "3")
    assert resolve_agent_tool_call_cap() == 3
    monkeypatch.setenv("CODE_REVIEW_AGENT_TOOL_CALL_CAP", "not-a-number")
    assert resolve_agent_tool_call_cap() == DEFAULT_AGENT_TOOL_CALL_CAP
    monkeypatch.setenv("CODE_REVIEW_AGENT_TOOL_CALL_CAP", "0")
    assert resolve_agent_tool_call_cap() == 1


def test_rejects_invalid_construction() -> None:
    with pytest.raises(ValueError):
        ToolCallBudgetModel(None, 5)
    with pytest.raises(ValueError):
        ToolCallBudgetModel(_AlwaysToolCallsModel(), 0)


def test_passthrough_below_cap_is_unchanged() -> None:
    inner = _AlwaysToolCallsModel()
    model = ToolCallBudgetModel(inner, 2)

    events = _drain(model, messages=[], tool_specs=[{"name": "read_file"}])

    assert events == _tool_use_events("call_0", "read_file", {"path": "app/main.py"})
    assert model.tool_calls_used == 1
    assert inner.calls[0]["tool_specs"] == [{"name": "read_file"}]


def test_at_cap_withdraws_tools_and_forces_end_turn() -> None:
    inner = _AlwaysToolCallsModel()
    model = ToolCallBudgetModel(inner, 1)

    _drain(model, messages=[], tool_specs=[{"name": "read_file"}])
    assert model.tool_calls_used == 1

    messages = [{"role": "user", "content": [{"text": "Review this"}]}]
    events = _drain(model, messages=messages, tool_specs=[{"name": "read_file"}])

    # Tools withdrawn, directive appended, caller's list untouched.
    assert inner.calls[1]["tool_specs"] is None
    assert len(messages) == 1
    sent = inner.calls[1]["messages"]
    assert len(sent) == 2
    assert "budget" in sent[-1]["content"][0]["text"]

    # The model kept asking for a tool; nothing tool-shaped survives, and the
    # turn ends the loop.
    assert not any("toolUse" in json.dumps(event) for event in events)
    stops = [event["messageStop"] for event in events if "messageStop" in event]
    assert stops == [{"stopReason": "end_turn"}]
    # Sibling metadata on the stop event is preserved.
    stop_event = next(event for event in events if "messageStop" in event)
    assert stop_event["metadata"]["usage"]["totalTokens"] == 2
    # No text came back from the model, so an honest placeholder is synthesized.
    assert any("No conclusion was reached" in json.dumps(event) for event in events)


def test_at_cap_keeps_real_final_text() -> None:
    inner = _AlwaysToolCallsModel(final_text="Finding 0 is real; I ran out of budget.")
    model = ToolCallBudgetModel(inner, 1)

    _drain(model, messages=[], tool_specs=[{"name": "read_file"}])
    events = _drain(model, messages=[], tool_specs=[{"name": "read_file"}])

    texts = [
        event["contentBlockDelta"]["delta"]["text"]
        for event in events
        if "contentBlockDelta" in event and "text" in event["contentBlockDelta"]["delta"]
    ]
    assert texts == ["Finding 0 is real; I ran out of budget."]
    assert "No conclusion was reached" not in json.dumps(events)


def test_delegates_unknown_attributes_and_config() -> None:
    inner = _AlwaysToolCallsModel()
    model = ToolCallBudgetModel(inner, 5)

    assert model.get_config() == inner.config
    model.update_config(temperature=0.5)
    assert inner.config["temperature"] == 0.5
    assert isinstance(model.clone(response_format="json"), _AlwaysToolCallsModel)
    assert model.inner is inner
    assert model.max_tool_calls == 5
    with pytest.raises(AttributeError):
        _ = model.no_such_attribute


def test_config_helpers_tolerate_a_bare_model() -> None:
    class _Bare:
        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            for event in _text_events("done"):
                yield event

    model = ToolCallBudgetModel(_Bare(), 1)
    assert model.get_config() == {}
    model.update_config(temperature=0.1)  # no-op, must not raise


def test_run_agent_via_reasoning_terminates_against_a_looping_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: a model that always answers with a tool call must not hang."""
    from strands import tool

    from software_engineering_team.code_review_agent.via_reasoning import (
        run_agent_via_reasoning,
    )

    monkeypatch.setenv("CODE_REVIEW_AGENT_TOOL_CALL_CAP", "3")

    calls: List[str] = []

    @tool
    def read_file(path: str) -> str:
        """Read a file.

        Args:
            path: the file path.
        """
        calls.append(path)
        return "file contents"

    model = _AlwaysToolCallsModel(final_text="Everything checks out.")

    result = run_agent_via_reasoning(
        model=model,
        reasoning_prompt="Review this",
        reasoning_system_prompt="Prose reviewer",
        formatting_instructions='Return {"verdict": str}',
        parse=lambda raw: json.loads(raw),
        tools=[read_file],
    )

    assert result == {"verdict": "kept"}
    # Exactly the cap's worth of tool calls, then one final tool-free turn.
    assert len(calls) == 3
