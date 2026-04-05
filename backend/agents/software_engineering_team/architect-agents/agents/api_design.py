"""API Design Architect Agent — specialist for API patterns, gateway design, and contract-first development."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from strands import Agent, tool  # noqa: E402
from tools import document_writer_tool, file_read_tool, web_search_tool  # noqa: E402

_PROMPT_PATH = _root / "prompts" / "api_design.md"


@tool
def api_design_architect(
    spec_summary: str,
    app_output: str,
    security_output: str,
    constraints: str = "",
) -> str:
    """Design API architecture: REST/GraphQL/gRPC selection, gateway patterns, versioning, rate limiting.

    Call this in Phase 2 (parallel with application_architect and data_architect),
    after security_architect has run in Phase 1.

    Args:
        spec_summary: High-level summary of the product/spec requirements.
        app_output: Output from the Application Architect (components, tech stack).
        security_output: Output from the Security Architect (auth requirements, constraints).
        constraints: Existing API standards, backward compatibility, or client constraints.

    Returns:
        API contracts/stubs, gateway topology, auth flow, versioning strategy.
    """
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    agent = Agent(
        model=_get_sonnet_model(),
        system_prompt=prompt,
        tools=[file_read_tool, web_search_tool, document_writer_tool],
        callback_handler=None,
    )
    context = f"""## Spec Summary
{spec_summary}

## Application Architecture Output
{app_output}

## Security Output
{security_output}

## Constraints
{constraints or "None specified"}

Design the API architecture. Produce API style selection, contracts, gateway topology, versioning strategy, and rate limiting design."""
    result = agent(context)
    return str(result)


def _get_sonnet_model() -> str:
    return os.environ.get("ARCHITECT_MODEL_SPECIALIST", "anthropic.claude-sonnet-4-20250514-v1:0")
