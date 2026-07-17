"""Data Architect Agent — specialist for data store selection and modeling."""

from __future__ import annotations

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

_PROMPT_PATH = _root / "prompts" / "data.md"


@tool
def data_architect(
    spec_summary: str,
    constraints: str = "",
) -> str:
    """Design data architecture: store selection, modeling, ETL, backup, multi-tenancy.

    Call this in Phase 1 (parallel with application_architect).

    Args:
        spec_summary: High-level summary of the product/spec requirements.
        constraints: Budget, compliance, or existing data constraints.

    Returns:
        Data store recommendations, high-level data model, pipeline architecture.
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

## Constraints
{constraints or "None specified"}

Design the data architecture. Produce data store recommendations, data model, and pipeline architecture."""
    result = agent(context)
    return str(result)


def _get_sonnet_model() -> str:
    import os

    return os.environ.get("ARCHITECT_MODEL_SPECIALIST", "anthropic.claude-sonnet-4-20250514-v1:0")
