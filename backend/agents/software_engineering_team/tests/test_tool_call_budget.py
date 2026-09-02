"""Tests for the hard per-run agent tool-call cap.

The regression these guard is a production hang: a code-review agent whose
model answers every turn with the same tool call loops forever, because the
tools' own budget only *asks* the model to stop and the Strands event loop
has no iteration limit of its own.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from software_engineering_team.code_review_agent.tool_call_budget import (
    DEFAULT_AGENT_TOOL_CALL_CAP,
    ToolCallBudgetModel,
    _has_text_delta,
    _is_tool_use_delta,
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

    def __init__(self, *, response_format: str = "text", final_text: Optional[str] = None) -> None:
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
        if self._final_text is not None and _sees_budget_directive(system_prompt):
            for event in _text_events(self._final_text):
                yield event
            return
        for event in _tool_use_events("call_0", "read_file", {"path": "app/main.py"}):
            yield event


def _sees_budget_directive(system_prompt: Any) -> bool:
    """Whether the wrapper's "budget exhausted" directive is in `system_prompt`.

    This is how a real model tells the final turn apart: the tools stay
    attached and the messages are untouched, so the directive on the system
    prompt is the only signal.
    """
    return "budget for this task is exhausted" in json.dumps(system_prompt, default=str)


def _spend_budget(model: ToolCallBudgetModel, n: int) -> None:
    """Put ``model`` in the state a run that already emitted ``n`` tool calls
    would be in, without driving a whole turn to get there.

    The private write is deliberate and centralized: these tests need the at-cap
    regime as a *precondition*, not as the thing under test, and draining a setup
    turn first would make each one exercise two code paths.

    The readback is what makes centralizing it worth anything. A bare assignment
    to a renamed attribute does not fail — Python just creates a new instance
    attribute while the real counter stays at 0 — so the write would silently
    become dead and four tests would fail downstream with "toolUse leaked
    through", naming nothing. Asserting through the public accessor turns that
    into a failure inside this helper, with this name in the traceback.
    """
    model._tool_calls_used = n
    assert model.tool_calls_used == n, (
        "_tool_calls_used no longer backs tool_calls_used; update this helper"
    )


def _drain(model: Any, **kwargs: Any) -> List[Dict]:
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


def test_rejects_a_non_int_cap_instead_of_truncating_it() -> None:
    """A coerced cap would not be the one the caller asked for.

    `int(2.5)` enforces 2 while the caller believes it asked for 2.5, so the
    contract would stop describing what actually runs. `bool` is an `int`
    subclass, and `True` as a cap is a caller bug rather than a budget of one.
    """
    for bad in (2.5, "3", True, None):
        with pytest.raises(ValueError, match="must be an int"):
            ToolCallBudgetModel(_AlwaysToolCallsModel(), bad)

    assert ToolCallBudgetModel(_AlwaysToolCallsModel(), 3).max_tool_calls == 3


def test_passthrough_below_cap_is_unchanged() -> None:
    inner = _AlwaysToolCallsModel()
    model = ToolCallBudgetModel(inner, 2)

    events = _drain(model, messages=[], tool_specs=[{"name": "read_file"}])

    assert events == _tool_use_events("call_0", "read_file", {"path": "app/main.py"})
    assert model.tool_calls_used == 1
    assert inner.calls[0]["tool_specs"] == [{"name": "read_file"}]


def test_at_cap_keeps_tools_and_forces_end_turn() -> None:
    inner = _AlwaysToolCallsModel()
    model = ToolCallBudgetModel(inner, 1)

    _drain(model, messages=[], tool_specs=[{"name": "read_file"}])
    assert model.tool_calls_used == 1

    messages = [{"role": "user", "content": [{"text": "Review this"}]}]
    events = _drain(model, messages=messages, tool_specs=[{"name": "read_file"}])

    # Tools stay attached and the messages are forwarded untouched — both
    # would otherwise make the request invalid under Anthropic. The directive
    # rides on the system prompt instead.
    assert inner.calls[1]["tool_specs"] == [{"name": "read_file"}]
    assert inner.calls[1]["messages"] == messages
    assert messages == [{"role": "user", "content": [{"text": "Review this"}]}]
    assert "budget for this task is exhausted" in inner.calls[1]["system_prompt"]

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
        async def stream(
            self,
            messages: Any,
            tool_specs: Optional[List[Any]] = None,
            system_prompt: Optional[str] = None,
            **kwargs: Any,
        ):
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


class _ParallelBatchModel:
    """Emits several `toolUse` blocks in one assistant turn (a parallel batch)."""

    stateful = False

    def __init__(self, batch_size: int) -> None:
        self._batch_size = batch_size

    def get_config(self) -> Dict[str, Any]:
        return {}

    def update_config(self, **overrides: Any) -> None:
        return None

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs: Any):
        if _sees_budget_directive(system_prompt):
            for event in _text_events("done"):
                yield event
            return
        yield {"messageStart": {"role": "assistant"}}
        for i in range(self._batch_size):
            yield {
                "contentBlockStart": {
                    "contentBlockIndex": i,
                    "start": {"toolUse": {"toolUseId": f"call_{i}", "name": "read_file"}},
                },
            }
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": i,
                    "delta": {"toolUse": {"input": json.dumps({"path": f"f{i}.py"})}},
                },
            }
            yield {"contentBlockStop": {"contentBlockIndex": i}}
        yield {"messageStop": {"stopReason": "tool_use"}}


def _tool_use_ids(events: List[Dict]) -> List[str]:
    return [
        event["contentBlockStart"]["start"]["toolUse"]["toolUseId"]
        for event in events
        if "contentBlockStart" in event and "toolUse" in event["contentBlockStart"]["start"]
    ]


def test_cap_bounds_tool_calls_within_a_single_parallel_batch() -> None:
    """A batch that crosses the cap mid-turn is truncated, not forwarded whole."""
    model = ToolCallBudgetModel(_ParallelBatchModel(5), 2)

    events = _drain(model, messages=[], tool_specs=[{"name": "read_file"}])

    # Only the calls that fit in the budget reach Strands; the rest are dropped
    # whole (start, input delta and stop), so they are never executed.
    assert _tool_use_ids(events) == ["call_0", "call_1"]
    assert model.tool_calls_used == 2
    assert sum(1 for event in events if "contentBlockStop" in event) == 2
    # Positive, not "nothing extra leaked": this is the one path where a KEPT
    # block's input deltas run through the wrapper's filter, so a defect that
    # dropped every tool-use delta would satisfy an absence check while handing
    # Strands two tool-use blocks with no input at all.
    tool_use_paths = [
        json.loads(event["contentBlockDelta"]["delta"]["toolUse"]["input"])["path"]
        for event in events
        if "toolUse" in (event.get("contentBlockDelta", {}).get("delta") or {})
    ]
    assert tool_use_paths == ["f0.py", "f1.py"]
    # The turn's own stop reason is untouched here — the next turn is the one
    # that ends the loop, with the tools still attached and a directive on the
    # system prompt (withdrawing the specs would break the Anthropic request).
    assert events[-1] == {"messageStop": {"stopReason": "tool_use"}}


def test_cap_of_one_executes_exactly_one_tool_call() -> None:
    model = ToolCallBudgetModel(_ParallelBatchModel(4), 1)

    events = _drain(model, messages=[], tool_specs=[{"name": "read_file"}])

    assert _tool_use_ids(events) == ["call_0"]
    assert model.tool_calls_used == 1


class _TruncatedFinalTurnModel:
    """Asks for a tool, then hits the token limit on the tool-free final turn."""

    stateful = False

    def get_config(self) -> Dict[str, Any]:
        return {}

    def update_config(self, **overrides: Any) -> None:
        return None

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs: Any):
        if not _sees_budget_directive(system_prompt):
            for event in _tool_use_events("call_0", "read_file", {"path": "app/main.py"}):
                yield event
            return
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
        yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Finding 0 is par"}}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "max_tokens"}}


def test_final_turn_preserves_a_terminal_stop_reason() -> None:
    """`max_tokens` must reach Strands so its truncation handling still fires."""
    model = ToolCallBudgetModel(_TruncatedFinalTurnModel(), 1)

    _drain(model, messages=[], tool_specs=[{"name": "read_file"}])
    events = _drain(model, messages=[], tool_specs=[{"name": "read_file"}])

    stops = [event["messageStop"] for event in events if "messageStop" in event]
    assert stops == [{"stopReason": "max_tokens"}]


class _DeltaAnnouncedToolUseModel:
    """Announces its tool use in the delta only — a shape Strands accepts.

    `streaming.handle_content_block_delta` fills toolUseId/name from the delta
    when `contentBlockStart` carried none, and `handle_message_stop` re-derives
    `stopReason="tool_use"` from any surviving tool-use block — so a block in
    this shape that slipped past the cap would restore the infinite loop.
    """

    stateful = False

    def get_config(self) -> Dict[str, Any]:
        return {}

    def update_config(self, **overrides: Any) -> None:
        return None

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs: Any):
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
        yield {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {
                    "toolUse": {
                        "toolUseId": "call_0",
                        "name": "read_file",
                        "input": json.dumps({"path": "app/main.py"}),
                    }
                },
            },
        }
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "tool_use"}}


def test_delta_announced_tool_use_counts_against_the_cap() -> None:
    model = ToolCallBudgetModel(_DeltaAnnouncedToolUseModel(), 1)

    first = _drain(model, messages=[], tool_specs=[{"name": "read_file"}])
    assert model.tool_calls_used == 1
    assert any(_is_tool_use_delta(event) for event in first)

    # Budget spent: the next turn's delta-announced tool use is dropped and the
    # loop is ended, rather than sailing past the cap.
    events = _drain(model, messages=[], tool_specs=[{"name": "read_file"}])
    assert model.tool_calls_used == 1
    assert not any(_is_tool_use_delta(event) for event in events)
    assert not any("toolUse" in json.dumps(event) for event in events)
    assert [event["messageStop"] for event in events if "messageStop" in event] == [
        {"stopReason": "end_turn"}
    ]


def test_malformed_delta_events_do_not_raise() -> None:
    """The helpers promise never to raise on a malformed event."""
    for bogus in ({"contentBlockDelta": "nope"}, {"contentBlockDelta": {"delta": 7}}, "junk", None):
        assert _has_text_delta(bogus) is False
        assert _is_tool_use_delta(bogus) is False


def test_strands_model_defaults_are_bound_to_the_wrapper() -> None:
    """A `Model` method reached through the fallback must bind `self`."""

    class _Bare:
        async def stream(
            self,
            messages: Any,
            tool_specs: Optional[List[Any]] = None,
            system_prompt: Optional[str] = None,
            **kwargs: Any,
        ):
            for event in _text_events("done"):
                yield event

    model = ToolCallBudgetModel(_Bare(), 3)

    # `context_window_limit` is a Model property: evaluated against the wrapper.
    assert model.context_window_limit is None
    # `count_tokens` is a Model method: the wrapper must be bound as `self`, so
    # the caller's first argument stays its first argument.
    import asyncio

    tokens = asyncio.run(model.count_tokens([{"role": "user", "content": [{"text": "hi"}]}]))
    assert isinstance(tokens, int)


def test_default_hard_cap_exceeds_advisory_tool_budget() -> None:
    """The hard cap must sit above the tools' own advisory budget.

    The two live in different modules and only a comment used to connect
    them: if the advisory budget ever rose above the default cap, a
    cooperating model would be cut off before it ever saw the "stop and
    answer now" nudge, and nothing would fail.
    """
    from software_engineering_team.code_review_agent import false_positive_filter

    assert DEFAULT_AGENT_TOOL_CALL_CAP > false_positive_filter._MAX_TOTAL_TOOL_CALLS


class _TextAndToolUseInOneBlockModel:
    """Puts real text and a tool use in a single content block."""

    stateful = False

    def get_config(self) -> Dict[str, Any]:
        return {}

    def update_config(self, **overrides: Any) -> None:
        return None

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs: Any):
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
        yield {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"text": "Verdict: finding 0 is real."},
            },
        }
        yield {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"toolUse": {"toolUseId": "c1", "name": "read_file", "input": "{}"}},
            },
        }
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "tool_use"}}


def test_dropped_tool_use_keeps_its_block_stop_when_text_shares_the_block() -> None:
    """A block carrying both text and an over-cap tool use must still close.

    Strands commits a block's accumulated text on `contentBlockStop`, so
    swallowing that stop would discard the model's real answer and leave an
    empty assistant message — which `_require_reasoning_prose` then turns into
    a semantic-exhaustion error instead of a graceful degrade.
    """
    model = ToolCallBudgetModel(_TextAndToolUseInOneBlockModel(), 1)
    _spend_budget(model, 1)  # budget already spent: this is the final turn

    events = _drain(model, messages=[], tool_specs=[{"name": "read_file"}])

    assert not any("toolUse" in json.dumps(event) for event in events)
    assert any(_has_text_delta(event) for event in events)
    # The block closes, so Strands commits the text.
    assert any("contentBlockStop" in event for event in events)
    # Real text came back, so no placeholder is synthesized.
    assert "No conclusion was reached" not in json.dumps(events)
    assert [event["messageStop"] for event in events if "messageStop" in event] == [
        {"stopReason": "end_turn"}
    ]


def test_drop_state_is_scoped_to_the_block_that_opened_it() -> None:
    """Another block's stop must neither end the drop nor be swallowed by it."""

    class _InterleavedModel:
        stateful = False

        def get_config(self) -> Dict[str, Any]:
            return {}

        def update_config(self, **overrides: Any) -> None:
            return None

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs: Any):
            yield {"messageStart": {"role": "assistant"}}
            # Over-cap tool use opens block 0 and is dropped...
            yield {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": "c1", "name": "read_file"}},
                },
            }
            # ...while an unrelated text block opens, runs and closes.
            yield {"contentBlockStart": {"contentBlockIndex": 1, "start": {}}}
            yield {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"text": "hello"}}}
            yield {"contentBlockStop": {"contentBlockIndex": 1}}
            # Block 0's own input delta and stop still belong to the dropped use.
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"toolUse": {"input": "{}"}},
                },
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "tool_use"}}

    model = ToolCallBudgetModel(_InterleavedModel(), 1)
    _spend_budget(model, 1)
    events = _drain(model, messages=[], tool_specs=[{"name": "read_file"}])

    # The text block survives intact, including its own stop.
    assert any(_has_text_delta(event) for event in events)
    stops = [event["contentBlockStop"] for event in events if "contentBlockStop" in event]
    assert stops == [{"contentBlockIndex": 1}]
    # Nothing tool-shaped leaks through, in either shape.
    assert not any("toolUse" in json.dumps(event) for event in events)


