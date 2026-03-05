"""Security Architect Agent — specialist for threat modeling and auth design."""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from strands import Agent, tool

from tools import document_writer_tool, file_read_tool, web_search_tool

_PROMPT_PATH = _root / "prompts" / "security.md"


@tool
def security_architect(
    spec_summary: str,
    app_output: str,
    data_output: str,
    constraints: str = "",
) -> str:
    """Design security: STRIDE-lite, OAuth2/OIDC, RBAC, encryption, compliance.

    Call this in Phase 2 (parallel with cloud_infrastructure_architect), after
    application_architect and data_architect have run.

    Args:
        spec_summary: High-level summary of the product/spec requirements.
        app_output: Output from the Application Architect.
        data_output: Output from the Data Architect.
        constraints: Compliance requirements (SOC2, HIPAA, PCI), existing auth.

    Returns:
        Security requirements matrix, auth flow design, encryption decisions, compliance notes.
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

## Data Architecture Output
{data_output}

## Constraints
{constraints or "None specified"}

Design the security architecture. Produce security requirements, auth flow, encryption, and compliance notes."""
    result = agent(context)
    return str(result)


def _get_sonnet_model() -> str:
    import os
    return os.environ.get("ARCHITECT_MODEL_SPECIALIST", "anthropic.claude-sonnet-4-20250514-v1:0")
