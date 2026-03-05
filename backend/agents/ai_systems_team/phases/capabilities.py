"""
Capabilities Phase - Map requirements to tools, memory, and model configurations.
"""

import logging
from typing import Callable, Dict, Optional

from ..models import (
    ArchitectureResult,
    CapabilitiesResult,
    MemoryPolicy,
    SpecIntakeResult,
    ToolContract,
)

logger = logging.getLogger(__name__)


def run_capabilities(
    spec_intake: SpecIntakeResult,
    architecture: ArchitectureResult,
    job_updater: Optional[Callable[..., None]] = None,
) -> CapabilitiesResult:
    """
    Map requirements to tools, memory, and model configurations.
    
    Args:
        spec_intake: Result from spec intake phase
        architecture: Result from architecture phase
        job_updater: Callback for progress updates
    
    Returns:
        CapabilitiesResult with tool contracts and policies
    """
    logger.info("Starting capabilities planning phase")
    
    if job_updater:
        job_updater(current_phase="capabilities", progress=40, status_text="Planning tool contracts")
    
    try:
        tool_contracts = _design_tool_contracts(spec_intake, architecture)
        
        if job_updater:
            job_updater(progress=45, status_text="Defining memory policy")
        
        memory_policy = _design_memory_policy(spec_intake)
        
        if job_updater:
            job_updater(progress=50, status_text="Determining model requirements")
        
        model_requirements = _determine_model_requirements(spec_intake, architecture)
        
        if job_updater:
            job_updater(progress=55, status_text="Capabilities planning complete")
        
        logger.info("Capabilities phase complete: %d tool contracts defined", 
                   len(tool_contracts))
        
        return CapabilitiesResult(
            success=True,
            tool_contracts=tool_contracts,
            memory_policy=memory_policy,
            model_requirements=model_requirements,
        )
    
    except Exception as e:
        logger.error("Capabilities phase failed: %s", e)
        return CapabilitiesResult(success=False, error=str(e))


def _design_tool_contracts(
    spec: SpecIntakeResult,
    architecture: ArchitectureResult,
) -> list:
    """Design tool contracts based on agent capabilities."""
    contracts = []
    
    if architecture.orchestration:
        for agent in architecture.orchestration.agents:
            for tool_name in agent.tools:
                contract = _create_tool_contract(tool_name, agent.capabilities)
                if contract and not any(c.name == contract.name for c in contracts):
                    contracts.append(contract)
    
    goals_text = " ".join(spec.goals).lower()
    
    if "search" in goals_text or "research" in goals_text:
        contracts.append(ToolContract(
            name="web_search",
            description="Search the web for information",
            inputs={"query": "str", "max_results": "int"},
            outputs={"results": "List[SearchResult]"},
            error_handling="Retry with backoff on rate limits",
            rate_limits="10 requests per minute",
        ))
    
    if "file" in goals_text or "document" in goals_text:
        contracts.append(ToolContract(
            name="file_operations",
            description="Read and write files",
            inputs={"path": "str", "operation": "str", "content": "Optional[str]"},
            outputs={"result": "FileOperationResult"},
            error_handling="Validate paths, handle permission errors",
        ))
    
    if "api" in goals_text or "http" in goals_text:
        contracts.append(ToolContract(
            name="http_client",
            description="Make HTTP requests to external APIs",
            inputs={"method": "str", "url": "str", "body": "Optional[dict]"},
            outputs={"response": "HTTPResponse"},
            error_handling="Retry transient errors, circuit breaker for failures",
            rate_limits="Configured per endpoint",
        ))
    
    contracts.append(ToolContract(
        name="llm",
        description="Language model for reasoning and generation",
        inputs={"prompt": "str", "temperature": "float", "max_tokens": "int"},
        outputs={"response": "str", "usage": "TokenUsage"},
        error_handling="Fallback to alternative model on failure",
    ))
    
    return contracts


def _create_tool_contract(tool_name: str, capabilities: list) -> Optional[ToolContract]:
    """Create a tool contract based on tool name and capabilities."""
    tool_templates = {
        "web_search": ToolContract(
            name="web_search",
            description="Search engine integration",
            inputs={"query": "str"},
            outputs={"results": "List[SearchResult]"},
            error_handling="Retry on timeout",
        ),
        "document_reader": ToolContract(
            name="document_reader",
            description="Read and parse documents",
            inputs={"path": "str", "format": "str"},
            outputs={"content": "str", "metadata": "dict"},
            error_handling="Handle unsupported formats gracefully",
        ),
        "validator": ToolContract(
            name="validator",
            description="Validate content against rules",
            inputs={"content": "str", "rules": "List[Rule]"},
            outputs={"valid": "bool", "errors": "List[ValidationError]"},
            error_handling="Continue validation on individual rule failures",
        ),
        "notification": ToolContract(
            name="notification",
            description="Send notifications to users",
            inputs={"recipient": "str", "message": "str", "channel": "str"},
            outputs={"sent": "bool", "message_id": "str"},
            error_handling="Queue for retry on delivery failure",
        ),
    }
    
    return tool_templates.get(tool_name)


def _design_memory_policy(spec: SpecIntakeResult) -> MemoryPolicy:
    """Design memory and state management policy."""
    goals_text = " ".join(spec.goals).lower()
    constraints_text = " ".join(spec.constraints).lower()
    
    long_term = "remember" in goals_text or "history" in goals_text or "learn" in goals_text
    retrieval = "retrieve" in goals_text or "rag" in goals_text or "knowledge" in goals_text
    
    retention_days = 30
    if "privacy" in constraints_text or "gdpr" in constraints_text:
        retention_days = 7
    if "compliance" in constraints_text:
        retention_days = 90
    
    return MemoryPolicy(
        session_memory=True,
        long_term_memory=long_term,
        retrieval_enabled=retrieval,
        audit_trail=True,
        retention_days=retention_days,
    )


def _determine_model_requirements(
    spec: SpecIntakeResult,
    architecture: ArchitectureResult,
) -> Dict[str, str]:
    """Determine model requirements based on capabilities needed."""
    requirements = {}
    
    goals_text = " ".join(spec.goals).lower()
    constraints_text = " ".join(spec.constraints).lower()
    
    if "code" in goals_text or "programming" in goals_text:
        requirements["primary_model"] = "code-specialized (e.g., CodeLlama, DeepSeek-Coder)"
    elif "reasoning" in goals_text or "complex" in goals_text:
        requirements["primary_model"] = "high-capability (e.g., GPT-4, Claude)"
    else:
        requirements["primary_model"] = "general-purpose (e.g., GPT-3.5, Llama)"
    
    if "latency" in constraints_text or "fast" in constraints_text:
        requirements["fallback_model"] = "fast-inference model for latency-sensitive tasks"
    
    if "embedding" in goals_text or "similarity" in goals_text:
        requirements["embedding_model"] = "embedding model (e.g., text-embedding-ada-002)"
    
    if architecture.orchestration and len(architecture.orchestration.agents) > 3:
        requirements["orchestration_model"] = "lightweight model for routing decisions"
    
    return requirements
