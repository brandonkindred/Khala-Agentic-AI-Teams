"""Brokered tool loop — execute an agent's tools and log them to memory.

Wraps :func:`llm_service.tool_loop.complete_json_with_tool_loop` with a *broker*
around every declared handler. Before dispatching each call the broker:

1. **Gates the call pre-dispatch** against the active enforced ``forbid_tool``
   predicates (:func:`agent_cognition.rules.enforcement.evaluate_tool_call`),
   evaluating against the **declared tool id** (e.g. ``git``) the manifest and
   predicates are written against — not the advertised function name (e.g.
   ``git_status``) — so ``forbid_tool: git`` blocks every git function. A
   forbidden call is refused **before the handler runs**, so its side effect never
   happens — a postcondition on the final output would be too late.
2. **Logs the call to memory** as ``tool_call`` + ``outcome``/``error``
   :class:`~agent_cognition.models.MemoryEvent` records, and emits a trusted,
   out-of-band :class:`~agent_cognition.models.ToolCall` audit entry (to the
   returned :class:`ToolAudit` *and* the shim's audit sink, see
   :mod:`agent_cognition.tools.channel`). Because the broker — not the model's
   self-reported writeback — produces the audit, it stays honest even when the
   writeback is dropped (e.g. a blocked run).

The broker wraps both ``sandbox_local`` handlers (the v1 default, loop runs in the
sandbox) and ``platform_bound`` handlers (the proxy drives the loop so secrets and
egress stay platform-side — :func:`drive_platform_bound_loop`). Secrets used inside
a platform handler never leave it: only the handler's *returned* result is sent
back to the model, and the broker sanitizes args/results before recording them.

Design by Contract:

* :func:`run_tool_loop` — Preconditions: ``agent_id`` and ``source_run_id`` are
  non-empty; ``toolset``/``enforced_rules`` are the bound tools and the agent's
  active enforced rules. Postconditions: returns the loop's final structured
  output and a :class:`ToolAudit` containing one ``tool_call`` event + one
  terminal (``outcome``/``error``) event and exactly one ``ToolCall`` per brokered
  call; a ``forbid_tool``-blocked call contributes a blocked ``tool_call`` event +
  ``ToolCall`` and **no** handler side effect. Event ``source_seq`` values are
  contiguous from ``source_seq_start`` so the later writeback dedups idempotently.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from agent_cognition.models import EventKind, MemoryEvent, Rule, ToolCall
from agent_cognition.rules.enforcement import evaluate_tool_call
from agent_cognition.tools.binding import BoundToolset, ExecutionSite
from agent_cognition.tools.channel import _record_brokered, _recording_window
from llm_service.tool_loop import complete_json_with_tool_loop

__all__ = [
    "ToolAudit",
    "run_tool_loop",
    "drive_platform_bound_loop",
]

# Substring denylist for sanitizing args/results before they touch memory. Tool
# secrets ride env / secure stores, never the episodic log (DESIGN §11 Secrets).
_SECRET_KEY_HINTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "credential",
    "private_key",
)
_MAX_STR = 512
_MAX_DEPTH = 4
_MAX_ITEMS = 50

# Salience weights (bounded to [0, 1]) — errors and refusals are the most worth
# remembering; routine call/outcome pairs are low-salience.
_SALIENCE_INTENT = 0.3
_SALIENCE_OUTCOME = 0.3
_SALIENCE_ERROR = 0.7
_SALIENCE_BLOCKED = 0.6


@dataclass
class ToolAudit:
    """The trusted record of what the broker actually executed for one loop.

    ``events`` are the episodic :class:`MemoryEvent` rows; ``tool_calls`` is the
    one-per-call summary the writeback reconciles against.
    """

    events: list[MemoryEvent] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)


def run_tool_loop(
    llm: Any,
    *,
    agent_id: str,
    source_run_id: str,
    user_prompt: str,
    system_prompt: str,
    toolset: BoundToolset,
    enforced_rules: list[Rule],
    source_seq_start: int = 0,
    max_rounds: int = 16,
    temperature: float = 0.2,
    think: bool = False,
    clock: Callable[[], datetime] = None,  # type: ignore[assignment]
) -> tuple[dict[str, Any], ToolAudit]:
    """Run the brokered tool loop and return ``(final_output, audit)``.

    See module docstring for the full contract. ``clock`` is injectable for
    deterministic tests; it defaults to UTC ``now``.
    """
    assert agent_id, "run_tool_loop: agent_id must be non-empty"
    assert source_run_id, "run_tool_loop: source_run_id must be non-empty"
    assert source_seq_start >= 0, "run_tool_loop: source_seq_start must be non-negative"
    tick = clock or _utcnow
    audit = ToolAudit()
    counter = _SeqCounter(source_seq_start)
    # Broker per *function*, but bound to its owning *declared* tool id so the
    # enforced gate matches `forbid_tool: <tool_id>` (e.g. `git`) rather than the
    # advertised function name (e.g. `git_status`). bind_tools guarantees function
    # names are unique across tools, so this mapping is unambiguous.
    brokered: dict[str, Callable[[dict[str, Any]], Any]] = {}
    for tool in toolset.tools:
        for fn_name, handler in tool.handlers.items():
            brokered[fn_name] = _make_broker(
                fn_name,
                handler,
                tool_id=tool.tool_id,
                agent_id=agent_id,
                source_run_id=source_run_id,
                enforced_rules=enforced_rules,
                audit=audit,
                counter=counter,
                clock=tick,
            )
    result = complete_json_with_tool_loop(
        llm,
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        tools=toolset.definitions(),
        tool_handlers=brokered,
        max_rounds=max_rounds,
        temperature=temperature,
        think=think,
    )
    return result, audit


def drive_platform_bound_loop(
    runtime: Any,
    *,
    agent_id: str,
    source_run_id: str,
    user_prompt: str,
    system_prompt: str,
    toolset: BoundToolset,
    enforced_rules: list[Rule],
    **kwargs: Any,
) -> tuple[dict[str, Any], ToolAudit]:
    """Proxy-driven loop for ``platform_bound`` tools (secrets stay platform-side).

    ``runtime`` plays the sandbox's role over the SB↔PX ``tool_calls`` /
    ``tool_results`` protocol — which is exactly the chat tool protocol
    :func:`complete_json_with_tool_loop` speaks (assistant ``tool_calls`` →
    ``tool`` result messages). The handlers (holding any secrets) run **here**, on
    the platform; only their returned results cross back to ``runtime``. The
    multi-turn runtime that lets a *generated* sandboxed agent pause/resume ships
    in Step 14; this entry point lands the protocol and is exercised with a stubbed
    runtime.

    Precondition: every tool in ``toolset`` is sited ``platform_bound``.
    """
    for tool in toolset.tools:
        assert tool.site is ExecutionSite.PLATFORM_BOUND, (
            f"drive_platform_bound_loop: tool '{tool.tool_id}' is {tool.site.value}, "
            "not platform_bound"
        )
    return run_tool_loop(
        runtime,
        agent_id=agent_id,
        source_run_id=source_run_id,
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        toolset=toolset,
        enforced_rules=enforced_rules,
        **kwargs,
    )


class _SeqCounter:
    """Monotonic ``source_seq`` allocator shared across a loop's events."""

    def __init__(self, start: int) -> None:
        self._next = start

    def take(self) -> int:
        seq = self._next
        self._next += 1
        return seq