def test_final_turn_wire_shape_has_no_consecutive_user_turns() -> None:
    """The final turn must translate to a valid Anthropic message list.

    The turn that spends the budget ends with the tool results, which Strands
    carries as a user message. Any directive packed into the messages — its own
    message or an extra block on that one — comes out the other side of the two
    translators as `user(tool_result)` followed by `user(directive)`, because
    `_strands_messages_to_openai` splits a toolResult-bearing message into its
    own `role="tool"` message. Keeping the directive on the system prompt is
    what avoids that, and this pins it end to end rather than by inspection.
    """
    from llm_service.clients.claude import _to_anthropic_messages
    from llm_service.strands_adapter import _strands_messages_to_openai
    from software_engineering_team.code_review_agent.tool_call_budget import (
        _system_with_directive,
    )

    messages = [
        {"role": "user", "content": [{"text": "Review this"}]},
        {
            "role": "assistant",
            "content": [
                {"toolUse": {"toolUseId": "t1", "name": "read_file", "input": {"path": "a.py"}}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "t1",
                        "status": "success",
                        "content": [{"text": "file body"}],
                    }
                }
            ],
        },
    ]

    # The wrapper forwards these messages verbatim; only the system prompt grows.
    system, anthropic_messages = _to_anthropic_messages(_strands_messages_to_openai(messages))
    # The translator must not be the thing that carries the directive -- the wrapper
    # puts it on the system prompt (asserted separately below), and a translator that
    # started synthesizing one would make that assertion pass for the wrong reason.
    assert "budget for this task is exhausted" not in (system or "")
    roles = [message["role"] for message in anthropic_messages]
    assert roles == ["user", "assistant", "user"]
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)), (
        "consecutive same-role turns are not a valid Anthropic message list"
    )

    directive_system = _system_with_directive("You are a reviewer.")
    assert directive_system.startswith("You are a reviewer.")
    assert "budget for this task is exhausted" in directive_system


