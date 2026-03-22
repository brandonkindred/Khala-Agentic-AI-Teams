"""Cloud Infrastructure Architect Agent — specialist for AWS/infrastructure design."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure architect-agents root is on path for tools import
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

_PROMPT_PATH = _root / "prompts" / "cloud_infra.md"


@tool
def cloud_infrastructure_architect(
    spec_summary: str,
    app_output: str,
    data_output: str,
    constraints: str = "",
) -> str:
    """Design AWS infrastructure: service selection, HA/DR, VPC, IAM, cost optimization.

    Call this AFTER application_architect and data_architect have run. Use their
    outputs to inform infrastructure choices (e.g., app components determine
    compute needs; data model determines database selection).

    Args:
        spec_summary: High-level summary of the product/spec requirements.
        app_output: Output from the Application Architect (components, APIs, tech stack).
        data_output: Output from the Data Architect (data stores, models, pipelines).
        constraints: Budget, SLA, compliance, or existing stack constraints.

    Returns:
        Infrastructure component list, network topology, and cost breakdown.
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

## Constraints
{constraints or "None specified"}

Design the cloud infrastructure for this system. Produce infrastructure components, network topology, and cost estimate."""
    result = agent(context)
    return str(result)


def _get_sonnet_model() -> str:
    import os
    return os.environ.get("ARCHITECT_MODEL_SPECIALIST", "anthropic.claude-sonnet-4-20250514-v1:0")
