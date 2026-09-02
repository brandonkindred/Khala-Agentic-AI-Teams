"""Unit tests for ``shared.graph.invocation``.

Every helper here is duck-typed over the shape of a Strands
``MultiAgentResult``, so these tests drive lightweight stand-ins rather than
building a real ``Graph`` and invoking a model. That keeps the async-to-sync
bridge and the JSON extraction fallbacks testable without an LLM.
"""

from __future__ import annotations

import asyncio
import math
import threading
from typing import Any

import pytest
from pydantic import BaseModel

from shared.graph.invocation import (
    _extract_text_from_message,
    _parse_json_model,
    extract_node_output,
    extract_node_text,
    invoke_graph_sync,
)


class _Model(BaseModel):
    name: str = "default"
    score: float = 0.0


class _FakeGraph:
    """Minimal stand-in for ``strands.multiagent.graph.Graph``.

    Records the thread it was awaited on so the running-loop branch of
    :func:`invoke_graph_sync` can be told apart from the direct one.
    """

    def __init__(self, result: Any = "graph-result") -> None:
        self._result = result
        self.tasks: list[str] = []
        self.thread_names: list[str] = []

    async def invoke_async(self, task: str) -> Any:
        self.tasks.append(task)
        self.thread_names.append(threading.current_thread().name)
        return self._result


class _FakeAgentResult:
    def __init__(self, message: Any) -> None:
        self.message = message


class _FakeNodeResult:
    """Node entry exposing the ``result`` attribute and agent-results accessor."""

    def __init__(self, agent_results: list[Any]) -> None:
        self.result = object()
        self._agent_results = agent_results

    def get_agent_results(self) -> list[Any]:
        return self._agent_results


class _FakeMultiAgentResult:
    def __init__(self, nodes: dict[str, Any]) -> None:
        self.result = nodes


def _result_with_text(node_id: str, text: str) -> _FakeMultiAgentResult:
    message = {"content": [{"text": text}]}
    return _FakeMultiAgentResult({node_id: _FakeNodeResult([_FakeAgentResult(message)])})


# ---------------------------------------------------------------------------
# invoke_graph_sync
# ---------------------------------------------------------------------------


def test_invoke_graph_sync_runs_directly_without_a_running_loop() -> None:
    graph = _FakeGraph()

    assert invoke_graph_sync(graph, "do the thing") == "graph-result"
    assert graph.tasks == ["do the thing"]
    assert graph.thread_names == [threading.current_thread().name]


def test_invoke_graph_sync_offloads_when_a_loop_is_already_running() -> None:
    """Inside a running loop the call must not raise ``This event loop is already running``."""
    graph = _FakeGraph()

    async def driver() -> Any:
        return invoke_graph_sync(graph, "nested")

    assert asyncio.run(driver()) == "graph-result"
    assert graph.tasks == ["nested"]
    # The coroutine ran on a worker thread, not the thread owning the outer loop.
    assert graph.thread_names != [threading.current_thread().name]


def test_invoke_graph_sync_propagates_graph_failures() -> None:
    class _Boom:
        async def invoke_async(self, _task: str) -> Any:
            raise RuntimeError("graph exploded")

    with pytest.raises(RuntimeError, match="graph exploded"):
        invoke_graph_sync(_Boom(), "task")


# ---------------------------------------------------------------------------
# extract_node_output
# ---------------------------------------------------------------------------


def test_extract_node_output_parses_json_from_the_last_message() -> None:
    result = _result_with_text("analyst", 'Here you go: {"name": "alpha", "score": 3.5} — done.')

    parsed = extract_node_output(result, "analyst", _Model)

    assert parsed == _Model(name="alpha", score=3.5)