def test_directive_keeps_system_prompt_and_content_in_step() -> None:
    """Both system views must grow together, or the persona is sent twice.

    The adapter drops the string form when it is exactly the "\\n"-join of the
    content blocks; extending only one view would break that equality.
    """
    from llm_service.strands_adapter import _system_prompt_is_redundant_with_content
    from software_engineering_team.code_review_agent.tool_call_budget import (
        _kwargs_with_directive,
        _system_with_directive,
    )

    blocks = [{"text": "You are a reviewer."}, {"text": "Follow the contract."}]
    system_prompt = "\n".join(block["text"] for block in blocks)
    kwargs = {"system_prompt_content": blocks}

    new_system = _system_with_directive(system_prompt)
    new_kwargs = _kwargs_with_directive(kwargs)
    new_blocks = new_kwargs["system_prompt_content"]

    assert _system_prompt_is_redundant_with_content(
        new_system, [block["text"] for block in new_blocks]
    )
    # The caller's list and blocks are untouched.
    assert kwargs["system_prompt_content"] == blocks
    assert len(blocks) == 2
    # With no content blocks, the kwargs come back unchanged.
    assert _kwargs_with_directive({"invocation_state": {}}) == {"invocation_state": {}}


def test_directive_preserves_a_real_persona_verbatim() -> None:
    """The equality is exact, so surrounding whitespace must survive.

    Every review persona in this package ends with a newline. Stripping it
    (or any other normalization) breaks the adapter's exact-equality check
    and sends the whole persona twice on the final turn — the case a
    whitespace-clean fixture cannot catch.
    """
    from llm_service.strands_adapter import _system_prompt_is_redundant_with_content
    from software_engineering_team.code_review_agent.scope_filter import (
        SCOPE_VERIFY_REASONING_SYSTEM_PROMPT as persona,
    )
    from software_engineering_team.code_review_agent.tool_call_budget import (
        _kwargs_with_directive,
        _system_with_directive,
    )

    assert persona != persona.strip(), "fixture must carry the real surrounding whitespace"

    # The shape Strands' split_system_prompt hands the model.
    new_system = _system_with_directive(persona)
    new_blocks = _kwargs_with_directive({"system_prompt_content": [{"text": persona}]})[
        "system_prompt_content"
    ]

    assert _system_prompt_is_redundant_with_content(
        new_system, [block["text"] for block in new_blocks]
    )
    assert new_system.startswith(persona)
    assert new_system.count("budget for this task is exhausted") == 1


