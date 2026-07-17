"""Security Architect Agent — specialist for threat modeling and auth design."""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from strands import Agent, tool  # noqa: E402
from tools import document_writer_tool, file_read_tool, web_search_tool  # noqa: E402

_PROMPT_PATH = _root / "prompts" / "security.md"


@tool
def security_architect(
    spec_summary: str,
    app_output: str = "",
    data_output: str = "",
    constraints: str = "",
    all_specialist_outputs: str = "",
    mode: str = "initial",
) -> str:
    """Design security architecture: STRIDE, OAuth2/OIDC, RBAC, encryption, compliance, threat modeling.

    This specialist runs in TWO modes:
    - **Phase 1 (mode='initial'):** Runs FIRST before all other specialists. Produces
      security constraints that are mandatory for all subsequent phases.
    - **Phase 5 (mode='final_gate'):** Runs LAST after all specialists and scrutineer.
      Reviews all outputs and either approves or vetoes the architecture.

    Args:
        spec_summary: High-level summary of the product/spec requirements.
        app_output: Output from the Application Architect (empty in Phase 1).
        data_output: Output from the Data Architect (empty in Phase 1).
        constraints: Compliance requirements (SOC2, HIPAA, PCI, GDPR), existing auth.
        all_specialist_outputs: Concatenated outputs from ALL specialists (Phase 5 only).
        mode: 'initial' for Phase 1 threat assessment, 'final_gate' for Phase 5 review.

    Returns:
        Phase 1: Security constraints, threat model, compliance checklist, auth recommendation.
        Phase 5: Security review, APPROVE/VETO decision, unresolved issues.
    """
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    agent = Agent(
        model=_get_sonnet_model(),
        system_prompt=prompt,
        tools=[file_read_tool, web_search_tool, document_writer_tool],
        callback_handler=None,
    )
    if mode == "final_gate":
        context = f"""## Mode: FINAL SECURITY GATE (Phase 5)

## Spec Summary
{spec_summary}

## All Specialist Architecture Outputs
{all_specialist_outputs}

## Constraints
{constraints or "None specified"}

Review ALL specialist outputs as the final security gate. Produce a security review with APPROVE or VETO decision. \
Flag any unresolved CRITICAL security issues that block delivery."""
    else:
        context = f"""## Mode: INITIAL SECURITY ASSESSMENT (Phase 1)

## Spec Summary
{spec_summary}

## Application Architecture Output
{app_output or "Not yet available — this is the initial assessment before other specialists run."}

## Data Architecture Output
{data_output or "Not yet available — this is the initial assessment before other specialists run."}

## Constraints
{constraints or "None specified"}

Produce the initial security assessment: threat model, security constraints for all other specialists, \
compliance checklist, and auth architecture recommendation."""
    result = agent(context)
    return str(result)


def _get_sonnet_model() -> str:
    import os

    return os.environ.get("ARCHITECT_MODEL_SPECIALIST", "anthropic.claude-sonnet-4-20250514-v1:0")
