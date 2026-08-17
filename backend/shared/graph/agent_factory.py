"""Generalized agent factory for Strands Graph/Swarm orchestration.

Wraps ``llm_service.get_strands_model()`` so every team gets correct
per-agent model resolution without hardcoding model strings.
"""

from __future__ import annotations

from typing import Any

from strands import Agent

from llm_service import get_strands_model


def build_agent(
    *,
    name: str,
    system_prompt: str,
    agent_key: str | None = None,
    response_format: str = "json",
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
    response_format:
        Declarative shape of this agent's output, kept co-located with the
        system prompt that produces it. ``"json"`` (default) forces
        ``response_format=json_object`` on the wire — use when the downstream
        consumer ``json.loads`` / ``model_validate_json`` the assistant
        content. ``"text"`` uses prose mode — use for conversational replies
        or template-based outputs where the consumer extracts structured
        data from prose itself. Ignored when ``structured_output`` is set —
        Strands routes that flow through ``complete_json`` regardless of
        mode.
    structured_output:
        Optional Pydantic ``BaseModel`` subclass for typed output.
    tools:
        Optional list of tools the agent may invoke.
    description:
        Short human-readable description of the agent's purpose.
    callback_handler:
        Optional callback handler for streaming events.
    """
    if response_format not in ("json", "text"):
        raise ValueError(f"response_format must be 'json' or 'text', got {response_format!r}")
    kwargs: dict[str, Any] = {
        "name": name,
        "system_prompt": system_prompt,
        "model": get_strands_model(agent_key, response_format=response_format),
        "callback_handler": callback_handler,
    }
    if structured_output is not None:
        kwargs["structured_output_model"] = structured_output
    if tools:
        kwargs["tools"] = tools
    if description:
        kwargs["description"] = description
    return Agent(**kwargs)
