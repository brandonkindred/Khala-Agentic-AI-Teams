"""Strands/AWS integration helpers for the accessibility audit team."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from .models import AccessibilityAuditResult, AuditRequest
from .orchestrator import AccessibilityAuditOrchestrator


class StrandsAuditInvocation(BaseModel):
    """Payload contract used by Strands runtimes for audit execution."""

    audit_request: AuditRequest = Field(..., description="Accessibility audit request")
    tech_stack: Dict[str, str] = Field(
        default_factory=lambda: {"web": "other", "mobile": "other"},
        description="Optional stack metadata used for remediation guidance",
    )


def create_accessibility_audit_orchestrator(
    llm_client: Optional[Any] = None,
) -> AccessibilityAuditOrchestrator:
    """Construct an orchestrator instance compatible with Strands tool invocation."""
    return AccessibilityAuditOrchestrator(llm_client=llm_client)


async def run_audit_async(
    llm_client: Optional[Any],
    payload: Dict[str, Any],
) -> AccessibilityAuditResult:
    """Async entrypoint for Strands-compatible runtimes."""
    invocation = StrandsAuditInvocation.model_validate(payload)
    orchestrator = create_accessibility_audit_orchestrator(llm_client=llm_client)
    return await orchestrator.run_audit(
        audit_request=invocation.audit_request,
        tech_stack=invocation.tech_stack,
    )


def run_audit_sync(
    llm_client: Optional[Any],
    payload: Dict[str, Any],
) -> AccessibilityAuditResult:
    """Sync wrapper for runtimes that invoke tools in a synchronous context."""
    return asyncio.run(run_audit_async(llm_client=llm_client, payload=payload))


def get_team_spec() -> Dict[str, Any]:
    """Return Strands registration metadata for the accessibility audit team."""
    return {
        "name": "accessibility_audit_team",
        "description": (
            "Runs a full accessibility audit workflow across web/mobile targets "
            "and returns structured findings and report metadata."
        ),
        "input_model": StrandsAuditInvocation,
        "output_model": AccessibilityAuditResult,
        "handler_factory": run_audit_sync,
        "async_handler_factory": run_audit_async,
    }
