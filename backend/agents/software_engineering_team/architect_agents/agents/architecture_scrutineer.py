"""Architecture Scrutineer Agent — cross-reviews all specialist outputs for conflicts, gaps, and risks."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from strands import Agent, tool  # noqa: E402
from tools import document_writer_tool, file_read_tool, web_search_tool  # noqa: E402

_PROMPT_PATH = _root / "prompts" / "scrutineer.md"


@tool
def architecture_scrutineer(
    spec_summary: str,
    all_specialist_outputs: str,
    security_constraints: str,
) -> str:
    """Cross-review all specialist architecture outputs for conflicts, security gaps, and risks.

    Call this in Phase 5, after ALL specialists have completed. Reviews the combined
    outputs for consistency, security compliance, performance bottlenecks, cost
    overruns, and integration gaps. CRITICAL findings trigger re-runs of affected
    specialists.

    Args:
        spec_summary: High-level summary of the product/spec requirements.
        all_specialist_outputs: Concatenated outputs from ALL specialist architects.
        security_constraints: Security constraints from the Phase 1 security assessment.

    Returns:
        Scrutiny report with findings (CRITICAL/HIGH/MEDIUM/LOW), remediations, and re-run recommendations.
    """
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    agent = Agent(
        model=_get_opus_model(),
        system_prompt=prompt,
        tools=[file_read_tool, web_search_tool, document_writer_tool],
        callback_handler=None,
    )
    context = f"""## Spec Summary
{spec_summary}

## Security Constraints (from Phase 1)
{security_constraints}

## All Specialist Architecture Outputs
{all_specialist_outputs}

Scrutinize all specialist outputs. Produce a findings report with severity levels, remediations, and which specialists need to re-run."""
    result = agent(context)
    return str(result)


def _get_opus_model() -> str:
    return os.environ.get("ARCHITECT_MODEL_ORCHESTRATOR", "anthropic.claude-opus-4-6-v1")
