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
  chat shape that ``LLMClient.chat`` accepts.
* Tool specs are translated from Strands ``ToolSpec`` to the OpenAI
  ``{"type": "function", "function": {...}}`` shape used by
  ``LLMClient.{complete_json,chat}``.
* Responses are replayed as a short synthetic stream:
  ``messageStart`` → one ``contentBlockDelta`` (text or tool use) → ``messageStop``.
  This matches what Strands' ``Agent`` loop expects without requiring the
  underlying client to actually stream.
* ``response_format`` selects the backing call:
  - ``"json"`` (default) → ``chat(response_format="json")`` — forces JSON output on the
    wire and parses the result. This preserves backward compatibility for the
    many Strands agents that ask for JSON in their system prompt and then
    ``json.loads`` the assistant content (e.g. ``RoutePlannerAgent``).
  - ``"text"`` → ``chat(response_format="text")`` — free-form prose; no ``response_format`` is
    forced and no JSON parsing is attempted. Use this only for conversational
    agents whose replies should be natural language (e.g. branding assistant).
* ``Agent(structured_output_model=...)`` routes structured output through the
  normal tool-calling event loop, so the live production path is
  ``chat()``/``stream()`` handling a ``StructuredOutputTool`` like any other
  tool call. This class's own ``structured_output()`` method (which always
  uses ``complete_json``) is retained only for Strands' deprecated
  ``Agent.structured_output()`` API and is not on the hot path for normal
  agent runs.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional, Union

from strands.models.model import Model
from strands.types.content import Messages
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

from .attribution import caller_agent, caller_team, current_attribution, llm_attribution
from .cache_breakpoint import CacheBreakpoint
from .factory import client_agent_key, get_client, unwrap_client
from .interface import LLMClient, record_complete_json_turn, take_complete_json_turns
from .util import _flatten_system_prompt_content

logger = logging.getLogger(__name__)

__all__ = ["LLMClientConfig", "LLMClientModel", "run_json_via_strands"]


ResponseFormat = Literal["json", "text"]


@dataclasses.dataclass(frozen=True)
class LLMClientConfig:
    """Immutable configuration for an ``LLMClientModel``.

    Replaces the previous mutable ``self.config: Dict[str, Any]`` plus ad-hoc
    ``clone()`` enumeration of known keys. The dataclass enforces the contract
    in one place: each field is an explicit name with a default, ``clone()``
    becomes ``dataclasses.replace`` and inherits unknown-kwarg validation for
    free, and unknown keys never silently disappear into a dict.

    ``response_format`` is validated in ``__post_init__`` rather than at the
    ``LLMClientModel`` boundary because the same validation is also needed by
    ``replace``-style updates.
    """

    agent_key: Optional[str] = None
    model_id: Optional[str] = None
    temperature: float = 0.0
    max_tokens: Optional[int] = None
    think: Optional[Union[bool, str]] = None
    response_format: ResponseFormat = "json"

    def __post_init__(self) -> None:
        if self.response_format not in ("json", "text"):
            raise ValueError(
                f"response_format must be 'json' or 'text', got {self.response_format!r}"
            )

    def as_dict(self) -> Dict[str, Any]:
        """Return a plain dict view — Strands' ``Model.get_config`` contract
        returns a mutable mapping. Mutations on the returned dict do NOT
        affect the underlying frozen dataclass."""
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Message + tool conversion helpers
# ---------------------------------------------------------------------------


def _has_cache_breakpoint(system_prompt_content: Optional[List[Any]]) -> bool:
    """True if any element of ``system_prompt_content`` is a ``CacheBreakpoint``.

    Preconditions:
        - ``system_prompt_content`` is ``None`` or a list of content blocks.

    Postconditions:
        - Returns ``False`` for ``None``/an empty list. Returns ``True`` iff
          at least one element is a ``CacheBreakpoint`` instance. O(n) in the
          list length; never raises.
    """
    return any(isinstance(block, CacheBreakpoint) for block in system_prompt_content or [])


