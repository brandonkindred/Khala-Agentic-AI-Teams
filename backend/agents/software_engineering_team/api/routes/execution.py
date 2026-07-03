"""SE team API — execution telemetry routes: task snapshot and the SSE event stream."""

import json
import logging
import time
from typing import Any, Dict

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from software_engineering_team.shared.execution_tracker import execution_tracker

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/execution/tasks")
def get_execution_tasks() -> Dict[str, Any]:
    """Get task status, plan progress, loop metrics, and timing metrics."""
    return execution_tracker.snapshot()


@router.get("/execution/stream")
def stream_execution_events() -> StreamingResponse:
    """SSE endpoint for execution updates."""

    def event_generator():  # pragma: no cover  # integration-only: long-lived SSE generator with time.sleep
        index = 0
        for _ in range(300):
            # Thread back the returned next_index (a total-emitted position): once the
            # tracker's bounded buffer wraps it diverges from a naive len()-based
            # counter, which would re-emit already-sent events.
            events, index = execution_tracker.events_since(index)
            if events:
                for event in events:
                    yield f"data: {json.dumps(event)}\n\n"
            else:
                yield ": keepalive\n\n"
            time.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
