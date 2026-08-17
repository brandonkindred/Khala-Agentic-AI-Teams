"""FastAPI shim mounted inside the agent sandbox runtime.

Usage::

    from shared.agent_invoke import mount_invoke_shim
    mount_invoke_shim(app)

Mounts ``POST /_agents/{agent_id}/invoke`` on ``app``. The per-agent
``SANDBOX_AGENT_ID`` guard is enforced by a middleware in
``agent_sandbox_runtime/entrypoint.py``; this shim only resolves the
manifest and dispatches.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import uuid
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .cognition_envelope import (
    CognitionEnvelopeError,
    dump_audit,
    maybe_drive_tool_loop,
    new_tool_audit,
    open_cognition_runtime,
    unwrap_cognition_request,
)
from .dispatch import AgentNotRunnableError, invoke_entrypoint
from .limits import (
    cap_output,
    cap_tool_audit,
    default_exec_timeout_s,
    max_output_bytes,
    max_payload_bytes,
    max_writeback_bytes,
    read_json_capped,
)

logger = logging.getLogger(__name__)


class InvokeEnvelope(BaseModel):
    """Response shape for ``POST /_agents/{agent_id}/invoke``."""

    output: Any | None = None
    duration_ms: int = 0
    trace_id: str
    logs_tail: list[str] = Field(default_factory=list)
    error: str | None = None
    truncated: bool = False
    timeout_hit: bool = False
    # Trusted, out-of-band record the shim-driven tool loop produced: per-call
    # `tool_audit` (ToolCall dumps) + episodic `memory_events` (MemoryEvent dumps),
    # populated in the shim's frame so agent code can't forge or drop them.
    tool_audit: list[dict[str, Any]] = Field(default_factory=list)
    memory_events: list[dict[str, Any]] = Field(default_factory=list)


def mount_invoke_shim(app: FastAPI) -> None:
    """Attach ``/_agents/{agent_id}/invoke`` to ``app``."""

    @app.post(
        "/_agents/{agent_id}/invoke",
        response_model=InvokeEnvelope,
        tags=["agent-console"],
        summary="Invoke a single specialist agent (Agent Console internal).",
    )
    async def _invoke(agent_id: str, request: Request) -> InvokeEnvelope:
        # Lazy import to avoid registry/agents load at sandbox startup.
        from agent_platform.registry import get_registry

        manifest = get_registry().get(agent_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
        if "requires-live-integration" in manifest.tags:
            raise HTTPException(
                status_code=409,
                detail=f"Agent {agent_id} requires live integrations and is not runnable in the sandbox.",
            )

        body: Any = await read_json_capped(request, max_bytes=max_payload_bytes())

        # Unwrap a cognition-wrapped request: the entrypoint receives only its
        # declared `input`; advisory rules + memory digest ride a side channel and
        # the broker's tool calls land in `tool_audit`. An unmarked body (or an
        # image without the cognition package) passes through unchanged.
        try:
            agent_body, cognition_block = unwrap_cognition_request(body)
        except CognitionEnvelopeError as exc:
            raise HTTPException(status_code=400, detail=f"Malformed cognition envelope: {exc}")

        trace_id = str(uuid.uuid4())
        logs_tail: list[str] = []
        handler = _InMemoryLogHandler(logs_tail)
        root = logging.getLogger()
        root.addHandler(handler)
        start = time.perf_counter()
        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
        tool_audit: list[dict[str, Any]] = []
        memory_events: list[dict[str, Any]] = []
        error: str | None = None
        output: Any | None = None
        dispatch_error: AgentNotRunnableError | None = None
        timeout_hit = False
        timeout_s = _resolve_exec_timeout(manifest)
        # A caller-owned audit accumulator: the broker appends to it as the loop
        # runs, so even if the invoke times out (the worker thread can outlive the
        # cancelled await) we can still snapshot the calls that completed.
        audit_obj = new_tool_audit()
        try:
            with ExitStack() as stack:
                stack.enter_context(redirect_stdout(stdout_buf))
                stack.enter_context(redirect_stderr(stderr_buf))
                stack.enter_context(open_cognition_runtime(cognition_block))
                driven = await asyncio.wait_for(
                    _invoke_and_drive(
                        manifest.source.entrypoint,
                        agent_body,
                        agent_id,
                        trace_id,
                        cognition_block,
                        time.monotonic() + timeout_s,
                        audit_obj,
                    ),
                    timeout=timeout_s,
                )
            output = driven["output"]
            tool_audit = driven["tool_calls"]
            memory_events = driven["events"]
            # A mid-flight tool-loop failure surfaces as a 422 while keeping the
            # partial trusted audit of the side effects that already ran.
            if driven["error"]:
                error = driven["error"]
        except AgentNotRunnableError as exc:
            # Config / deployment problem — bad entrypoint, missing symbol,
            # non-zero-arg constructor. Defer the raise until after `finally`
            # so logs/stdout are still captured in the envelope.
            logger.exception("agent %s not runnable", agent_id)
            dispatch_error = exc
        except HTTPException:
            raise
        except asyncio.TimeoutError:
            logger.warning("agent %s exceeded execution timeout (%.1fs)", agent_id, timeout_s)
            error = f"AgentExecutionTimeout: exceeded {timeout_s:.1f}s"
            timeout_hit = True
            # The worker thread may still be finishing — snapshot the audit of the
            # tool calls that completed before the deadline so they aren't lost.
            tool_audit, memory_events = dump_audit(audit_obj)
        except Exception as exc:
            # User-space exception raised by the agent itself — surface it
            # with logs via a 422 so the caller can still render the envelope.
            logger.exception("agent %s raised during invoke", agent_id)
            error = f"{type(exc).__name__}: {exc}"
        finally:
            root.removeHandler(handler)
            for line in stdout_buf.getvalue().splitlines():
                logs_tail.append(f"[stdout] {line}")
            for line in stderr_buf.getvalue().splitlines():
                logs_tail.append(f"[stderr] {line}")

        duration_ms = int((time.perf_counter() - start) * 1000)

        # Bound the cognition control data (tool audit + episodic events) within
        # the writeback budget — independent of the per-field `output` cap — so it
        # can't blow the response size and neither field starves the other. The
        # trusted per-call `tool_audit` is kept first; `events` take the remainder.
        capped_audit, capped_events = _cap_writeback(
            tool_audit, memory_events, max_bytes=max_writeback_bytes()
        )
        # Keep the metadata within the proxy's per-response overhead budget: the
        # last 50 lines, each truncated, so a chatty agent can't push the envelope
        # past output_cap + writeback_cap + RESPONSE_ENVELOPE_OVERHEAD_BYTES.
        bounded_logs = _bounded_logs(logs_tail)

        if dispatch_error is not None:
            # Infrastructure/config failure — must NOT return 200. Clients
            # that rely on status codes (including the unified API proxy's
            # run persistence) treat 5xx as a hard failure, which is what
            # this is. Body still carries the envelope shape so the UI can
            # render the error + captured logs.
            envelope = InvokeEnvelope(
                output=None,
                duration_ms=duration_ms,
                trace_id=trace_id,
                logs_tail=bounded_logs,
                error=f"AgentNotRunnable: {dispatch_error}",
                tool_audit=capped_audit,
                memory_events=capped_events,
            )
            raise HTTPException(status_code=500, detail=envelope.model_dump())

        capped_output, truncated = cap_output(_jsonable(output), max_bytes=max_output_bytes())
        envelope = InvokeEnvelope(
            output=capped_output,
            duration_ms=duration_ms,
            trace_id=trace_id,
            logs_tail=bounded_logs,
            error=error,
            truncated=truncated,
            timeout_hit=timeout_hit,
            tool_audit=capped_audit,
            memory_events=capped_events,
        )
        if timeout_hit:
            raise HTTPException(status_code=504, detail=envelope.model_dump())
        if error:
            raise HTTPException(status_code=422, detail=envelope.model_dump())
        return envelope


async def _invoke_and_drive(
    entrypoint: str,
    body: Any,
    agent_id: str | None,
    source_run_id: str,
    cognition: dict[str, Any] | None,
    deadline: float | None,
    audit: Any,
) -> dict[str, Any]:
    """Invoke the entrypoint, then drive the tool loop if it returned a plan.

    Returns ``maybe_drive_tool_loop``'s dict (``output`` / ``tool_calls`` /
    ``events`` / ``error``). When the entrypoint returns a cognition
    ``ToolLoopPlan``, the brokered loop is driven **here** (off the event loop, in
    a worker thread, since it makes blocking LLM calls) so the trusted audit is
    produced in the shim's frame — never reachable to agent code. ``deadline``
    (a ``time.monotonic()`` instant) bounds handler side effects: the worker thread
    can outlive a cancelled await, so the broker stops dispatching past it.
    ``audit`` is the caller-owned accumulator the shim can snapshot on timeout.
    """
    result = await invoke_entrypoint(entrypoint, body, agent_id=agent_id)
    return await asyncio.to_thread(
        maybe_drive_tool_loop,
        result,
        agent_id=agent_id,
        source_run_id=source_run_id,
        cognition=cognition,
        deadline=deadline,
        audit=audit,
    )


def _cap_writeback(
    tool_audit: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    max_bytes: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bound the combined cognition writeback (audit + events) to ``max_bytes``.

    The per-call ``tool_audit`` (the trusted record) is capped first and kept;
    ``events`` get whatever budget remains, so the two together never exceed the
    writeback budget the proxy reserves.
    """
    capped_audit, _ = cap_tool_audit(tool_audit, max_bytes=max_bytes)
    remaining = max(0, max_bytes - len(json.dumps(capped_audit, default=str)))
    capped_events, _ = cap_tool_audit(events, max_bytes=remaining)
    return capped_audit, capped_events