def test_an_unrelated_blocks_stop_does_not_discard_another_blocks_text() -> None:
    """Drop bookkeeping is per block, so one block's stop cannot speak for another.

    Block 0 carries the model's real text and then an over-cap tool use; an
    unrelated block 1 opens and closes in between. With stream-wide flags,
    block 1's stop cleared the record that block 0 had already forwarded text,
    so block 0's own stop was swallowed as if the block were empty — Strands
    never commits the text, `_final_turn` has already counted it as emitted so
    no placeholder is synthesized, and the run ends in an empty assistant
    message (`LLMSemanticExhaustionError` downstream).
    """

    class _InterleavedTextAndToolUse:
        stateful = False

        def get_config(self) -> Dict[str, Any]:
            return {}

        def update_config(self, **overrides: Any) -> None:
            return None

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs: Any):
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"text": "Verdict: finding 0 is real"},
                },
            }
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"toolUse": {"toolUseId": "c1", "name": "read_file", "input": "{}"}},
                },
            }
            # An unrelated block opens and closes while block 0 is still open.
            yield {"contentBlockStart": {"contentBlockIndex": 1, "start": {}}}
            yield {"contentBlockStop": {"contentBlockIndex": 1}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "tool_use"}}

    model = ToolCallBudgetModel(_InterleavedTextAndToolUse(), 1)
    _spend_budget(model, 1)  # budget spent: this is the final turn

    events = _drain(model, messages=[], tool_specs=[{"name": "read_file"}])

    assert not any("toolUse" in json.dumps(event) for event in events)
    assert any(_has_text_delta(event) for event in events)
    stops = [event["contentBlockStop"] for event in events if "contentBlockStop" in event]
    # Block 0's stop survives (its text must be committed); block 1's too.
    assert {stop["contentBlockIndex"] for stop in stops} == {0, 1}
    assert "No conclusion was reached" not in json.dumps(events)


