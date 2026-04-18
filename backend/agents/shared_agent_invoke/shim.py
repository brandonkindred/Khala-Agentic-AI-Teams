"""FastAPI shim mounted inside each team service.

Usage::

    from shared_agent_invoke import mount_invoke_shim
    mount_invoke_shim(app, team_key="blogging")

Mounts ``POST /_agents/{agent_id}/invoke`` on ``app``.
"""

from __future__ import annotations

import io
import logging
import time
import uuid
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .dispatch import AgentNotRunnableError, invoke_entrypoint

logger = logging.getLogger(__name__)


class InvokeEnvelope(BaseModel):
    """Response shape for ``POST /_agents/{agent_id}/invoke``."""

    output: Any | None = None
    duration_ms: int = 0
    trace_id: str
    logs_tail: list[str] = Field(default_factory=list)
    error: str | None = None


def mount_invoke_shim(app: FastAPI, *, team_key: str) -> None:
    """Attach ``/_agents/{agent_id}/invoke`` to ``app`` for agents belonging to ``team_key``."""

    @app.post(
        "/_agents/{agent_id}/invoke",
        response_model=InvokeEnvelope,
        tags=["agent-console"],
        summary="Invoke a single specialist agent (Agent Console internal).",
    )
    async def _invoke(agent_id: str, request: Request) -> InvokeEnvelope:
        # Lazy import to avoid registry/agents load at team-service startup.
        from agent_registry import get_registry

        manifest = get_registry().get(agent_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
        if manifest.team != team_key:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Agent {agent_id} belongs to team {manifest.team!r}, "
                    f"not this service ({team_key!r})."
                ),
            )
        if "requires-live-integration" in manifest.tags:
            raise HTTPException(
                status_code=409,
                detail=f"Agent {agent_id} requires live integrations and is not runnable in the sandbox.",
            )

        try:
            body: Any = await request.json()
        except Exception:
            body = {}

        trace_id = str(uuid.uuid4())
        logs_tail: list[str] = []
        handler = _InMemoryLogHandler(logs_tail)
        root = logging.getLogger()
        root.addHandler(handler)
        start = time.perf_counter()
        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
        error: str | None = None
        output: Any | None = None
        dispatch_error: AgentNotRunnableError | None = None
        try:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                output = await invoke_entrypoint(manifest.source.entrypoint, body)
        except AgentNotRunnableError as exc:
            # Config / deployment problem — bad entrypoint, missing symbol,
            # non-zero-arg constructor. Defer the raise until after `finally`
            # so logs/stdout are still captured in the envelope.
            logger.exception("agent %s not runnable", agent_id)
            dispatch_error = exc
        except HTTPException:
            raise
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
                logs_tail=logs_tail[-50:],
                error=f"AgentNotRunnable: {dispatch_error}",
            )
            raise HTTPException(status_code=500, detail=envelope.model_dump())

        envelope = InvokeEnvelope(
            output=_jsonable(output),
            duration_ms=duration_ms,
            trace_id=trace_id,
            logs_tail=logs_tail[-50:],
            error=error,
        )
        if error:
            raise HTTPException(status_code=422, detail=envelope.model_dump())
        return envelope


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