def _make_broker(
    name: str,
    handler: Callable[[dict[str, Any]], Any],
    *,
    tool_id: str,
    agent_id: str,
    source_run_id: str,
    enforced_rules: list[Rule],
    audit: ToolAudit,
    counter: _SeqCounter,
    clock: Callable[[], datetime],
) -> Callable[[dict[str, Any]], Any]:
    """Wrap one handler with the gate + memory-logging broker.

    ``name`` is the advertised function (e.g. ``git_status``); ``tool_id`` is the
    owning declared tool (e.g. ``git``). The enforced gate evaluates against
    ``tool_id`` — the identifier predicates and the manifest are written against —
    while the function name is preserved in the memory record for provenance.
    """

    def _run(args: dict[str, Any]) -> Any:
        started = clock()
        safe_args = _sanitize(args)
        # Gate on the DECLARED tool id, not the function name, so a rule like
        # `forbid_tool: git` blocks every git function (git_status, git_commit, …).
        allow, reason = evaluate_tool_call(tool_id, args or {}, enforced_rules)
        if not allow:
            # Refuse BEFORE the handler runs — no side effect. Record a blocked
            # tool_call (trusted) and hand the model a structured refusal.
            blocked = ToolCall(
                tool_id=tool_id, args=safe_args, ok=False, error=reason, occurred_at=started
            )
            _emit_event(
                audit,
                counter,
                agent_id=agent_id,
                source_run_id=source_run_id,
                kind=EventKind.TOOL_CALL,
                content=name,
                data={"tool_id": tool_id, "blocked": True, "reason": reason, "args": safe_args},
                salience=_SALIENCE_BLOCKED,
                occurred_at=started,
            )
            _finish_call(audit, blocked)
            return {
                "success": False,
                "error": "forbidden_by_rule",
                "message": reason,
                "tool_id": tool_id,
                "function": name,
            }

        _emit_event(
            audit,
            counter,
            agent_id=agent_id,
            source_run_id=source_run_id,
            kind=EventKind.TOOL_CALL,
            content=name,
            data={"tool_id": tool_id, "args": safe_args},
            salience=_SALIENCE_INTENT,
            occurred_at=started,
        )
        try:
            result = handler(args)
        except Exception as exc:  # handler raised — record an error event, keep looping
            err = ToolCall(
                tool_id=tool_id, args=safe_args, ok=False, error=str(exc), occurred_at=started
            )
            _emit_event(
                audit,
                counter,
                agent_id=agent_id,
                source_run_id=source_run_id,
                kind=EventKind.ERROR,
                content=name,
                data={"tool_id": tool_id, "error": str(exc)},
                salience=_SALIENCE_ERROR,
                occurred_at=clock(),
            )
            _finish_call(audit, err)
            return {
                "success": False,
                "error": "handler_exception",
                "message": str(exc),
                "tool_id": tool_id,
                "function": name,
            }

        ok = _result_ok(result)
        call = ToolCall(
            tool_id=tool_id,
            args=safe_args,
            ok=ok,
            result=_sanitize(result),
            occurred_at=started,
        )
        _emit_event(
            audit,
            counter,
            agent_id=agent_id,
            source_run_id=source_run_id,
            kind=EventKind.OUTCOME if ok else EventKind.ERROR,
            content=name,
            data={"tool_id": tool_id, "ok": ok, "result": _sanitize(result)},
            salience=_SALIENCE_OUTCOME if ok else _SALIENCE_ERROR,
            occurred_at=clock(),
        )
        _finish_call(audit, call)
        return result

    return _run


