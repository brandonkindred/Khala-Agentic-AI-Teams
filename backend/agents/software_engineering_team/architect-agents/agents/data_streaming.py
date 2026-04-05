"""Data Streaming Architect Agent — specialist for event-driven architecture and real-time pipelines."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from strands import Agent, tool  # noqa: E402
from tools import (  # noqa: E402
    aws_pricing_tool,
    document_writer_tool,
    file_read_tool,
    web_search_tool,
)

_PROMPT_PATH = _root / "prompts" / "data_streaming.md"


@tool
def data_streaming_architect(
    spec_summary: str,
    app_output: str,
    data_output: str,
    api_output: str,
    constraints: str = "",
) -> str:
    """Design data streaming architecture: event-driven patterns, message brokers, real-time pipelines, CDC.

    Call this in Phase 3 (parallel with cloud_infrastructure_architect and
    devops_architect), after application_architect, data_architect,
    api_design_architect, and security_architect have run.

    Args:
        spec_summary: High-level summary of the product/spec requirements.
        app_output: Output from the Application Architect (components, data flow).
        data_output: Output from the Data Architect (stores, models, pipelines).
        api_output: Output from the API Design Architect (contracts, gateway).
        constraints: Budget, latency SLAs, throughput requirements, existing streaming infra.

    Returns:
        Streaming topology, broker selection, event schema design, processing pipeline architecture.
    """
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    agent = Agent(
        model=_get_sonnet_model(),
        system_prompt=prompt,
        tools=[file_read_tool, aws_pricing_tool, web_search_tool, document_writer_tool],
        callback_handler=None,
    )
    context = f"""## Spec Summary
{spec_summary}

## Application Architecture Output
{app_output}

## Data Architecture Output
{data_output}

## API Architecture Output
{api_output}

## Constraints
{constraints or "None specified"}

Design the data streaming architecture. Produce streaming topology, broker selection, event schemas, and processing pipeline design."""
    result = agent(context)
    return str(result)


def _get_sonnet_model() -> str:
    return os.environ.get("ARCHITECT_MODEL_SPECIALIST", "anthropic.claude-sonnet-4-20250514-v1:0")