def test_a_new_block_can_announce_a_tool_use_without_an_intervening_stop() -> None:
    """A missing `contentBlockStop` must not let the next tool use past the cap.

    `counted` used to be cleared only on a stop, so a stream that opened a new
    block without closing the previous one left the flag set — and a
    delta-announced tool use in that new block was then neither counted nor
    dropped. It reached Strands, which re-derives `stopReason="tool_use"` from
    the surviving block, and the run recursed past the cap.
    """

    class _NoStopBetweenBlocks:
        stateful = False

        def get_config(self) -> Dict[str, Any]:
            return {}

        def update_config(self, **overrides: Any) -> None:
            return None

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs: Any):
            yield {"messageStart": {"role": "assistant"}}
            yield {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": "c0", "name": "read_file"}},
                },
            }
            # No contentBlockStop for block 0.
            yield {"contentBlockStart": {"contentBlockIndex": 1, "start": {}}}
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": 1,
                    "delta": {"toolUse": {"toolUseId": "c1", "name": "read_file", "input": "{}"}},
                },
            }
            yield {"messageStop": {"stopReason": "tool_use"}}

    model = ToolCallBudgetModel(_NoStopBetweenBlocks(), 1)

    events = _drain(model, messages=[], tool_specs=[{"name": "read_file"}])

    # Exactly the cap's worth of tool uses reaches Strands: block 0's, not block 1's.
    assert model.tool_calls_used == 1
    tool_events = [event for event in events if "toolUse" in json.dumps(event)]
    assert len(tool_events) == 1
    assert "c1" not in json.dumps(events)


