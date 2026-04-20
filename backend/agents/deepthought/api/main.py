"""FastAPI application for the Deepthought recursive agent system."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from deepthought.models import AgentEvent, DeepthoughtRequest, DeepthoughtResponse
from deepthought.orchestrator import DeepthoughtOrchestrator
from deepthought.shared.job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    cancel_job,
    create_job,
    delete_job,
    get_job,
    is_job_cancelled,
    list_jobs,
    update_job,
)
from shared_observability import init_otel, instrument_fastapi_app

logger = logging.getLogger(__name__)

init_otel(service_name="deepthought-team", team_key="deepthought")

app = FastAPI(
    title="Deepthought API",
    description=(
        "Recursive self-organising multi-agent system that dynamically creates "
        "specialist sub-agents to answer complex questions."
    ),
    version="2.0.0",
)
instrument_fastapi_app(app, team_key="deepthought")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


class DeepthoughtJobSubmission(BaseModel):
    job_id: str
    status: str = JOB_STATUS_PENDING


class DeepthoughtJobStatus(BaseModel):
    job_id: str
    status: str
    progress: Optional[int] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class DeepthoughtJobListItem(BaseModel):
    job_id: str
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class DeepthoughtJobListResponse(BaseModel):
    jobs: List[DeepthoughtJobListItem]


def _run_deepthought_background(job_id: str, request: DeepthoughtRequest) -> None:
    try:
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_RUNNING)
        orchestrator = DeepthoughtOrchestrator()
        result = orchestrator.process_message(request)
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump())
    except Exception as e:
        logger.exception("Deepthought job %s failed", job_id)
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))


@app.post("/deepthought/ask", response_model=DeepthoughtJobSubmission)
def ask(request: DeepthoughtRequest) -> DeepthoughtJobSubmission:
    """Submit a question; returns a ``job_id`` to poll for the answer.

    Poll ``GET /deepthought/status/{job_id}`` until ``status`` is
    ``completed``; the full ``DeepthoughtResponse`` (answer, agent tree,
    knowledge entries, event log) lives in the ``result`` field. Clients
    that want live agent events should use ``POST /deepthought/ask/stream``
    (SSE) instead.
    """
    job_id = str(uuid4())
    create_job(job_id, message=request.message)
    thread = threading.Thread(
        target=_run_deepthought_background, args=(job_id, request), daemon=True
    )
    thread.start()
    return DeepthoughtJobSubmission(job_id=job_id, status=JOB_STATUS_PENDING)


@app.get("/deepthought/status/{job_id}", response_model=DeepthoughtJobStatus)
def get_deepthought_status(job_id: str) -> DeepthoughtJobStatus:
    data = get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return DeepthoughtJobStatus(
        job_id=data.get("job_id", job_id),
        status=data.get("status", JOB_STATUS_PENDING),
        progress=data.get("progress"),
        result=data.get("result"),
        error=data.get("error"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


@app.get("/deepthought/jobs", response_model=DeepthoughtJobListResponse)
def list_deepthought_jobs(running_only: bool = False) -> DeepthoughtJobListResponse:
    statuses = [JOB_STATUS_PENDING, JOB_STATUS_RUNNING] if running_only else None
    items = [
        DeepthoughtJobListItem(
            job_id=j.get("job_id", ""),
            status=j.get("status", JOB_STATUS_PENDING),
            created_at=j.get("created_at"),
            updated_at=j.get("updated_at"),
        )
        for j in list_jobs(statuses=statuses)
    ]
    return DeepthoughtJobListResponse(jobs=items)


@app.post("/deepthought/jobs/{job_id}/cancel")
def cancel_deepthought_job(job_id: str) -> Dict[str, Any]:
    data = get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if cancel_job(job_id):
        return {"job_id": job_id, "status": JOB_STATUS_CANCELLED, "success": True}
    return {
        "job_id": job_id,
        "status": data.get("status"),
        "success": False,
        "message": f"Cannot cancel job in status {data.get('status')}",
    }


@app.delete("/deepthought/jobs/{job_id}")
def delete_deepthought_job(job_id: str) -> Dict[str, Any]:
    if get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not delete_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "deleted": True}


@app.post("/deepthought/ask/stream")
async def ask_stream(request: DeepthoughtRequest) -> StreamingResponse:
    """Submit a question and receive SSE events as agents work, then the final result.

    Events are sent as ``text/event-stream`` with types:
    - ``agent_event``: real-time agent activity (spawn, analyse, synthesise, etc.)
    - ``result``: the final ``DeepthoughtResponse`` JSON
    - ``error``: if something goes wrong
    - ``done``: signals the stream is complete
    """
    import asyncio

    event_queue: queue.Queue[AgentEvent | None] = queue.Queue()
    result_holder: list[DeepthoughtResponse | Exception] = []

    def _run() -> None:
        try:
            orchestrator = DeepthoughtOrchestrator()
            original_collect = orchestrator._collect_event

            def _push_event(event: AgentEvent) -> None:
                original_collect(event)
                event_queue.put(event)

            orchestrator._collect_event = _push_event  # type: ignore[assignment]
            resp = orchestrator.process_message(request)
            result_holder.append(resp)
        except Exception as exc:
            result_holder.append(exc)
        finally:
            event_queue.put(None)  # sentinel

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    # How long the stream may go without emitting any bytes before we send
    # a keepalive comment. Must be shorter than any intermediate proxy's
    # read-idle timeout (nginx/cloudflare defaults are ~60s). Overridable via
    # env var for tests and ops; falls back to a safe default on malformed
    # values so one bad env var cannot 500 every streaming request.
    _heartbeat_raw = os.getenv("DEEPTHOUGHT_STREAM_KEEPALIVE_SECONDS", "10")
    try:
        heartbeat_interval = float(_heartbeat_raw)
        if heartbeat_interval <= 0:
            raise ValueError("non-positive heartbeat interval")
    except (TypeError, ValueError):
        logger.warning(
            "Invalid DEEPTHOUGHT_STREAM_KEEPALIVE_SECONDS=%r; using default 10s",
            _heartbeat_raw,
        )
        heartbeat_interval = 10.0

    async def _generate():
        loop = asyncio.get_event_loop()
        # Immediate open-stream marker — guarantees the client sees bytes within
        # ~100ms even if classification + first agent spawn takes a while.
        yield ": stream open\n\n"
        last_byte_time = loop.time()

        while True:
            try:
                event = event_queue.get_nowait()
            except queue.Empty:
                if loop.time() - last_byte_time >= heartbeat_interval:
                    yield ": keepalive\n\n"
                    last_byte_time = loop.time()
                await asyncio.sleep(0.1)
                continue
            if event is None:
                break
            yield f"event: agent_event\ndata: {event.model_dump_json()}\n\n"
            last_byte_time = loop.time()

        # Orchestrator completed. Errors are surfaced here via result_holder —
        # this path also guarantees a trailing `event: done` frame even when the
        # orchestrator raised, because `_run` always queues the sentinel.
        if result_holder and isinstance(result_holder[0], DeepthoughtResponse):
            yield f"event: result\ndata: {result_holder[0].model_dump_json()}\n\n"
        elif result_holder and isinstance(result_holder[0], Exception):
            error_msg = json.dumps({"error": str(result_holder[0])})
            yield f"event: error\ndata: {error_msg}\n\n"

        # Terminate the stream cleanly on the normal completion path. We
        # deliberately do NOT emit this from a finally block: if the client
        # disconnects, Python raises GeneratorExit inside the generator, and
        # yielding during cleanup raises "async generator ignored GeneratorExit".
        # A disconnected client cannot receive `done` anyway, so emitting it
        # only on normal completion is both correct and sufficient.
        yield "event: done\ndata: {}\n\n"
        # No try/finally with yields: yielding during async-generator finalization
        # (e.g. client disconnect triggers aclose()) raises
        # RuntimeError: async generator ignored GeneratorExit. If the client is
        # gone there is nothing to send anyway; orchestrator errors are already
        # handled above via result_holder.

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
