"""FastAPI application for the Deepthought recursive agent system."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from deepthought.models import DeepthoughtRequest, DeepthoughtResponse
from deepthought.orchestrator import DeepthoughtOrchestrator

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Deepthought API",
    description=(
        "Recursive self-organising multi-agent system that dynamically creates "
        "specialist sub-agents to answer complex questions."
    ),
    version="1.0.0",
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

    The response includes the synthesised answer and the full agent
    decomposition tree showing which specialists were consulted.
    """
    orchestrator = DeepthoughtOrchestrator()
    return orchestrator.process_message(request)
