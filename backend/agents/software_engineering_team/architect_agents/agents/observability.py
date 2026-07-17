"""Observability Architect Agent — specialist for logging, metrics, tracing, SLOs."""

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

_PROMPT_PATH = _root / "prompts" / "observability.md"


@tool
def observability_architect(
    spec_summary: str,
    app_output: str,
    data_output: str,
    infra_output: str,
    security_output: str,
) -> str:
    """Design observability: logging, metrics, tracing, dashboards, SLOs, cost.

    Call this in Phase 3 (after all other specialists). Consider the cost of
    observability — log volume and metric cardinality can drive significant costs.

    Args:
        spec_summary: High-level summary of the product/spec requirements.
        app_output: Output from the Application Architect.
        data_output: Output from the Data Architect.
        infra_output: Output from the Cloud Infrastructure Architect.
        security_output: Output from the Security Architect.

    Returns:
        Observability stack recommendation, alert runbook stubs, SLO targets.
    """
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    agent = Agent(
        model=_get_haiku_model(),
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

## Infrastructure Output
{infra_output}

## Security Output
{security_output}

Design the observability architecture. Produce stack recommendation, runbooks, and SLO targets. Consider cost of observability."""
    result = agent(context)
    return str(result)


def _get_haiku_model() -> str:
    return os.environ.get(
        "ARCHITECT_MODEL_OBSERVABILITY",
        "anthropic.claude-haiku-4-5-20251001-v1:0",
    )