def _render_system_content(
    system_prompt: Optional[str],
    system_prompt_content: Optional[List[Any]],
    *,
    supports_prompt_caching: bool,
) -> Optional[Union[str, List[Dict[str, Any]]]]:
    """Render the combined system content for one ``stream()`` call.

    Threads a ``CacheBreakpoint`` marked prefix (see
    ``llm_service.cache_breakpoint``) through to the provider's wire-level
    cache-control block when the backing client supports it; degrades it to
    plain text (via ``_flatten_system_prompt_content``, which unwraps
    ``.text``) otherwise — a documented no-op with no output change and no
    error.

    Preconditions:
        - ``system_prompt`` is ``None`` or a str.
        - ``system_prompt_content`` is ``None`` or a list whose elements are
          dicts (``{"text": ...}``), ``CacheBreakpoint`` instances, or other
          objects (stringified — matches ``_flatten_system_prompt_content``).
        - ``supports_prompt_caching`` reflects the backing client's
          capability already resolved by the caller for this call.

    Postconditions:
        - When ``system_prompt_content`` contains no ``CacheBreakpoint``, OR
          ``supports_prompt_caching`` is False: returns EXACTLY the
          pre-existing plain-string computation — ``system_prompt`` and the
          flattened ``system_prompt_content`` joined by ``"\\n\\n"`` (``None``
          when both are empty). This is the byte-identical regression-safety
          path for every caller that does not use ``CacheBreakpoint``, and
          the documented no-op degrade for a caller that does but whose
          backing client cannot honor it.
        - Otherwise: returns a ``list[dict]`` of Anthropic-shaped
          ``{"type": "text", "text": ...}`` blocks — an optional leading
          block for ``system_prompt``, then one block per
          ``system_prompt_content`` entry in order, with each
          ``CacheBreakpoint`` additionally carrying ``"cache_control":
          {"type": "ephemeral"}``.
        - Never raises. Never mutates ``system_prompt_content`` or its
          elements.
    """
    if not _has_cache_breakpoint(system_prompt_content) or not supports_prompt_caching:
        return (
            "\n\n".join(
                part
                for part in (system_prompt, _flatten_system_prompt_content(system_prompt_content))
                if part
            )
            or None
        )
    blocks: List[Dict[str, Any]] = []
    if system_prompt:
        blocks.append({"type": "text", "text": system_prompt})
    for block in system_prompt_content or []:
        if isinstance(block, CacheBreakpoint):
            blocks.append(
                {"type": "text", "text": block.text, "cache_control": {"type": "ephemeral"}}
            )
        elif isinstance(block, dict):
            text = str(block.get("text", "") or "")
            if text:
                blocks.append({"type": "text", "text": text})
        elif block:
            blocks.append({"type": "text", "text": str(block)})
    return blocks or None


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
    * ``{reasoningContent: ...}`` blocks are replayed as ``reasoning_content``
      on the assistant tool-call message (DeepSeek thinking mode requires it
      on subsequent requests).
    * Unknown block types (image, document, etc.) are skipped with a debug
      log — this adapter is text, reasoning, and tools only.
    """
    out: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
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
            elif "reasoningContent" in block:
                # DeepSeek thinking mode requires the tool-call turn's
                # reasoning to be replayed on subsequent requests (400
                # otherwise) — collect it instead of dropping the block.
                rc = block.get("reasoningContent") or {}
                text = ((rc.get("reasoningText") or {}).get("text")) or ""
                if text:
                    reasoning_parts.append(str(text))
            else:
                logger.debug(
                    "strands_adapter: skipping unsupported content block %r", list(block.keys())
                )

        if tool_calls:
            # Assistant turns with tool calls must be emitted as assistant
            # regardless of the incoming ``role``. Strands only sets
            # ``toolUse`` content on assistant messages, so this is defensive.
            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(text_parts),
                "tool_calls": tool_calls,
            }
            if reasoning_parts:
                # Both dialects: Ollama's OpenAI-compatible endpoint reads
                # `reasoning` (openai.go); DeepSeek-native reads
                # `reasoning_content`. Each backend ignores the other's key.
                joined_reasoning = "\n".join(reasoning_parts)
                assistant_msg["reasoning"] = joined_reasoning
                assistant_msg["reasoning_content"] = joined_reasoning
            out.append(assistant_msg)
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
    response_format:
        Either ``"json"`` (default) or ``"text"``. ``"json"`` routes ``stream``
        through ``chat(response_format="json")`` (forces JSON output on the wire) which is
        the safe default for the many Strands agents that ask for JSON in
        their system prompt and then ``json.loads`` the result. ``"text"``
        routes through ``chat(response_format="text")`` and returns raw prose — use for
        conversational agents only.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        agent_key: Optional[str] = None,
        model_id: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        think: Optional[Union[bool, str]] = None,
        response_format: ResponseFormat = "json",
    ) -> None:
        """Construct the adapter around a backing ``LLMClient``.

        See the class docstring above for parameter semantics.

        Preconditions:
            - ``client`` is not ``None``.

        Postconditions:
            - ``self.client`` is ``client``.
            - ``self._config`` is a validated ``LLMClientConfig`` built from
              the remaining keyword arguments.
        """
        if client is None:
            # Explicit validation rather than ``assert``: the precondition
            # must hold even under ``python -O`` (which strips asserts), or a
            # missing client would surface as a confusing downstream
            # AttributeError instead of a clear construction-time failure.
            raise ValueError("client is required")
        self._client = client
        # The dataclass enforces ``response_format ∈ {"json","text"}``;
        # invalid values raise ``ValueError`` from ``__post_init__``.
        self._config = LLMClientConfig(
            agent_key=agent_key,
            model_id=model_id or type(client).__name__,
            temperature=temperature,
            max_tokens=max_tokens,
            think=think,
            response_format=response_format,
        )

    # -- strands.models.Model required interface ---------------------------

    def update_config(self, **model_config: Any) -> None:
        """Update the fields listed in ``model_config`` on this model's config.

        Strands' ``Model.update_config`` is part of the public contract, so we
        keep the method name. Unlike the previous mutable-dict implementation,
        this builds a new ``LLMClientConfig`` (which validates), so unknown
        kwargs raise ``TypeError`` instead of being silently retained.

        Postconditions:
            - Fields present in ``model_config`` are updated to the given
              values; unspecified fields retain their current values.
            - Raises ``TypeError`` if ``model_config`` contains a key that is
              not a field of ``LLMClientConfig``, or ``ValueError`` if the
              resulting ``response_format`` is invalid.
        """
        self._config = dataclasses.replace(self._config, **model_config)

    def get_config(self) -> Dict[str, Any]:
        """Return the adapter config as a plain dict — Strands' contract.

        The returned dict is a copy; mutations don't affect the frozen
        underlying ``LLMClientConfig``.
        """
        return self._config.as_dict()

    @property
    def config(self) -> Dict[str, Any]:
        """Strands' ``Model`` contract exposes ``config`` as a dict — its
        ``Agent`` loop calls ``model.config.get(...)`` internally. Return the
        dict view here so that path keeps working; the frozen typed
        ``LLMClientConfig`` is available on ``self._config`` for internal
        adapter use where attribute access is cleaner."""
        return self._config.as_dict()

    @property
    def client(self) -> LLMClient:
        """Public accessor for the backing ``LLMClient``.

        Lets callers obtain the underlying client without reaching into the
        private ``_client`` attribute (e.g. to re-wrap it in a different
        ``response_format`` when this model cannot be cloned in place).

        Postconditions:
            - Returns the non-None ``LLMClient`` this adapter was built with.
        """
        return self._client

    def get_max_context_tokens(self) -> int:
        """Delegate to the backing client (``LLMClient`` contract).

        Context-sizing and compaction helpers are typed against ``LLMClient``
        but are routinely handed this adapter (e.g. the SE quality gates'
        default ``llm_getter`` returns ``get_strands_model(...)``). Without
        delegation every such call raised ``AttributeError`` — code review
        failed closed and ``compaction`` silently degraded to a 16384-token
        assumption behind its ``hasattr`` guard.

        Postconditions:
            - Returns the backing client's max context tokens.
        """
        return self._client.get_max_context_tokens()

    def supports_structured_output(self) -> bool:
        """Delegate to the backing ``LLMClient`` (see ``LLMClient.supports_structured_output``).

        Postconditions:
            - Returns the backing client's capability flag. Synchronous, no
              network call, never raises (assuming the backing client's
              override doesn't — the default and Ollama's override both
              don't).
        """
        return self._client.supports_structured_output()

    def supports_prompt_caching(self) -> bool:
        """Delegate to the backing ``LLMClient`` (see ``LLMClient.supports_prompt_caching``).

        Postconditions:
            - Returns the backing client's capability flag. Synchronous, no
              network call, never raises (assuming the backing client's
              override doesn't).
        """
        return self._client.supports_prompt_caching()

    def clone(self, **overrides: Any) -> "LLMClientModel":
        """Return a new ``LLMClientModel`` sharing the backing client but with
        per-field overrides applied to the config.

        Use this when one caller (e.g. ``BlogWriterAgent``) needs both a JSON
        and a text variant of the same upstream model — the cached
        ``get_strands_model`` is for the canonical default, ``clone`` is for
        deriving the sibling without re-hitting the factory or constructing a
        fresh backing client.

        Example::

            text_model = json_model.clone(response_format="text")

        The new model is constructed via the normal ``__init__`` path so it
        re-runs every invariant (``client is not None`` validation, dataclass
        ``__post_init__`` validation). The cost is one extra
        ``LLMClientConfig`` construction; the win is the sibling stays valid
        if ``__init__`` ever grows additional setup.
        """
        new_config = dataclasses.replace(self._config, **overrides)
        return LLMClientModel(
            self._client,
            agent_key=new_config.agent_key,
            model_id=new_config.model_id,
            temperature=new_config.temperature,
            max_tokens=new_config.max_tokens,
            think=new_config.think,
            response_format=new_config.response_format,
        )

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

        The backing ``LLMClient`` call (``chat(response_format="json")`` by default, or
        ``chat(response_format="text")`` when the model was configured with
        ``response_format="text"``) is dispatched in a worker thread so the
        event loop stays responsive. The full assistant turn is emitted as a
        single content delta (text or tool use) — downstream Strands components
        expect complete blocks, not token-level streaming.

        Defaulting to ``chat(response_format="json")`` preserves backward compatibility for
        Strands agents that ask for JSON in their system prompt and then
        ``json.loads`` the assistant content (e.g.
        ``RoutePlannerAgent``). Conversational agents that want free-form
        prose opt into ``response_format="text"`` and are routed through
        ``chat(response_format="text")`` instead.

        ``system_prompt`` (a plain string) and ``system_prompt_content``
        (Strands' structured content-block form, e.g. ``[{"text": "..."}]``)
        are both accepted and merged into a single ``{"role": "system", ...}``
        message: ``system_prompt_content`` is flattened to text and appended
        after ``system_prompt`` when both are present. Either may be omitted;
        when both are absent, no system message is emitted. A
        ``CacheBreakpoint`` (see ``llm_service.cache_breakpoint``) inside
        ``system_prompt_content`` becomes an Anthropic ``cache_control``
        breakpoint block on the outgoing request when
        ``self._client.supports_prompt_caching()`` is True; otherwise it
        degrades to plain text (its ``.text``) — a documented no-op with no
        output change and no error. See ``_render_system_content``.

        ``tool_choice`` is accepted for interface compatibility but is not
        forwarded: ``LLMClient`` does not currently expose a tool_choice knob.
        """
        del tool_choice  # interface-only: LLMClient exposes no tool_choice knob
        oai_messages = _strands_messages_to_openai(messages)
        has_cache_breakpoint = _has_cache_breakpoint(system_prompt_content)
        system_content = _render_system_content(
            system_prompt,
            system_prompt_content,
            supports_prompt_caching=(
                self._client.supports_prompt_caching() if has_cache_breakpoint else False
            ),
        )
        if system_content:
            oai_messages.insert(0, {"role": "system", "content": system_content})

        oai_tools = _tool_specs_to_openai(tool_specs)

        # Per-call overrides come through ``invocation_state``; fall back to
        # the model's default config. Building a transient LLMClientConfig
        # gives us the same field-level validation as ``clone()`` /
        # ``update_config`` for free — a typo'd ``response_format="Text"``
        # raises ``ValueError`` from ``LLMClientConfig.__post_init__``
        # instead of silently falling back to JSON mode.
        state = invocation_state or {}
        cfg = self._config
        try:
            call_cfg = dataclasses.replace(
                cfg,
                **{
                    k: state[k]
                    for k in ("temperature", "max_tokens", "think", "response_format")
                    if k in state
                },
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invocation_state contains invalid override: {exc}") from exc
        temperature = float(call_cfg.temperature or 0.0)
        max_tokens = call_cfg.max_tokens
        think = call_cfg.think  # bool | str | None — never coerce; levels must survive
        response_format = call_cfg.response_format

        logger.debug(
            "strands_adapter.stream: messages=%d tools=%s temp=%s think=%s response_format=%s agent_key=%s",
            len(oai_messages),
            len(oai_tools) if oai_tools else 0,
            temperature,
            think,
            response_format,
            cfg.agent_key,
        )

        # Derive team + agent identity here, on the calling task whose stack still
        # holds the originating agent frame; binding into the context means they
        # survive the ``to_thread`` hand-off (asyncio copies the context), where
        # the worker thread's stack no longer reaches the agent.
        #
        # Priority for the agent key: the explicitly configured ``cfg.agent_key``
        # (which also reflects ``clone``/``update_config`` changes) wins; then a
        # key already bound on the context by an orchestrator; then the key held
        # by the backing ``_AttributingClient`` when the model was built via
        # ``get_strands_model(client=get_client("backend"))`` *without* repeating
        # ``agent_key`` — recovering it here is essential because the dispatch
        # below unwraps that client, discarding its binding. Only when none of
        # those authoritative keys exist does a path-derived identity fill the
        # field so unkeyed ``get_strands_model()`` calls aren't recorded as
        # ``agent=-``. The configured objective is a fallback — a caller that
        # bound a task-specific objective (e.g. the PA wrapper) keeps it.
        agent_key = (
            cfg.agent_key
            or current_attribution().agent_key
            or client_agent_key(self._client)
            or caller_agent()
        )
        team = current_attribution().team or caller_team()
        objective = (
            current_attribution().objective or f"strands agent turn ({cfg.agent_key or 'agent'})"
        )
        # Dispatch to the raw (unwrapped) client so that if ``self._client`` is
        # an ``_AttributingClient`` (from ``get_client(agent_key)``), its inner
        # ``llm_attribution(agent_key=...)`` binding does not override the key
        # we set above.  The correct key is already on the context via the
        # outer ``with llm_attribution(...)``; bypassing the wrapper ensures
        # ``cfg.agent_key`` (which may differ after ``clone``/``update_config``)
        # is the effective binding rather than the wrapper's original key.
        turn_started = time.monotonic()
        client = unwrap_client(self._client)
        worker_turns: list[tuple[str, str, float]] = []

        def _chat_in_worker() -> Any:
            # ContextVar writes in this thread do not copy back to the caller.
            # Drop the inherited snapshot, then stash turns on the shared list
            # even when chat() raises (self-correction that still fails).
            take_complete_json_turns()
            try:
                return client.chat(
                    oai_messages,
                    objective=objective,
                    response_format=response_format,
                    temperature=temperature,
                    tools=oai_tools,
                    think=think,
                    max_tokens=max_tokens,
                )
            finally:
                worker_turns.extend(take_complete_json_turns())

        def _replay_worker_turns() -> None:
            if worker_turns:
                for turn_prompt, turn_response, started in worker_turns:
                    record_complete_json_turn(turn_prompt, turn_response, started_monotonic=started)
                return
            try:
                observer_response = json.dumps(result, default=str)
            except (TypeError, ValueError):
                observer_response = str(result)
            record_complete_json_turn(
                json.dumps(oai_messages, default=str),
                observer_response,
                started_monotonic=turn_started,
            )

        chat_error: BaseException | None = None
        result: Any = None
        with llm_attribution(agent_key=agent_key or None, team=team):
            try:
                result = await asyncio.to_thread(_chat_in_worker)
            except BaseException as exc:
                chat_error = exc
        if worker_turns:
            for turn_prompt, turn_response, started in worker_turns:
                record_complete_json_turn(turn_prompt, turn_response, started_monotonic=started)
        elif chat_error is None:
            _replay_worker_turns()
        if chat_error is not None:
            raise chat_error

        yield {"messageStart": {"role": "assistant"}}

        tool_calls = None
        if isinstance(result, dict):
            tc = result.get("__tool_calls__")
            if isinstance(tc, list) and tc:
                tool_calls = tc

        if tool_calls is not None:
            # Surface the tool-call turn's reasoning as a strands
            # reasoningContent block so the Agent's message history carries
            # it back through _strands_messages_to_openai on the next turn —
            # DeepSeek thinking mode 400s when it is omitted.
            reasoning = result.get("__reasoning_content__") if isinstance(result, dict) else None
            if reasoning:
                yield {"contentBlockStart": {"start": {}}}
                yield {
                    "contentBlockDelta": {"delta": {"reasoningContent": {"text": str(reasoning)}}}
                }
                yield {"contentBlockStop": {}}
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

        # Plain content. ``chat(response_format="json")`` returns a dict (we JSON-serialize
        # so downstream JSON-parsing agents like ``RoutePlannerAgent`` see
        # well-formed JSON text); ``chat(response_format="text")`` returns a string (used as-is).
        # A dict result from ``chat(response_format="text")`` is also serialized defensively.
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
        ``LLMClient.complete_json`` in a worker thread — passing ``output_model``
        through as ``structured_output_model`` so a client that supports
        class-identity routing (e.g. the dummy stub) doesn't have to infer it
        from prompt text — and feeds the dict through
        ``output_model.model_validate``. Raises ``ValueError`` if the response
        cannot be validated — matching the behavior of Strands' built-in
        Ollama/OpenAI models.
        """
        oai_messages = _strands_messages_to_openai(prompt)
        user_parts = [
            str(m.get("content") or "")
            for m in oai_messages
            if m.get("role") in ("user", "tool") and m.get("content")
        ]
        text_prompt = "\n\n".join(p for p in user_parts if p)

        temperature = float(self._config.temperature or 0.0)
        think = self._config.think  # bool | str | None — never coerce; levels must survive

        # Bind the team + agent identity on the calling task (see ``stream``) so
        # they survive the ``to_thread`` hand-off into the worker thread. Same
        # key priority as ``stream``: configured key, then a context-bound key,
        # then the backing ``_AttributingClient``'s key (which the unwrap below
        # would otherwise discard), and only then a path-derived fallback. The
        # configured objective is a fallback — a bound task-specific one wins.
        configured_key = getattr(self._config, "agent_key", "") or ""
        agent_key = (
            configured_key
            or current_attribution().agent_key
            or client_agent_key(self._client)
            or caller_agent()
        )
        team = current_attribution().team or caller_team()
        objective = (
            current_attribution().objective
            or f"strands structured output ({configured_key or 'agent'})"
        )
        with llm_attribution(agent_key=agent_key or None, team=team):
            data = await asyncio.to_thread(
                unwrap_client(self._client).complete_json,
                text_prompt,
                objective=objective,
                temperature=temperature,
                system_prompt=system_prompt,
                think=think,
                structured_output_model=output_model,
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


def _get_strands_model(
    agent_key: Optional[str] = None,
    *,
    temperature: float = 0.0,
    max_tokens: Optional[int] = None,
    think: Optional[Union[bool, str]] = None,
    model_id: Optional[str] = None,
    client: Optional[LLMClient] = None,
    response_format: str = "json",
) -> LLMClientModel:
    """Construct a Strands-compatible ``Model`` wired to a raw ``LLMClient``.

    This is a low-level, package-private helper: the canonical public entry
    point for constructing a Strands ``Agent`` that should use the project's
    LLM stack is :func:`llm_service.get_strands_model` (backed by
    ``strands_provider``), which adds provider resolution, model caching, and
    API-key-fingerprint cache invalidation on top of a directly-built
    :class:`LLMClientModel`. This function is used directly, and intentionally,
    only by ``strategy_lab.model_factory`` where the caller needs to inject
    its own timeout-scoped client and bypass the provider cache. Under the
    hood it calls :func:`llm_service.get_client` (respecting ``LLM_PROVIDER``,
    ``LLM_MODEL_<agent_key>``, and the rest of the env contract) and wraps
    the result in :class:`LLMClientModel`.

    ``response_format`` defaults to ``"json"`` (forces JSON output on the wire
    via ``chat(response_format="json")``) to preserve backward compatibility for Strands
    agents whose system prompts ask for JSON. Pass ``response_format="text"``
    for conversational agents that need free-form natural-language replies.

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
        response_format=response_format,
    )


def run_json_via_strands(
    client: LLMClient,
    *,
    system_prompt: str,
    user_prompt: str,
    agent_key: Optional[str] = None,
    temperature: float = 0.0,
    think: Optional[Union[bool, str]] = None,
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
