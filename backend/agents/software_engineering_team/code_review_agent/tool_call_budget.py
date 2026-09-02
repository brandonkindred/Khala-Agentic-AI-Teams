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
tool-use block and rewrites a ``tool_use`` stop reason (only that one --
every other stop reason is terminal already, and relabelling one would
suppress the handling it exists for) to ``end_turn`` — so the Strands event
loop terminates no matter what the model emits.

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
from dataclasses import dataclass
from typing import Any, AsyncGenerator

from strands.models.model import Model as _StrandsModel

from software_engineering_team.shared.env_config import env_int

logger = logging.getLogger(__name__)

_MISSING = object()

# Hard stop on total tool calls one Strands agent run may make. Must stay
# above ``false_positive_filter._MAX_TOTAL_TOOL_CALLS`` (the advisory budget
# baked into the tools themselves) so a cooperating model still gets its
# "stop and answer now" nudge and a few turns to act on it; this cap only
# catches the model that ignores it. The ordering is locked by
# ``test_default_hard_cap_exceeds_advisory_tool_budget``.
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
    # Truthiness, not mere presence: Strands' own ``handle_content_block_start``
    # reads ``if "toolUse" in start and start["toolUse"]``, so a falsy
    # ``toolUse`` (``None``/``{}``) is an ordinary content block there. Counting
    # it would charge the budget for a call that never happens, and at the cap
    # would drop a block carrying real assistant text.
    return isinstance(inner, dict) and bool(inner.get("toolUse"))


def _block_delta(event: Any) -> dict[str, Any]:
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


def _block_index(event: Any) -> Any:
    """The ``contentBlockIndex`` an event carries, or ``_MISSING``.

    Postconditions:
        - Returns the index found under whichever block key the event uses;
          ``_MISSING`` when the event carries none. Never raises.
    """
    if not isinstance(event, dict):
        return _MISSING
    for key in ("contentBlockStart", "contentBlockDelta", "contentBlockStop"):
        block = event.get(key)
        if isinstance(block, dict) and "contentBlockIndex" in block:
            return block["contentBlockIndex"]
    return _MISSING


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


def _system_with_directive(system_prompt: str | None) -> str:
    """``system_prompt`` with the budget directive appended.

    Postconditions:
        - Returns a non-empty string: the directive alone when there is no
          system prompt, else the prompt and the directive joined by ``"\\n"``
          -- the same separator Strands' ``split_system_prompt`` uses to
          derive the string form from system content blocks, so
          :func:`_kwargs_with_directive` can keep the two views in step.
        - ``system_prompt`` is used verbatim, never stripped. The adapter
          compares the string form to the ``"\\n"``-join of the content blocks
          for EXACT equality; trimming whitespace here (the review personas
          end with a newline) would break that equality and send the whole
          persona twice on the final turn.
    """
    base = system_prompt or ""
    return f"{base}\n{_BUDGET_DIRECTIVE}" if base else _BUDGET_DIRECTIVE


def _kwargs_with_directive(kwargs: dict[str, Any]) -> dict[str, Any]:
    """``kwargs`` with the directive appended to ``system_prompt_content``.

    A real Strands ``Agent`` passes ``system_prompt`` and
    ``system_prompt_content`` as two views of one value, and the adapter drops
    the string when it is exactly the ``"\\n"``-join of the content blocks
    (``strands_adapter._system_prompt_is_redundant_with_content``). Appending
    the directive to only one view would break that equality and send the
    whole persona twice, so both are extended in step.

    Postconditions:
        - Returns a new dict; ``kwargs``, its content list and its blocks are
          never mutated. When no ``system_prompt_content`` is present, the
          kwargs are returned unchanged (a copy) and the directive rides on
          the system prompt string alone.
    """
    content = kwargs.get("system_prompt_content")
    if not isinstance(content, list) or not content:
        return dict(kwargs)
    return {**kwargs, "system_prompt_content": [*content, {"text": _BUDGET_DIRECTIVE}]}


