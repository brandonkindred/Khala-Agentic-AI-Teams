"""FastAPI application for the Deepthought recursive agent system."""

from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Generator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from deepthought.models import AgentEvent, DeepthoughtRequest, DeepthoughtResponse
from deepthought.orchestrator import DeepthoughtOrchestrator

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Deepthought API",
    description=(
        "Recursive self-organising multi-agent system that dynamically creates "
        "specialist sub-agents to answer complex questions."
    ),
    version="2.0.0",
)

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


@app.post("/deepthought/ask", response_model=DeepthoughtResponse)
def ask(request: DeepthoughtRequest) -> DeepthoughtResponse:
    """Submit a question and receive a recursively-decomposed answer.

    The response includes the synthesised answer, the full agent
    decomposition tree, knowledge base entries, and event log.
    """
    orchestrator = DeepthoughtOrchestrator()
    return orchestrator.process_message(request)


@app.post("/deepthought/ask/stream")
def ask_stream(request: DeepthoughtRequest) -> StreamingResponse:
    """Submit a question and receive SSE events as agents work, then the final result.

    Events are sent as ``text/event-stream`` with types:
    - ``agent_event``: real-time agent activity (spawn, analyse, synthesise, etc.)
    - ``result``: the final ``DeepthoughtResponse`` JSON
    - ``error``: if something goes wrong
    - ``done``: signals the stream is complete
    """
    event_queue: queue.Queue[AgentEvent | None] = queue.Queue()
    result_holder: list[DeepthoughtResponse | Exception] = []

    def _run() -> None:
        try:
            orchestrator = DeepthoughtOrchestrator()
            # Override the event collector to push to our queue
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

    def _generate() -> Generator[str, None, None]:
        while True:
            event = event_queue.get()
            if event is None:
                break
            yield f"event: agent_event\ndata: {event.model_dump_json()}\n\n"

        if result_holder and isinstance(result_holder[0], DeepthoughtResponse):
            yield f"event: result\ndata: {result_holder[0].model_dump_json()}\n\n"
        elif result_holder and isinstance(result_holder[0], Exception):
            error_msg = json.dumps({"error": str(result_holder[0])})
            yield f"event: error\ndata: {error_msg}\n\n"

        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
