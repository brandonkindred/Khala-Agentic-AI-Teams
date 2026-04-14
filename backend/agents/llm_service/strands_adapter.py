"""
Strands Agents SDK adapter for ``llm_service.LLMClient``.

This module exposes ``LLMClientModel``, a ``strands.models.Model`` implementation
that wraps any existing ``LLMClient`` (Ollama, Dummy, future providers). Strands
``Agent`` instances can then be constructed against the Khala LLM service and
automatically inherit rate limiting, telemetry, retries, JSON repair, and the
per-agent model routing that lives in ``llm_service.factory.get_client``.

Use via :func:`get_strands_model`::

    from llm_service import get_strands_model
    from strands import Agent

    model = get_strands_model(agent_key="qa_agent", temperature=0.1)
    agent = Agent(model=model, system_prompt="You are a QA expert.")
    result = agent("Review this diff: ...")

Design notes
------------
* ``LLMClient`` is synchronous. Strands ``Model.stream`` is an async generator.
  The adapter bridges via ``asyncio.to_thread`` so the blocking LLM call does
  not stall the event loop.
* Strands message format is Bedrock-style (``list[Message]`` with
  ``ContentBlock`` items). The adapter flattens these to the OpenAI-compatible
  chat shape that ``LLMClient.chat_json_round`` accepts.
* Tool specs are translated from Strands ``ToolSpec`` to the OpenAI
  ``{"type": "function", "function": {...}}`` shape used by
  ``LLMClient.{complete_json,chat_json_round}``.
* Responses from ``chat_json_round`` are replayed as a short synthetic stream:
  ``messageStart`` → one ``contentBlockDelta`` (text or tool use) → ``messageStop``.
  This matches what Strands' ``Agent`` loop expects without requiring the
  underlying client to actually stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from strands.models.model import Model
from strands.types.content import Messages
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

from .factory import get_client
from .interface import LLMClient

logger = logging.getLogger(__name__)

__all__ = ["LLMClientModel", "get_strands_model", "run_json_via_strands"]


# ---------------------------------------------------------------------------
# Message + tool conversion helpers
# ---------------------------------------------------------------------------


def _tool_result_content_to_text(content: List[Dict[str, Any]]) -> str:
    """Flatten Strands ``toolResult.content`` blocks into a single string payload."""
    parts: List[str] = []
    for block in content or []:
        if "text" in block:
            parts.append(str(block["text"]))
        elif "json" in block:
            parts.append(json.dumps(block["json"]))
        # image/document/video tool results are intentionally dropped: the
        # underlying ``LLMClient`` contract is text-in/text-out.
    return "\n".join(parts)


def _strands_messages_to_openai(messages: Messages) -> List[Dict[str, Any]]:
    """Convert Strands ``Messages`` to the OpenAI-compatible chat shape.

    Rules:

    * ``{text: ...}`` blocks accumulate into the outer message's ``content``.
    * ``{toolUse: ...}`` blocks emit ``tool_calls`` on an assistant message.
    * ``{toolResult: ...}`` blocks are flushed as their own ``role="tool"``
      messages (OpenAI's contract puts tool responses in a distinct message).
    * Unknown block types (image, document, reasoningContent, etc.) are
      skipped with a debug log — this adapter is text-and-tools only.
    """
    out: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        text_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []

        for block in msg.get("content", []) or []:
            if "text" in block:
                text_parts.append(str(block["text"]))
            elif "toolUse" in block:
                tu = block["toolUse"]
                args = tu.get("input", {})
                if not isinstance(args, str):
                    args = json.dumps(args)
                tool_calls.append(
                    {
                        "id": str(tu.get("toolUseId", "")),
                        "type": "function",
                        "function": {
                            "name": str(tu.get("name", "")),
                            "arguments": args,
                        },
                    }
                )
            elif "toolResult" in block:
                tr = block["toolResult"]
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(tr.get("toolUseId", "")),
                        "content": _tool_result_content_to_text(tr.get("content", [])),
                    }
                )
            else:
                logger.debug(
                    "strands_adapter: skipping unsupported content block %r", list(block.keys())
                )

        if tool_calls:
            # Assistant turns with tool calls must be emitted as assistant
            # regardless of the incoming ``role``. Strands only sets
            # ``toolUse`` content on assistant messages, so this is defensive.
            out.append(
                {
                    "role": "assistant",
                    "content": "\n".join(text_parts),
                    "tool_calls": tool_calls,
                }
            )
        elif text_parts:
            out.append({"role": role, "content": "\n".join(text_parts)})

    return out


def _tool_specs_to_openai(tool_specs: Optional[List[ToolSpec]]) -> Optional[List[Dict[str, Any]]]:
    """Convert Strands ``ToolSpec`` list to OpenAI function-tool definitions.

    Strands encodes the input schema as ``{"json": <schema>}``; we unwrap it
    when present but also accept a bare dict for forward compatibility.
    """
    if not tool_specs:
        return None
    out: List[Dict[str, Any]] = []
    for spec in tool_specs:
        input_schema = spec.get("inputSchema", {}) or {}
        parameters = (
            input_schema.get("json", input_schema) if isinstance(input_schema, dict) else {}
        )
        out.append(
            {
                "type": "function",
                "function": {
                    "name": spec.get("name", ""),
                    "description": spec.get("description", ""),
                    "parameters": parameters,
                },
            }
        )
    return out


# ---------------------------------------------------------------------------
# Model implementation
# ---------------------------------------------------------------------------


class LLMClientModel(Model):
    """Strands ``Model`` backed by an ``llm_service.LLMClient``.

    Parameters
    ----------
    client:
        The backing ``LLMClient``. Typically obtained via
        :func:`llm_service.get_client`, but any implementation is accepted
        (including ``DummyLLMClient`` for tests).
    agent_key:
        Optional agent identifier forwarded to telemetry / per-agent model
        routing. Purely informational on the adapter side; kept on the config
        so ``get_config()`` surfaces it.
    model_id:
        Human-readable model label for ``get_config``. Defaults to the backing
        client's class name.
    temperature / max_tokens / think:
        Default sampling parameters applied to every ``stream`` /
        ``structured_output`` call. Overridable per-call via ``update_config``
        or ``invocation_state``.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        agent_key: Optional[str] = None,
        model_id: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        think: bool = False,
    ) -> None:
        assert client is not None, "client is required"
        self._client = client
        self.config: Dict[str, Any] = {
            "agent_key": agent_key,
            "model_id": model_id or type(client).__name__,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "think": think,
        }

    # -- strands.models.Model required interface ---------------------------

    def update_config(self, **model_config: Any) -> None:
        """Shallow-merge ``model_config`` into the adapter's config dict."""
        self.config.update(model_config)

    def get_config(self) -> Dict[str, Any]:
        """Return the adapter config (agent_key, model_id, sampling params)."""
        return self.config

    async def stream(
        self,
        messages: Messages,
        tool_specs: Optional[List[ToolSpec]] = None,
        system_prompt: Optional[str] = None,
        *,
        tool_choice: Optional[ToolChoice] = None,
        system_prompt_content: Optional[List[Any]] = None,
        invocation_state: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Run one turn of the backing LLM and synthesize Strands stream events.

        The backing ``LLMClient.chat_json_round`` is called in a worker thread
        so the event loop stays responsive. The full assistant turn is emitted
        as a single content delta (text or tool use) — downstream Strands
        components expect complete blocks, not token-level streaming.

        ``tool_choice`` is accepted for interface compatibility but is not
        forwarded: ``LLMClient`` does not currently expose a tool_choice knob.
        """
        del tool_choice, system_prompt_content  # interface-only
        oai_messages = _strands_messages_to_openai(messages)
        if system_prompt:
            oai_messages.insert(0, {"role": "system", "content": system_prompt})

        oai_tools = _tool_specs_to_openai(tool_specs)

        # Per-call overrides come through ``invocation_state``; fall back to
        # the model's default config.
        state = invocation_state or {}
        temperature = float(state.get("temperature", self.config.get("temperature", 0.0)) or 0.0)
        max_tokens = state.get("max_tokens", self.config.get("max_tokens"))
        think = bool(state.get("think", self.config.get("think", False)))

        logger.debug(
            "strands_adapter.stream: messages=%d tools=%s temp=%s think=%s agent_key=%s",
            len(oai_messages),
            len(oai_tools) if oai_tools else 0,
            temperature,
            think,
            self.config.get("agent_key"),
        )

        result = await asyncio.to_thread(
            self._client.chat_json_round,
            oai_messages,
            temperature=temperature,
            tools=oai_tools,
            think=think,
            max_tokens=max_tokens,
        )

        yield {"messageStart": {"role": "assistant"}}

        tool_calls = None
        if isinstance(result, dict):
            tc = result.get("__tool_calls__")
            if isinstance(tc, list) and tc:
                tool_calls = tc

        if tool_calls is not None:
            for idx, call in enumerate(tool_calls):
                fn = (call or {}).get("function") or {}
                tool_name = str(fn.get("name") or call.get("name") or f"tool_{idx}")
                tool_id = str(call.get("id") or fn.get("id") or f"{tool_name}_{idx}")
                raw_args = fn.get("arguments", call.get("arguments", {}))
                if not isinstance(raw_args, str):
                    raw_args = json.dumps(raw_args)
                yield {
                    "contentBlockStart": {
                        "start": {"toolUse": {"name": tool_name, "toolUseId": tool_id}},
                    },
                }
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": raw_args}}}}
                yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return

        # Plain text / structured response: serialize dict results to JSON so
        # the caller receives deterministic content.
        if isinstance(result, str):
            text = result
        else:
            try:
                text = json.dumps(result)
            except (TypeError, ValueError):
                text = str(result)

        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": text}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}

    async def structured_output(
        self,
        output_model: type,
        prompt: Messages,
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Get structured output validated against a Pydantic model.

        Flattens the incoming message list to a single user prompt, calls
        ``LLMClient.complete_json`` in a worker thread, and feeds the dict
        through ``output_model.model_validate``. Raises ``ValueError`` if the
        response cannot be validated — matching the behavior of Strands'
        built-in Ollama/OpenAI models.
        """
        oai_messages = _strands_messages_to_openai(prompt)
        user_parts = [
            str(m.get("content") or "")
            for m in oai_messages
            if m.get("role") in ("user", "tool") and m.get("content")
        ]
        text_prompt = "\n\n".join(p for p in user_parts if p)

        temperature = float(self.config.get("temperature", 0.0) or 0.0)
        think = bool(self.config.get("think", False))

        data = await asyncio.to_thread(
            self._client.complete_json,
            text_prompt,
            temperature=temperature,
            system_prompt=system_prompt,
            think=think,
        )

        try:
            validated = output_model.model_validate(data)
        except Exception as exc:  # pragma: no cover - defensive
            raise ValueError(
                f"strands_adapter: failed to parse LLM response into {output_model.__name__}: {exc}"
            ) from exc

        yield {"output": validated}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_strands_model(
    agent_key: Optional[str] = None,
    *,
    temperature: float = 0.0,
    max_tokens: Optional[int] = None,
    think: bool = False,
    model_id: Optional[str] = None,
    client: Optional[LLMClient] = None,
) -> LLMClientModel:
    """Return a Strands-compatible ``Model`` wired to the Khala LLM service.

    This is the canonical entry point for constructing a Strands ``Agent``
    that should use the project's LLM stack. Under the hood it calls
    :func:`llm_service.get_client` (respecting ``LLM_PROVIDER``,
    ``LLM_MODEL_<agent_key>``, and the rest of the env contract) and wraps
    the result in :class:`LLMClientModel`.

    Pass ``client=`` explicitly to inject a ``DummyLLMClient`` or a mock in
    tests without touching the factory cache.
    """
    backing = client if client is not None else get_client(agent_key)
    return LLMClientModel(
        backing,
        agent_key=agent_key,
        model_id=model_id,
        temperature=temperature,
        max_tokens=max_tokens,
        think=think,
    )


def run_json_via_strands(
    client: LLMClient,
    *,
    system_prompt: str,
    user_prompt: str,
    agent_key: Optional[str] = None,
    temperature: float = 0.0,
    think: bool = False,
) -> Dict[str, Any]:
    """Single-shot LLM call through a fresh Strands ``Agent``, returning a
    JSON-parsed dict.

    Use this for agents whose downstream code does defensive ``data.get(...)``
    parsing and does **not** want the strictness of ``structured_output_model``.
    Wave 5 migrations (TechLeadAgent, ArchitectureExpertAgent) rely on this
    because they have many distinct call shapes and extensive fallback logic
    that would be fragile to encode as per-call Pydantic schemas.

    A fresh ``LLMClientModel`` + ``Agent`` is constructed on every call to
    avoid the Strands Agent message-history state leak that affected Wave
    1–4 migrations (see ``befcf0d``). The function returns an empty dict
    on any exception so callers can continue with their ``data.get(...)``
    defaults — matching the pre-migration behavior.
    """
    from strands import Agent  # noqa: PLC0415 — keep Strands optional at module load

    model = LLMClientModel(
        client,
        agent_key=agent_key,
        temperature=temperature,
        think=think,
    )
    agent = Agent(model=model, system_prompt=system_prompt)

    try:
        result = agent(user_prompt)
    except Exception as exc:  # noqa: BLE001 — LLM/validation errors must not crash the run
        logger.warning("run_json_via_strands: agent call failed: %s", exc)
        return {}

    message = getattr(result, "message", None) or {}
    for block in message.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}
