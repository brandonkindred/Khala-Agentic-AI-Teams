"""DevOps Architect Agent — specialist for CI/CD, IaC, deployment strategy, and GitOps."""

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

_PROMPT_PATH = _root / "prompts" / "devops.md"


@tool
def devops_architect(
    spec_summary: str,
    app_output: str,
    infra_output: str,
    security_output: str,
    constraints: str = "",
) -> str:
    """Design DevOps architecture: CI/CD pipelines, IaC, deployment strategies, GitOps.

    Call this in Phase 3 (parallel with cloud_infrastructure_architect and
    data_streaming_architect), after application_architect, data_architect,
    api_design_architect, and security_architect have run.

    Args:
        spec_summary: High-level summary of the product/spec requirements.
        app_output: Output from the Application Architect (components, tech stack).
        infra_output: Output from the Cloud Infrastructure Architect (services, topology).
        security_output: Output from the Security Architect (constraints, compliance).
        constraints: Budget, SLA, existing CI/CD, or toolchain constraints.

    Returns:
        CI/CD architecture, IaC strategy, deployment plan, environment topology.
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

## Infrastructure Output
{infra_output}

## Security Output
{security_output}

## Constraints
{constraints or "None specified"}

Design the DevOps architecture. Produce CI/CD pipeline design, IaC strategy, deployment plan, and environment topology."""
    result = agent(context)
    return str(result)


def _get_sonnet_model() -> str:
    return os.environ.get("ARCHITECT_MODEL_SPECIALIST", "anthropic.claude-sonnet-4-20250514-v1:0")