_MAX_LOG_LINES = 50
_MAX_LOG_LINE_CHARS = 1000


def _bounded_logs(lines: list[str]) -> list[str]:
    """Return the last ``_MAX_LOG_LINES`` lines, each truncated to a max length.

    Bounds ``logs_tail`` to a deterministic size so the response envelope's
    metadata stays within the proxy's per-response overhead budget regardless of
    how chatty the agent is.
    """
    out: list[str] = []
    for line in lines[-_MAX_LOG_LINES:]:
        out.append(line if len(line) <= _MAX_LOG_LINE_CHARS else line[:_MAX_LOG_LINE_CHARS] + "…")
    return out


def _resolve_exec_timeout(manifest: Any) -> float:
    """Per-agent timeout override from ``manifest.invoke.timeout_seconds`` if set."""
    invoke = getattr(manifest, "invoke", None)
    if invoke is not None:
        override = getattr(invoke, "timeout_seconds", None)
        if override is not None and override > 0:
            return float(override)
    return default_exec_timeout_s()


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of a Pydantic or plain object to JSON-ready data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    # Fallback — stringify.
    return repr(value)


class _InMemoryLogHandler(logging.Handler):
    """Append formatted log records to a caller-owned list."""

    def __init__(self, sink: list[str]) -> None:
        super().__init__(level=logging.INFO)
        self._sink = sink
        self.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink.append(self.format(record))
        except Exception:
            # Never let log capture crash the invoke path.
            pass
