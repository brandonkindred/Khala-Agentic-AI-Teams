"""Hard per-run tool-call cap for code-review Strands agents.

The tool-side budget in ``false_positive_filter._make_call_tracker`` is
*advisory*: once a run exceeds it, every further tool call skips its real
lookup and returns a stop directive asking the model to answer now. A model
that ignores that directive keeps emitting the same tool call, Strands keeps
executing it (a no-op that returns the same directive), and the event loop
runs forever — the agent never reaches a ``stopReason`` other than
``tool_use``, and nothing caps the iterations: Strands applies a turn budget
only when the caller passes ``limits`` to the invocation, which none of these
callers do. Observed in production as a code-review run repeating one
identical ``read_file`` call hundreds of times, burning an LLM round trip
each time.

:class:`ToolCallBudgetModel` closes that loop at the layer that can end it
unilaterally, whatever the caller configures: the model. It counts the tool
uses a run has emitted and, once the cap is reached, drops every further
tool-use block and rewrites a ``tool_use`` stop reason to ``end_turn`` — so
the Strands event loop terminates no matter what the model emits.

Invariants:
    - The wrapper never increases the number of tool calls a run makes and
      never alters events before the cap is reached (byte-identical
      passthrough).
    - Once the cap is reached, no ``toolUse`` block and no
      ``stopReason="tool_use"`` can leave this model, so the agent loop is
      guaranteed to terminate on that turn.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from strands.models.model import Model as _StrandsModel

from software_engineering_team.shared.env_config import env_int

logger = logging.getLogger(__name__)

_MISSING = object()

# Hard stop on total tool calls one Strands agent run may make. Deliberately
# above ``false_positive_filter._MAX_TOTAL_TOOL_CALLS`` (the advisory budget
# baked into the tools themselves) so a cooperating model still gets its
# "stop and answer now" nudge and a few turns to act on it; this cap only
# catches the model that ignores it.
DEFAULT_AGENT_TOOL_CALL_CAP = 50

_CAP_ENV = "CODE_REVIEW_AGENT_TOOL_CALL_CAP"

_BUDGET_DIRECTIVE = (
    "Your tool-call budget for this task is exhausted: any further tool call "
    "you make will be ignored and will return nothing. Answer now, in prose, "
    "using only what you have already inspected. If you could not finish "
    "investigating something, say so explicitly and be conservative in your "
    "conclusions rather than guessing."
)

_NO_OUTPUT_FALLBACK = (
    "(No conclusion was reached: this run exhausted its tool-call budget "
    "without producing an answer.)"
)


def resolve_agent_tool_call_cap() -> int:
    """Per-run hard cap on agent tool calls, from ``CODE_REVIEW_AGENT_TOOL_CALL_CAP``.

    Postconditions:
        - Returns an int >= 1; garbage or unset env → ``DEFAULT_AGENT_TOOL_CALL_CAP``.
    """
    return env_int(_CAP_ENV, DEFAULT_AGENT_TOOL_CALL_CAP, 1)


def _is_tool_use_start(event: Any) -> bool:
    """Whether ``event`` opens a ``toolUse`` content block.

    Preconditions:
        - ``event`` is any Strands stream event (dict-shaped or not).
    Postconditions:
        - Returns ``True`` only for ``contentBlockStart`` events whose
          ``start`` carries a ``toolUse``. Never raises on a malformed event.
    """
    if not isinstance(event, dict):
        return False
    start = event.get("contentBlockStart")
    if not isinstance(start, dict):
        return False
    inner = start.get("start")
    return isinstance(inner, dict) and "toolUse" in inner


def _block_delta(event: Any) -> Dict[str, Any]:
    """The ``contentBlockDelta.delta`` mapping of ``event``, or ``{}``.

    Postconditions:
        - Returns a dict; anything not shaped like a delta event (including a
          truthy non-dict under either key) yields ``{}``. Never raises.
    """
    if not isinstance(event, dict):
        return {}
    block = event.get("contentBlockDelta")
    if not isinstance(block, dict):
        return {}
    delta = block.get("delta")
    return delta if isinstance(delta, dict) else {}


def _has_text_delta(event: Any) -> bool:
    """Whether ``event`` carries assistant text.

    Postconditions:
        - ``True`` for a ``contentBlockDelta`` with a non-empty ``text``
          delta. Never raises on a malformed event.
    """
    return bool(_block_delta(event).get("text"))


def _is_tool_use_delta(event: Any) -> bool:
    """Whether ``event`` is a ``toolUse`` input delta.

    Postconditions:
        - ``True`` for a ``contentBlockDelta`` carrying ``toolUse``. Never
          raises on a malformed event.
    """
    return "toolUse" in _block_delta(event)


class ToolCallBudgetModel:
    """Strands model wrapper that hard-stops a run at ``max_tool_calls``.

    Duck-typed rather than subclassing ``strands.models.Model``: the code
    review passes inject plain test doubles as models, and every attribute
    this wrapper does not define itself is delegated to the wrapped object,
    so it stands in for whatever the caller had (``clone``,
    ``supports_prompt_caching``, ``get_config``, …).

    Preconditions:
        - ``inner`` exposes an async-generator ``stream`` with the Strands
          ``Model.stream`` signature.
        - ``max_tool_calls >= 1``.

    Postconditions:
        - Below the cap: every event of ``inner.stream`` is yielded
          unchanged; ``tool_calls_used`` counts the ``toolUse`` blocks seen.
        - Once the cap is reached mid-turn (an assistant turn may carry a
          parallel batch of ``toolUse`` blocks): each further block in that
          turn is dropped whole, so Strands never executes it, while the
          turn's other content passes through untouched.
        - At/after the cap: exactly one further turn is issued, carrying a
          directive to answer now (``tool_specs`` is forwarded unchanged --
          see ``_final_turn`` for why withdrawing it would break the request
          under Anthropic). Its ``toolUse`` blocks are dropped and a
          ``tool_use`` stop reason is rewritten to ``end_turn``, so the
          Strands event loop cannot recurse. Any other stop reason passes
          through untouched — it already ends the loop, and a terminal one
          such as ``max_tokens`` must keep reaching Strands so its truncation
          handling still fires. A turn that yields no text at all gets one
          synthesized text block so the caller sees an honest "no conclusion"
          answer rather than an empty assistant message.

    Invariants:
        - ``tool_calls_used`` is monotonically non-decreasing and never
          exceeds ``max_tool_calls`` — the cap bounds the tool calls a run
          can execute, not just the turns it can take.
    """

    def __init__(self, inner: Any, max_tool_calls: int, *, label: str = "code_review") -> None:
        if inner is None:
            raise ValueError("inner model is required")
        if max_tool_calls < 1:
            raise ValueError(f"max_tool_calls must be >= 1, got {max_tool_calls}")
        self._inner = inner
        self._max_tool_calls = int(max_tool_calls)
        self._label = label
        self._tool_calls_used = 0
        self._stopped = False

    # -- introspection ---------------------------------------------------

    @property
    def inner(self) -> Any:
        """The wrapped model."""
        return self._inner

    @property
    def max_tool_calls(self) -> int:
        """The per-run hard cap this wrapper enforces."""
        return self._max_tool_calls

    @property
    def tool_calls_used(self) -> int:
        """Tool uses this run has emitted so far."""
        return self._tool_calls_used

    def __getattr__(self, name: str) -> Any:
        """Delegate any attribute this wrapper does not define to ``inner``.

        Only consulted for attributes missing on the wrapper itself, so the
        methods defined below always win. An attribute ``inner`` also lacks
        falls back to ``strands.models.Model``'s own default (e.g. the
        ``stateful`` property Strands reads off every model), so a bare test
        double stands in for a model here exactly as it does when handed to
        ``Agent`` directly.

        Postconditions:
            - Raises ``AttributeError`` only when neither ``inner`` nor
              ``Model`` provides ``name``.
        """
        # ``_inner`` is set in ``__init__``; guard against lookups that run
        # before that (e.g. during unpickling) recursing forever.
        if name == "_inner":
            raise AttributeError(name)
        try:
            return getattr(self._inner, name)
        except AttributeError:
            return self._strands_model_default(name)

    def _strands_model_default(self, name: str) -> Any:
        """``strands.models.Model``'s own default for ``name``, bound to self.

        Postconditions:
            - Evaluates a ``Model`` property against this wrapper (so e.g.
              ``context_window_limit`` reads through ``get_config``), and binds
              a method or other descriptor to this wrapper so calling it passes
              the wrapper as ``self`` -- returning the raw function instead
              would silently consume the caller's first argument as ``self``
              (e.g. ``count_tokens(messages)``). Raises ``AttributeError``
              when ``Model`` has no such attribute.
        """
        attr = inspect.getattr_static(_StrandsModel, name, _MISSING)
        if attr is _MISSING:
            raise AttributeError(name)
        getter = getattr(type(attr), "__get__", None)
        if getter is not None:
            return getter(attr, self, type(self))
        return attr

    # -- strands.models.Model interface ----------------------------------

    def update_config(self, **model_config: Any) -> None:
        """Delegate to ``inner`` when it supports config updates; else no-op."""
        update = getattr(self._inner, "update_config", None)
        if callable(update):
            update(**model_config)

    def get_config(self) -> Any:
        """Delegate to ``inner``'s config, or ``{}`` when it exposes none."""
        get = getattr(self._inner, "get_config", None)
        return get() if callable(get) else {}

    async def structured_output(
        self,
        output_model: type,
        prompt: Any,
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Pass structured-output calls straight through (they use no tools)."""
        async for event in self._inner.structured_output(
            output_model, prompt, system_prompt=system_prompt, **kwargs
        ):
            yield event

    async def stream(
        self,
        messages: Any,
        tool_specs: Optional[List[Any]] = None,
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Stream one turn, enforcing the run's tool-call cap.

        Postconditions:
            - See the class docstring. Never raises on its own behalf; any
              exception comes from ``inner.stream``.
        """
        if self._tool_calls_used < self._max_tool_calls:
            async for event in self._within_budget(
                self._inner.stream(
                    messages, tool_specs=tool_specs, system_prompt=system_prompt, **kwargs
                )
            ):
                yield event
            return

        self._note_cap_reached()
        async for event in self._final_turn(messages, tool_specs, system_prompt, kwargs):
            yield event

    # -- internals -------------------------------------------------------

    def _note_cap_reached(self) -> None:
        """Log the cap being hit, once per run."""
        if self._stopped:
            return
        self._stopped = True
        logger.warning(
            "%s: agent hit the hard tool-call cap (%d calls, %s); dropping every further "
            "tool call and forcing a final answer",
            self._label,
            self._max_tool_calls,
            _CAP_ENV,
        )

    async def _within_budget(
        self, stream: AsyncGenerator[Dict[str, Any], None]
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Forward ``stream``, counting tool uses and dropping over-budget ones.

        A single assistant turn may carry several ``toolUse`` blocks (a
        parallel batch), so the cap has to be enforced block by block, not
        only between turns — otherwise one turn started at 49/50 could still
        execute an arbitrarily large batch.

        Postconditions:
            - Yields every event unchanged until ``tool_calls_used`` reaches
              the cap; from then on, each complete ``toolUse`` block (start,
              input deltas, stop) is dropped so Strands never executes it.
              Non-tool content in the same turn is untouched.
            - ``tool_calls_used`` never exceeds ``max_tool_calls``.
        """
        dropping = False
        counted_block = False
        async for event in stream:
            # A tool use normally opens with ``contentBlockStart``, but Strands
            # also accepts one announced solely in a ``contentBlockDelta``
            # (``streaming.handle_content_block_delta`` fills toolUseId/name
            # from the delta). Both shapes must be counted, or such a model
            # would slip past the cap entirely — and Strands would then re-derive
            # ``stopReason="tool_use"`` from the surviving block, undoing the
            # ``end_turn`` rewrite and restoring the very loop this guards.
            opens_tool_use = _is_tool_use_start(event) or (
                _is_tool_use_delta(event) and not dropping and not counted_block
            )
            if opens_tool_use:
                if self._tool_calls_used >= self._max_tool_calls:
                    self._note_cap_reached()
                    dropping = True
                    counted_block = False
                    continue
                self._tool_calls_used += 1
                dropping = False
                counted_block = True
                yield event
                continue
            if dropping and _is_tool_use_delta(event):
                continue
            if isinstance(event, dict) and "contentBlockStop" in event:
                # The block ends here either way: stop dropping, and let the
                # next tool use (in whichever shape it arrives) count again.
                was_dropping = dropping
                dropping = False
                counted_block = False
                if was_dropping:
                    continue
            yield event

    async def _final_turn(
        self,
        messages: Any,
        tool_specs: Optional[List[Any]],
        system_prompt: Optional[str],
        kwargs: Dict[str, Any],
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Yield one guaranteed-terminal turn.

        ``tool_specs`` is passed through unchanged rather than withdrawn. What
        makes this turn terminal is the wrapper, not the absence of tools:
        every ``toolUse`` block is dropped and a ``tool_use`` stop reason is
        rewritten, so the loop ends whatever the model asks for. Withdrawing
        the tools would additionally break the request itself under Anthropic
        — the conversation still holds the run's ``tool_use``/``tool_result``
        blocks, and ``ClaudeLLMClient`` omits ``tools`` entirely when the spec
        list is empty, which Anthropic rejects ("requests which include
        tool_use or tool_result blocks must define tools"). That 400 would
        replace the graceful final answer with an exception.

        Postconditions:
            - A stop directive is added to ``messages`` (a copy — the caller's
              list is never mutated) and ``tool_specs`` is forwarded as given.
            - ``toolUse`` blocks are dropped (the budget is already spent, so
              ``_within_budget`` drops every one of them) and a
              ``stopReason`` of ``tool_use`` is rewritten to ``end_turn``, so
              Strands cannot recurse into another turn. Every other stop
              reason passes through untouched: they already end the loop, and
              rewriting a terminal one would suppress the handling it exists
              for -- notably ``max_tokens``, which Strands turns into a
              truncation exception that the callers' fail-safes depend on.
              Silently relabelling it would send a truncated answer into the
              formatting pass as if it were complete.
            - At least one text block is emitted.
        """
        emitted_text = False
        saw_stop = False
        async for event in self._within_budget(
            self._inner.stream(
                self._with_directive(messages),
                tool_specs=tool_specs,
                system_prompt=system_prompt,
                **kwargs,
            )
        ):
            if _has_text_delta(event):
                emitted_text = True
            if isinstance(event, dict) and "messageStop" in event:
                saw_stop = True
                if not emitted_text:
                    for fallback in self._fallback_text_events():
                        yield fallback
                    emitted_text = True
                stop = event.get("messageStop")
                if isinstance(stop, dict) and stop.get("stopReason") == "tool_use":
                    # Sibling keys (``metadata`` with usage/latency) ride along
                    # on the same event — rewrite the stop reason, keep the rest.
                    yield {**event, "messageStop": {**stop, "stopReason": "end_turn"}}
                else:
                    yield event
                continue
            yield event
        if not emitted_text:
            for fallback in self._fallback_text_events():
                yield fallback
        if not saw_stop:
            yield {"messageStop": {"stopReason": "end_turn"}}

    @staticmethod
    def _fallback_text_events() -> List[Dict[str, Any]]:
        """Events for the synthesized "no conclusion" answer."""
        return [
            {"contentBlockStart": {"start": {}}},
            {"contentBlockDelta": {"delta": {"text": _NO_OUTPUT_FALLBACK}}},
            {"contentBlockStop": {}},
        ]

    @staticmethod
    def _with_directive(messages: Any) -> Any:
        """``messages`` carrying a trailing user directive to answer now.

        The directive is appended *into* the final user message when there is
        one — the common case, since the turn that spent the budget ends with
        the tool results, which Strands carries as a user message. Adding a
        second user message instead would put two consecutive user turns on
        the wire, which the OpenAI-compatible translator passes straight
        through and Anthropic can reject. Only when the conversation does not
        end in a user message is a new one appended.

        Postconditions:
            - Returns a new list and never mutates ``messages`` or any message
              or content block inside it (the trailing message is copied, not
              edited in place). A non-list ``messages`` (a test double's own
              shape) is returned unchanged rather than coerced.
            - Exactly one copy of the directive is present, as the last text
              the model sees.
        """
        if not isinstance(messages, list):
            return messages
        directive_block = {"text": _BUDGET_DIRECTIVE}
        last = messages[-1] if messages else None
        if isinstance(last, dict) and last.get("role") == "user":
            content = last.get("content")
            if isinstance(content, list):
                merged = {**last, "content": [*content, directive_block]}
                return [*messages[:-1], merged]
        return [*messages, {"role": "user", "content": [directive_block]}]


__all__ = [
    "DEFAULT_AGENT_TOOL_CALL_CAP",
    "ToolCallBudgetModel",
    "resolve_agent_tool_call_cap",
]
