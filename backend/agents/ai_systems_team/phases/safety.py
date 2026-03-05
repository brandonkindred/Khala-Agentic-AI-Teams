"""
Safety Phase - Apply guardrails, policy checks, and human approval gates.
"""

import logging
from typing import Callable, Optional

from ..models import (
    ArchitectureResult,
    SafetyCheckpoint,
    SafetyResult,
    SpecIntakeResult,
)

logger = logging.getLogger(__name__)


def run_safety(
    spec_intake: SpecIntakeResult,
    architecture: ArchitectureResult,
    job_updater: Optional[Callable[..., None]] = None,
) -> SafetyResult:
    """
    Define safety checkpoints, guardrails, and policy requirements.
    
    Args:
        spec_intake: Result from spec intake phase
        architecture: Result from architecture phase
        job_updater: Callback for progress updates
    
    Returns:
        SafetyResult with checkpoints and guardrails
    """
    logger.info("Starting safety and governance phase")
    
    if job_updater:
        job_updater(current_phase="safety", progress=78, status_text="Defining safety checkpoints")
    
    try:
        checkpoints = _define_safety_checkpoints(spec_intake, architecture)
        
        if job_updater:
            job_updater(progress=82, status_text="Setting up guardrails")
        
        guardrails = _define_guardrails(spec_intake)
        
        if job_updater:
            job_updater(progress=86, status_text="Identifying policy requirements")
        
        policy_requirements = _define_policy_requirements(spec_intake)
        
        if job_updater:
            job_updater(progress=90, status_text="Safety configuration complete")
        
        logger.info("Safety phase complete: %d checkpoints, %d guardrails", 
                   len(checkpoints), len(guardrails))
        
        return SafetyResult(
            success=True,
            checkpoints=checkpoints,
            guardrails=guardrails,
            policy_requirements=policy_requirements,
        )
    
    except Exception as e:
        logger.error("Safety phase failed: %s", e)
        return SafetyResult(success=False, error=str(e))


def _define_safety_checkpoints(
    spec: SpecIntakeResult,
    architecture: ArchitectureResult,
) -> list:
    """Define safety checkpoints based on spec and architecture."""
    checkpoints = []
    
    for approval_point in spec.human_approval_points:
        checkpoints.append(SafetyCheckpoint(
            name=f"human_approval_{len(checkpoints) + 1}",
            description=f"Human approval required: {approval_point}",
            trigger=f"Before executing: {approval_point}",
            action="Pause execution and request human approval via notification",
            requires_human_approval=True,
        ))
    
    checkpoints.append(SafetyCheckpoint(
        name="input_validation",
        description="Validate all inputs before processing",
        trigger="On every request",
        action="Reject malformed or suspicious inputs",
        requires_human_approval=False,
    ))
    
    checkpoints.append(SafetyCheckpoint(
        name="output_filtering",
        description="Filter outputs for sensitive information",
        trigger="Before returning any response",
        action="Redact PII, secrets, and forbidden content",
        requires_human_approval=False,
    ))
    
    for action in spec.disallowed_actions:
        checkpoints.append(SafetyCheckpoint(
            name=f"block_{action.replace(' ', '_')[:20]}",
            description=f"Block disallowed action: {action}",
            trigger=f"When system attempts: {action}",
            action="Reject request and log violation",
            requires_human_approval=False,
        ))
    
    if architecture.orchestration and len(architecture.orchestration.agents) > 1:
        checkpoints.append(SafetyCheckpoint(
            name="inter_agent_validation",
            description="Validate data passed between agents",
            trigger="On every agent handoff",
            action="Verify data integrity and permissions",
            requires_human_approval=False,
        ))
    
    checkpoints.append(SafetyCheckpoint(
        name="cost_threshold",
        description="Monitor and limit API costs",
        trigger="When estimated cost exceeds threshold",
        action="Pause execution and alert operators",
        requires_human_approval=True,
    ))
    
    return checkpoints


def _define_guardrails(spec: SpecIntakeResult) -> list:
    """Define runtime guardrails."""
    guardrails = []
    
    guardrails.append("Rate limiting: Max 100 requests per minute per user")
    guardrails.append("Token limits: Max 4096 tokens per LLM request")
    guardrails.append("Timeout limits: Max 30 seconds per operation")
    guardrails.append("Retry limits: Max 3 retries with exponential backoff")
    
    for action in spec.disallowed_actions:
        guardrails.append(f"Action blocker: Prevent {action}")
    
    constraints_text = " ".join(spec.constraints).lower()
    
    if "pii" in constraints_text or "privacy" in constraints_text:
        guardrails.append("PII detection: Scan and redact personal information")
    
    if "cost" in constraints_text or "budget" in constraints_text:
        guardrails.append("Cost tracking: Monitor and enforce budget limits")
    
    guardrails.append("Content filtering: Block harmful or inappropriate outputs")
    guardrails.append("Injection prevention: Detect and block prompt injection attempts")
    guardrails.append("Audit logging: Log all actions for compliance and debugging")
    
    return guardrails


def _define_policy_requirements(spec: SpecIntakeResult) -> list:
    """Define policy requirements for the system."""
    requirements = []
    
    requirements.append("Logging: All requests and responses must be logged")
    requirements.append("Audit trail: Maintain complete history of agent decisions")
    requirements.append("Error reporting: All errors must be captured and reported")
    
    constraints_text = " ".join(spec.constraints).lower()
    
    if "gdpr" in constraints_text:
        requirements.append("GDPR compliance: Right to erasure, data portability")
        requirements.append("Data minimization: Only collect necessary data")
        requirements.append("Consent tracking: Record user consent for data processing")
    
    if "soc2" in constraints_text or "compliance" in constraints_text:
        requirements.append("SOC2 controls: Access controls, encryption, monitoring")
        requirements.append("Change management: Track all system changes")
    
    if "hipaa" in constraints_text:
        requirements.append("HIPAA compliance: PHI protection, access logging")
    
    if spec.human_approval_points:
        requirements.append("Human oversight: Document all human-in-the-loop decisions")
        requirements.append("Escalation paths: Clear process for escalating issues")
    
    requirements.append("Incident response: Defined process for security incidents")
    requirements.append("Data retention: Clear policies for data lifecycle")
    
    return requirements