def test_a_falsy_tool_use_start_is_not_a_tool_call() -> None:
    """`{"toolUse": None}` is an ordinary block to Strands, so it must be here too.

    `streaming.handle_content_block_start` reads
    `if "toolUse" in start and start["toolUse"]`. Treating mere presence as a
    tool use charges the budget for a call that never happens, and at the cap
    drops a block that may carry real assistant text.
    """
    from software_engineering_team.code_review_agent.tool_call_budget import _is_tool_use_start

    assert _is_tool_use_start(
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "t", "name": "read_file"}}}}
    )
    for falsy in (None, {}, ""):
        assert not _is_tool_use_start({"contentBlockStart": {"start": {"toolUse": falsy}}}), falsy


def test_a_forwarded_block_start_keeps_its_stop_even_when_its_tool_use_is_dropped() -> None:
    """A block whose start reached Strands must be closed by its own stop.

    Strands opens a content block on `contentBlockStart` and closes it on
    `contentBlockStop`. If the start was forwarded and the stop swallowed, the
    block stays open there and the NEXT block's content accumulates into it,
    committing under the wrong boundary. That is reachable whenever a
    delta-announced tool use is the only thing in an already-opened block.
    """

    class _PlainStartThenOverCapToolDelta:
        stateful = False

        def get_config(self) -> Dict[str, Any]:
            return {}

        def update_config(self, **overrides: Any) -> None:
            return None

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs: Any):
            yield {"messageStart": {"role": "assistant"}}
            # A plain start (no toolUse) — forwarded, so Strands opens a block.
            yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"toolUse": {"toolUseId": "c1", "name": "read_file", "input": "{}"}},
                },
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "tool_use"}}

    model = ToolCallBudgetModel(_PlainStartThenOverCapToolDelta(), 1)
    _spend_budget(model, 1)  # budget spent: the tool use is dropped

    events = _drain(model, messages=[], tool_specs=[{"name": "read_file"}])

    assert not any("toolUse" in json.dumps(event) for event in events)
    # Count only the model's own block (index 0) — the synthesized
    # "no conclusion" fallback contributes an unindexed block of its own.
    starts = [e for e in events if (e.get("contentBlockStart") or {}).get("contentBlockIndex") == 0]
    stops = [e for e in events if (e.get("contentBlockStop") or {}).get("contentBlockIndex") == 0]
    # The block Strands opened is the block Strands closes.
    assert len(starts) == len(stops) == 1


def test_getattr_propagates_an_attribute_error_from_inners_own_getter() -> None:
    """An AttributeError raised inside an attribute that ``inner`` does provide is the
    wrapped model's error, not "inner lacks this".

    Swallowing it hands back a Strands ``Model`` default computed against the
    wrapper -- a plausible-looking value standing in for a real failure (a lazy
    import, a missing config key), and the hardest kind to trace back.
    """

    class _ExplodingModel:
        @property
        def context_window_limit(self):
            raise AttributeError("inner is missing its lazily-imported config")

        async def stream(self, *_a, **_k):  # pragma: no cover - never called
            yield {}

    wrapped = ToolCallBudgetModel(_ExplodingModel(), 5)

    with pytest.raises(AttributeError, match="lazily-imported config"):
        wrapped.context_window_limit


def test_getattr_still_falls_back_for_an_attribute_inner_genuinely_lacks() -> None:
    """The guard above must not close the fallback a bare test double relies on:
    an attribute neither object defines still resolves to Model's default."""

    class _BareModel:
        async def stream(self, *_a, **_k):  # pragma: no cover - never called
            yield {}

    wrapped = ToolCallBudgetModel(_BareModel(), 5)

    # ``Model.stateful`` is read off every model by Strands; inner does not
    # define it, so the Model default answers.
    assert wrapped.stateful is False

    with pytest.raises(AttributeError):
        wrapped.no_such_attribute_anywhere