@dataclass
class _BlockState:
    """What this wrapper has done with one content block of one turn.

    Kept per block (keyed by ``contentBlockIndex``, or one shared entry for a
    stream that omits it) because a turn's blocks can interleave: scalar
    stream-wide flags let one block's events describe another's.

    Invariants:
        - ``counted`` is True once a tool use in this block has been charged
          to the run's budget, so a second announcement of the SAME tool use
          (a ``contentBlockStart`` followed by its input delta) is not charged
          twice.
        - ``dropping`` is True once a tool use in this block was refused for
          being over budget; its input deltas are then skipped too.
        - ``forwarded`` is True once anything from this block has reached
          Strands, which is what makes the block's ``contentBlockStop``
          load-bearing: Strands commits the block's accumulated content there.
    """

    counted: bool = False
    dropping: bool = False
    forwarded: bool = False

    def reset(self) -> None:
        """Clear the record so this key can describe a new block."""
        self.counted = False
        self.dropping = False
        self.forwarded = False


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
        - ``max_tool_calls`` is an ``int`` >= 1. A non-int (``bool`` included)
          raises rather than being coerced -- a truncated cap would not be the
          one the caller asked for.

    Postconditions:
        - Below the cap: every event of ``inner.stream`` is yielded
          unchanged; ``tool_calls_used`` counts the ``toolUse`` blocks seen.
        - Once the cap is reached mid-turn (an assistant turn may carry a
          parallel batch of ``toolUse`` blocks): each further block in that
          turn is dropped whole, so Strands never executes it, while the
          turn's other content passes through untouched.
        - At/after the cap: exactly one further turn is issued, with a
          directive to answer now appended to the system prompt (``messages``
          and ``tool_specs`` are forwarded unchanged -- see ``_final_turn``
          for why a directive message and withdrawn tools would each break
          the request under Anthropic). Its ``toolUse`` blocks are dropped and a
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
        # Validated, never coerced: ``int(2.5)`` would enforce a cap of 2 while
        # the caller believes it asked for 2.5, so the contract would no longer
        # describe what runs. ``bool`` is an ``int`` subclass and is rejected
        # too -- ``True`` as a cap is a caller bug, not a budget of one.
        if isinstance(max_tool_calls, bool) or not isinstance(max_tool_calls, int):
            raise ValueError(f"max_tool_calls must be an int, got {max_tool_calls!r}")
        if max_tool_calls < 1:
            raise ValueError(f"max_tool_calls must be >= 1, got {max_tool_calls}")
        self._inner = inner
        self._max_tool_calls = max_tool_calls
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
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Pass structured-output calls straight through (they use no tools)."""
        async for event in self._inner.structured_output(
            output_model, prompt, system_prompt=system_prompt, **kwargs
        ):
            yield event

    async def stream(
        self,
        messages: Any,
        tool_specs: list[Any] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
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
        self, upstream: AsyncGenerator[dict[str, Any], None]
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Forward ``upstream``, counting tool uses and dropping over-budget ones.

        Named ``upstream`` rather than ``stream``: this class exists to wrap
        ``stream``, so a parameter of that name inside it would shadow the
        method and invite a bare ``stream(...)`` where ``self.stream(...)``
        was meant.

        A single assistant turn may carry several ``toolUse`` blocks (a
        parallel batch), so the cap has to be enforced block by block, not
        only between turns — otherwise one turn started at 49/50 could still
        execute an arbitrarily large batch.

        Postconditions:
            - Yields every event unchanged until ``tool_calls_used`` reaches
              the cap; from then on, each over-budget ``toolUse`` is dropped
              (its opening event and input deltas) so Strands never executes
              it. ``tool_calls_used`` never exceeds ``max_tool_calls``.
            - A block's ``contentBlockStop`` is swallowed only when nothing
              from that block was forwarded. A block that also carried
              content this wrapper passed through -- a model may put text and
              a tool use in ONE block -- keeps its stop, because Strands
              commits a block's accumulated text on ``contentBlockStop``:
              swallowing it there would silently discard the model's answer
              and leave an empty assistant message.
            - Drop state is scoped to the block that opened it: the stop is
              matched by ``contentBlockIndex`` when both carry one, so a
              differently-indexed block's stop can neither end the drop early
              nor be swallowed by it.
        """
        # State is per BLOCK, not per stream. A single set of flags cannot
        # describe a turn whose blocks interleave: an unrelated block's stop
        # would clear the flag recording that a dropped block had already
        # forwarded the model's text, and that block's own stop would then be
        # swallowed -- discarding the answer.
        blocks: dict[Any, _BlockState] = {}

        def _state(event: Any) -> _BlockState:
            key = _block_index(event)
            state = blocks.get(key)
            if state is None:
                state = _BlockState()
                blocks[key] = state
            return state

        async for event in upstream:
            state = _state(event)
            if _is_tool_use_start(event):
                # A fresh block: whatever the previous block with this key did
                # says nothing about this one.
                state.reset()
            # A tool use normally opens with ``contentBlockStart``, but Strands
            # also accepts one announced solely in a ``contentBlockDelta``
            # (``streaming.handle_content_block_delta`` fills toolUseId/name
            # from the delta). Both shapes must be counted, or such a model
            # would slip past the cap entirely -- and Strands would then
            # re-derive ``stopReason="tool_use"`` from the surviving block,
            # undoing the ``end_turn`` rewrite and restoring the very loop this
            # guards.
            opens_tool_use = _is_tool_use_start(event) or (
                _is_tool_use_delta(event) and not state.dropping and not state.counted
            )
            if opens_tool_use:
                if self._tool_calls_used >= self._max_tool_calls:
                    self._note_cap_reached()
                    state.dropping = True
                    continue
                self._tool_calls_used += 1
                state.counted = True
                state.forwarded = True
                yield event
                continue
            if state.dropping and _is_tool_use_delta(event):
                continue
            if isinstance(event, dict) and "contentBlockStop" in event:
                blocks.pop(_block_index(event), None)
                if state.dropping and not state.forwarded:
                    # Nothing of this block reached Strands; its stop would
                    # commit an empty block.
                    continue
                yield event
                continue
            # Anything of this block that reaches Strands makes its
            # ``contentBlockStop`` load-bearing -- a forwarded
            # ``contentBlockStart`` opens a block there that only its stop
            # closes, so swallowing that stop would leave the block open and
            # the next block's content would accumulate under it.
            state.forwarded = True
            yield event

    async def _final_turn(
        self,
        messages: Any,
        tool_specs: list[Any] | None,
        system_prompt: str | None,
        kwargs: dict[str, Any],
    ) -> AsyncGenerator[dict[str, Any], None]:
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

        The stop directive rides on the system prompt, not the messages. The
        turn that spends the budget ends with the tool results, which Strands
        carries as a user message, so any directive message appended after it
        reaches Anthropic as a second consecutive ``user`` turn no matter how
        it is packed: ``_strands_messages_to_openai`` splits a
        ``toolResult``-bearing message into its own ``role="tool"`` message
        and ``_to_anthropic_messages`` flushes that as ``user(tool_result)``,
        leaving ``user(directive)`` behind it. The system prompt cannot
        produce an invalid message shape on any provider.

        Postconditions:
            - ``messages`` and ``tool_specs`` are forwarded exactly as given;
              only the system prompt differs from the caller's.
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
                messages,
                tool_specs=tool_specs,
                system_prompt=_system_with_directive(system_prompt),
                **_kwargs_with_directive(kwargs),
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
    def _fallback_text_events() -> list[dict[str, Any]]:
        """Events for the synthesized "no conclusion" answer."""
        return [
            {"contentBlockStart": {"start": {}}},
            {"contentBlockDelta": {"delta": {"text": _NO_OUTPUT_FALLBACK}}},
            {"contentBlockStop": {}},
        ]


__all__ = [
    "DEFAULT_AGENT_TOOL_CALL_CAP",
    "ToolCallBudgetModel",
    "resolve_agent_tool_call_cap",
]
