"""Generalized agent factory for Strands Graph/Swarm orchestration.

Wraps ``llm_service.get_strands_model()`` so every team gets correct
per-agent model resolution without hardcoding model strings.
"""

from __future__ import annotations

from typing import Any, Literal

from strands import Agent

from llm_service import get_strands_model

# Mirrors the values ``llm_service.get_strands_model``'s ``response_format``
# accepts, surfaced here so callers get static checking on ``output_mode``.
OutputMode = Literal["json", "text"]


def build_agent(
    *,
    name: str,
    system_prompt: str,
    agent_key: str | None = None,
    output_mode: OutputMode = "json",
    structured_output: Any | None = None,
    tools: list | None = None,
    description: str = "",
    callback_handler: Any | None = None,
) -> Agent:
    """Create a ``strands.Agent`` with centralized model resolution.

    Parameters
    ----------
    name:
        Unique agent name (used as graph node ID).
    system_prompt:
        Full system prompt defining the agent's role and instructions.
    agent_key:
        Key for per-agent model resolution via ``llm_service``.
        Falls back to ``LLM_MODEL`` env var when ``None``.
    output_mode:
        Declarative shape of this agent's output, forwarded to
        ``get_strands_model``'s ``response_format``. ``"json"`` (default)
        forces JSON output on the wire — use when the downstream consumer
        ``json.loads``/``model_validate_json``s the assistant content.
        ``"text"`` uses prose mode — use for conversational replies or
        template-based outputs the consumer parses from prose itself.
    structured_output:
        Optional Pydantic ``BaseModel`` subclass for typed output. When set,
        ``output_mode`` is ignored — Strands routes through its
        ``structured_output_model`` flow which uses ``complete_json``
        regardless of mode.
    tools:
        Optional list of tools the agent may invoke.
    description:
        Short human-readable description of the agent's purpose.
    callback_handler:
        Optional callback handler for streaming events.
    """
    if output_mode not in ("json", "text"):
        raise ValueError(f"output_mode must be 'json' or 'text', got {output_mode!r}")
    kwargs: dict[str, Any] = {
        "name": name,
        "system_prompt": system_prompt,
        "model": get_strands_model(agent_key, response_format=output_mode),
        "callback_handler": callback_handler,
    }
    if structured_output is not None:
        kwargs["structured_output_model"] = structured_output
    if tools:
        kwargs["tools"] = tools
    if description:
        kwargs["description"] = description
    return Agent(**kwargs)
