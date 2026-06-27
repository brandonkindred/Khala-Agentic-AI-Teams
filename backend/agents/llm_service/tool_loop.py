"""
Multi-turn LLM tool loop: chat rounds until the model returns structured JSON (no tool calls).

Uses ``LLMClient.chat(response_format="json", ...)`` (implemented by Ollama / Dummy clients).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List

from .interface import LLMClient, LLMPermanentError

logger = logging.getLogger(__name__)


def _normalize_tool_calls_for_api(tool_calls: List[dict[str, Any]]) -> List[dict[str, Any]]:
    """Ensure function.arguments is a string for chat/completions payloads."""
    normalized: List[dict[str, Any]] = []
    for tc in tool_calls:
        entry = dict(tc)
        fn = dict(entry.get("function") or {})
        args = fn.get("arguments")
        if isinstance(args, dict):
            fn["arguments"] = json.dumps(args)
        elif args is None:
            fn["arguments"] = "{}"
        else:
            fn["arguments"] = str(args)
        entry["function"] = fn
        normalized.append(entry)
    return normalized


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {}
        try:
            out = json.loads(s)
            return out if isinstance(out, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def complete_json_with_tool_loop(
    llm: LLMClient,
    *,
    objective: str,
    user_prompt: str,
    system_prompt: str,
    tools: list,
    tool_handlers: Dict[str, Callable[[dict[str, Any]], Any]],
    max_rounds: int = 16,
    temperature: float = 0.2,
    think: bool = False,
) -> Dict[str, Any]:
    """
    Run a tool loop: the model may call tools (by name); each handler receives argument dicts
    and should return a JSON-serializable result stored as the tool message content.

    Stops when the model returns a JSON object **without** ``__tool_calls__`` (final structured output).

    :param objective: Required short phrase describing the call's purpose; forwarded
        to ``llm.chat`` on every round and stamped onto LLM logs/telemetry for attribution.
    """
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    for round_idx in range(max_rounds):
        result = llm.chat(
            messages,
            objective=objective,
            response_format="json",
            temperature=temperature,
            tools=tools,
            think=think,
        )
        if not isinstance(result, dict):
            raise LLMPermanentError(f"chat returned non-dict: {type(result)}")

        if "__tool_calls__" in result:
            tcalls = result["__tool_calls__"]
            if not isinstance(tcalls, list) or not tcalls:
                raise LLMPermanentError("LLM returned empty __tool_calls__")
            assistant_msg: dict = {
                "role": "assistant",
                "content": "",
                "tool_calls": _normalize_tool_calls_for_api(tcalls),
            }
            # DeepSeek thinking mode requires the tool-call turn's reasoning
            # to be echoed back on subsequent requests (the API 400s without
            # it). Emit both dialects: Ollama's OpenAI-compatible endpoint
            # reads `reasoning` (openai.go); DeepSeek-native reads
            # `reasoning_content`. Each backend ignores the other's key.
            if result.get("__reasoning_content__"):
                assistant_msg["reasoning"] = result["__reasoning_content__"]
                assistant_msg["reasoning_content"] = result["__reasoning_content__"]
            # Anthropic extended thinking requires the signed thinking blocks from a
            # tool-use turn to be echoed back unchanged on the next request (the API
            # 400s without them). The Claude client carries them in the envelope and
            # re-emits them when this assistant turn is replayed; other providers
            # never set the key, so this is a no-op for them.
            if result.get("__thinking_blocks__"):
                assistant_msg["thinking_blocks"] = result["__thinking_blocks__"]
            messages.append(assistant_msg)
            for tc in tcalls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = (fn.get("name") or "").strip()
                args = _parse_tool_arguments(fn.get("arguments"))
                tool_id = str(tc.get("id") or "")
                handler = tool_handlers.get(name)
                try:
                    if handler is None:
                        payload = {"success": False, "error": "unknown_tool", "message": name}
                    else:
                        payload = handler(args)
                except Exception as e:
                    logger.warning("tool handler %s raised: %s", name, e)
                    payload = {"success": False, "error": "handler_exception", "message": str(e)}
                if not isinstance(payload, str):
                    payload_str = json.dumps(payload)
                else:
                    payload_str = payload
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": payload_str,
                    }
                )
            logger.info("tool_loop round %d executed %d tool call(s)", round_idx + 1, len(tcalls))
            continue

        return result

    raise LLMPermanentError(f"Tool loop exceeded max_rounds={max_rounds}")
