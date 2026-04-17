"""FastAPI application for the Deepthought recursive agent system."""

from __future__ import annotations

import json
import logging
import queue
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from deepthought.models import AgentEvent, DeepthoughtRequest, DeepthoughtResponse
from deepthought.orchestrator import DeepthoughtOrchestrator
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


@app.post("/deepthought/ask", response_model=DeepthoughtResponse)
def ask(request: DeepthoughtRequest) -> DeepthoughtResponse:
    """Submit a question and receive a recursively-decomposed answer.

    The response includes the synthesised answer, the full agent
    decomposition tree, knowledge base entries, and event log.
    """
    orchestrator = DeepthoughtOrchestrator()
    return orchestrator.process_message(request)


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
    # read-idle timeout (nginx/cloudflare defaults are ~60s).
    HEARTBEAT_INTERVAL = 10.0

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
                if loop.time() - last_byte_time >= HEARTBEAT_INTERVAL:
                    yield ": keepalive\n\n"
                    last_byte_time = loop.time()
                await asyncio.sleep(0.1)
                continue
            if event is None:
                break
            yield f"event: agent_event\ndata: {event.model_dump_json()}\n\n"
            last_byte_time = loop.time()

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

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
