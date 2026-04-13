"""Graph invocation helpers.

Handles the async-to-sync bridge and result extraction that every
orchestrator needs when calling a Strands Graph.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
from typing import Any, TypeVar

from pydantic import BaseModel
from strands.multiagent.graph import Graph

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def invoke_graph_sync(graph: Graph, task: str) -> Any:
    """Invoke a Strands ``Graph`` synchronously, handling running event loops.

    When called from inside an already-running asyncio loop (e.g. inside a
    Temporal activity or FastAPI endpoint), spins up a thread-pool executor
    to avoid ``RuntimeError: This event loop is already running``.

    Parameters
    ----------
    graph:
        A built Strands ``Graph`` instance.
    task:
        The task string to pass to the graph.

    Returns
    -------
    The raw graph result (``MultiAgentResult``).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(lambda: asyncio.run(graph.invoke_async(task))).result()
    return asyncio.run(graph.invoke_async(task))


def extract_node_output(
    result: Any,
    node_id: str,
    model_class: type[T],
) -> T:
    """Extract and parse a typed output from a graph node result.

    Attempts to find JSON in the node's last agent message and parse it
    into the given Pydantic model.  Falls back to a default instance if
    parsing fails.

    Parameters
    ----------
    result:
        The raw ``MultiAgentResult`` from ``graph.invoke_async()``.
    node_id:
        The graph node whose output to extract.
    model_class:
        Pydantic ``BaseModel`` subclass to parse into.

    Returns
    -------
    An instance of *model_class*.
    """
    try:
        if hasattr(result, "result") and hasattr(result.result, "get"):
            node_result = result.result.get(node_id)
            if node_result and hasattr(node_result, "result"):
                agent_results = node_result.get_agent_results()
                if agent_results:
                    last = agent_results[-1]
                    if hasattr(last, "message") and last.message:
                        text = _extract_text_from_message(last.message)
                        if text:
                            return _parse_json_model(text, model_class)
    except Exception:
        logger.debug("Failed to extract output for node %s", node_id, exc_info=True)
    return model_class()


def extract_node_text(result: Any, node_id: str) -> str:
    """Extract raw text output from a graph node result.

    Parameters
    ----------
    result:
        The raw ``MultiAgentResult`` from ``graph.invoke_async()``.
    node_id:
        The graph node whose output to extract.

    Returns
    -------
    The text content, or empty string if extraction fails.
    """
    try:
        if hasattr(result, "result") and hasattr(result.result, "get"):
            node_result = result.result.get(node_id)
            if node_result and hasattr(node_result, "result"):
                agent_results = node_result.get_agent_results()
                if agent_results:
                    last = agent_results[-1]
                    if hasattr(last, "message") and last.message:
                        return _extract_text_from_message(last.message)
    except Exception:
        logger.debug("Failed to extract text for node %s", node_id, exc_info=True)
    return ""


def _extract_text_from_message(message: dict) -> str:
    """Pull text content from a Strands agent message."""
    text = ""
    for block in message.get("content", []):
        if isinstance(block, dict) and block.get("text"):
            text += block["text"]
        elif hasattr(block, "text"):
            text += block.text
    return text


def _parse_json_model(text: str, model_class: type[T]) -> T:
    """Find the outermost JSON object in *text* and parse it as *model_class*."""
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        json_str = text[start:end]
        try:
            return model_class.model_validate_json(json_str)
        except Exception:
            # Try loading as dict first to handle non-strict JSON
            data = json.loads(json_str)
            return model_class.model_validate(data)
    raise ValueError("No JSON object found in text")