def test_extract_node_output_uses_the_last_agent_result() -> None:
    node = _FakeNodeResult(
        [
            _FakeAgentResult({"content": [{"text": '{"name": "stale"}'}]}),
            _FakeAgentResult({"content": [{"text": '{"name": "fresh"}'}]}),
        ]
    )

    parsed = extract_node_output(_FakeMultiAgentResult({"n": node}), "n", _Model)

    assert parsed.name == "fresh"


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(_FakeMultiAgentResult({}), id="unknown-node"),
        pytest.param(_FakeMultiAgentResult({"n": _FakeNodeResult([])}), id="no-agent-results"),
        pytest.param(
            _FakeMultiAgentResult({"n": _FakeNodeResult([_FakeAgentResult(None)])}),
            id="empty-message",
        ),
        pytest.param(_result_with_text("n", ""), id="empty-text"),
        pytest.param(_result_with_text("n", "prose with no json at all"), id="no-json"),
        pytest.param(_result_with_text("n", '{"score": "not-a-number"}'), id="unparseable-json"),
        pytest.param(object(), id="not-a-multiagent-result"),
    ],
)
def test_extract_node_output_falls_back_to_a_default_instance(result: Any) -> None:
    assert extract_node_output(result, "n", _Model) == _Model()


# ---------------------------------------------------------------------------
# extract_node_text
# ---------------------------------------------------------------------------


def test_extract_node_text_returns_concatenated_text() -> None:
    node = _FakeNodeResult([_FakeAgentResult({"content": [{"text": "first "}, {"text": "second"}]})])

    assert extract_node_text(_FakeMultiAgentResult({"n": node}), "n") == "first second"


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(_FakeMultiAgentResult({}), id="unknown-node"),
        pytest.param(_FakeMultiAgentResult({"n": _FakeNodeResult([])}), id="no-agent-results"),
        pytest.param(
            _FakeMultiAgentResult({"n": _FakeNodeResult([_FakeAgentResult(None)])}),
            id="empty-message",
        ),
        pytest.param(object(), id="not-a-multiagent-result"),
    ],
)
def test_extract_node_text_returns_empty_string_on_any_miss(result: Any) -> None:
    assert extract_node_text(result, "n") == ""


def test_extract_node_text_swallows_lookup_failures() -> None:
    """A malformed result must degrade to "" rather than break the caller."""

    class _ExplodingNodes:
        def get(self, _node_id: str) -> Any:
            raise RuntimeError("node registry corrupt")

    assert extract_node_text(_FakeMultiAgentResult(_ExplodingNodes()), "n") == ""


# ---------------------------------------------------------------------------
# _extract_text_from_message
# ---------------------------------------------------------------------------


def test_extract_text_from_message_handles_dict_blocks() -> None:
    message = {"content": [{"text": "a"}, {"text": "b"}]}

    assert _extract_text_from_message(message) == "ab"


def test_extract_text_from_message_handles_object_blocks() -> None:
    """Strands may hand back block objects rather than plain dicts."""

    class _Block:
        def __init__(self, text: str) -> None:
            self.text = text

    message = {"content": [_Block("x"), _Block("y")]}

    assert _extract_text_from_message(message) == "xy"


def test_extract_text_from_message_skips_non_text_blocks() -> None:
    message = {"content": [{"toolUse": {"name": "search"}}, {"text": "kept"}, {"text": ""}]}

    assert _extract_text_from_message(message) == "kept"


def test_extract_text_from_message_handles_missing_content() -> None:
    assert _extract_text_from_message({}) == ""


# ---------------------------------------------------------------------------
# _parse_json_model
# ---------------------------------------------------------------------------


def test_parse_json_model_ignores_prose_around_the_object() -> None:
    text = 'Result:\n{"name": "outer", "score": 1.0}\nThanks!'

    assert _parse_json_model(text, _Model).name == "outer"


def test_parse_json_model_falls_back_to_json_loads_for_non_strict_json() -> None:
    """``json.loads`` accepts NaN where pydantic's strict JSON parser does not."""
    parsed = _parse_json_model('{"name": "nan-case", "score": NaN}', _Model)

    assert parsed.name == "nan-case"
    assert math.isnan(parsed.score)


def test_parse_json_model_raises_when_no_object_is_present() -> None:
    with pytest.raises(ValueError, match="No JSON object found in text"):
        _parse_json_model("no braces here", _Model)


def test_parse_json_model_propagates_undecodable_json() -> None:
    """Both failure modes are ValueErrors: JSONDecodeError and pydantic's ValidationError."""
    with pytest.raises(ValueError):
        _parse_json_model("{not json at all}", _Model)
