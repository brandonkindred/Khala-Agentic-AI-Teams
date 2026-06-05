"""FastAPI shim mounted inside the agent sandbox runtime.

Usage::

    from shared_agent_invoke import mount_invoke_shim
    mount_invoke_shim(app)

Mounts ``POST /_agents/{agent_id}/invoke`` on ``app``. The per-agent
``SANDBOX_AGENT_ID`` guard is enforced by a middleware in
``agent_sandbox_runtime/entrypoint.py``; this shim only resolves the
manifest and dispatches.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import uuid
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .cognition_envelope import (
    CognitionEnvelopeError,
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
    # Trusted, out-of-band record of every tool call the cognition broker
    # actually dispatched — populated even when the agent's writeback is dropped.
    tool_audit: list[dict[str, Any]] = Field(default_factory=list)


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
        from agent_registry import get_registry

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
        error: str | None = None
        output: Any | None = None
        dispatch_error: AgentNotRunnableError | None = None
        timeout_hit = False
        timeout_s = _resolve_exec_timeout(manifest)
        try:
            with ExitStack() as stack:
                stack.enter_context(redirect_stdout(stdout_buf))
                stack.enter_context(redirect_stderr(stderr_buf))
                stack.enter_context(open_cognition_runtime(cognition_block, tool_audit))
                output = await asyncio.wait_for(
                    invoke_entrypoint(manifest.source.entrypoint, agent_body),
                    timeout=timeout_s,
                )
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

        # Bound the trusted audit on its own budget — independent of the per-field
        # `output` cap — so a large audit can't blow the response size and neither
        # field starves the other.
        capped_audit, _ = cap_tool_audit(tool_audit, max_bytes=max_writeback_bytes())
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
        )
        if timeout_hit:
            raise HTTPException(status_code=504, detail=envelope.model_dump())
        if error:
            raise HTTPException(status_code=422, detail=envelope.model_dump())
        return envelope


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