def _emit_event(
    audit: ToolAudit,
    counter: _SeqCounter,
    *,
    agent_id: str,
    source_run_id: str,
    kind: EventKind,
    content: str,
    data: dict[str, Any],
    salience: float,
    occurred_at: datetime,
) -> None:
    audit.events.append(
        MemoryEvent(
            id=str(uuid4()),
            agent_id=agent_id,
            kind=kind,
            content=content,
            data=data,
            salience=salience,
            occurred_at=occurred_at,
            source_run_id=source_run_id,
            source_seq=counter.take(),
        )
    )


def _finish_call(audit: ToolAudit, call: ToolCall) -> None:
    """Append the per-call summary and mirror it to the trusted audit sink.

    The sink write is wrapped in :func:`_recording_window` — the only sanctioned
    way to populate the trusted audit — so agent code cannot forge entries
    through the channel (the writer is module-private to the runner↔channel pair).
    """
    audit.tool_calls.append(call)
    with _recording_window():
        _record_brokered(call.model_dump(mode="json"))


def _result_ok(result: Any) -> bool:
    """A handler result is a failure only if it explicitly says so.

    Matches the existing tool conventions (``{"success": False, ...}``); anything
    else — including a plain value — is treated as a success.
    """
    if isinstance(result, Mapping) and result.get("success") is False:
        return False
    return True


def _sanitize(value: Any, *, _depth: int = 0) -> Any:
    """Redact secret-like keys and bound size before a value touches memory."""
    if _depth >= _MAX_DEPTH:
        return "<truncated:depth>"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:_MAX_ITEMS]:
            key_s = str(key)
            if _is_secret_key(key_s):
                out[key_s] = "***"
            else:
                out[key_s] = _sanitize(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, _depth=_depth + 1) for item in list(value)[:_MAX_ITEMS]]
    if isinstance(value, str) and len(value) > _MAX_STR:
        return value[:_MAX_STR] + "…<truncated>"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Unknown object — stringify defensively (never let logging crash the loop).
    text = repr(value)
    return text[:_MAX_STR] + "…<truncated>" if len(text) > _MAX_STR else text


def _is_secret_key(key: str) -> bool:
    low = key.lower()
    return any(hint in low for hint in _SECRET_KEY_HINTS)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)
