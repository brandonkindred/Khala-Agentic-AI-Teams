"""Application Architect Agent — specialist for system decomposition and API design."""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from agents.prompts import APPLICATION_PROMPT, cached_system_prompt  # noqa: E402
from strands import Agent, tool  # noqa: E402
from tools import document_writer_tool, file_read_tool, web_search_tool  # noqa: E402


@tool
def application_architect(
    spec_summary: str,
    constraints: str = "",
) -> str:
    """Design application architecture: microservices vs monolith, API patterns, tech stack.

    Call this in Phase 1 (parallel with data_architect). Push back on unnecessary
    microservices — prefer modular monolith when distributed services are not justified.

    Args:
        spec_summary: High-level summary of the product/spec requirements.
        constraints: Budget, existing stack, or technology constraints.

    Returns:
        Component/service diagram, API contract stubs, data flow, tech stack recommendation.
    """
    agent = Agent(
        model=_get_sonnet_model(),
        system_prompt=cached_system_prompt(APPLICATION_PROMPT),
        tools=[file_read_tool, web_search_tool, document_writer_tool],
        callback_handler=None,
    )
    context = f"""## Spec Summary
{spec_summary}

## Constraints
{constraints or "None specified"}

Design the application architecture. Produce component diagram, API stubs, data flow, and tech stack recommendation."""
    result = agent(context)
    return str(result)


def _get_sonnet_model() -> str:
    import os

    return os.environ.get("ARCHITECT_MODEL_SPECIALIST", "anthropic.claude-sonnet-4-20250514-v1:0")
