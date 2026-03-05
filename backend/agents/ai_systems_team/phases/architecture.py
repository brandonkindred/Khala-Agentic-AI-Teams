"""
Architecture Phase - Design agent topology and orchestration strategy.
"""

import logging
from typing import Callable, Optional

from ..models import (
    AgentRole,
    ArchitectureResult,
    HandoffRule,
    OrchestrationGraph,
    OrchestrationPattern,
    SpecIntakeResult,
)

logger = logging.getLogger(__name__)


def run_architecture(
    spec_intake: SpecIntakeResult,
    job_updater: Optional[Callable[..., None]] = None,
) -> ArchitectureResult:
    """
    Design the agent architecture based on spec requirements.
    
    Args:
        spec_intake: Result from spec intake phase
        job_updater: Callback for progress updates
    
    Returns:
        ArchitectureResult with orchestration graph
    """
    logger.info("Starting architecture phase")
    
    if job_updater:
        job_updater(current_phase="architecture", progress=20, status_text="Analyzing requirements")
    
    try:
        pattern = _determine_orchestration_pattern(spec_intake)
        
        if job_updater:
            job_updater(progress=25, status_text="Designing agent roles")
        
        agents = _design_agent_roles(spec_intake, pattern)
        
        if job_updater:
            job_updater(progress=30, status_text="Defining handoffs")
        
        handoffs = _define_handoffs(agents, pattern)
        
        entry_point = agents[0].name if agents else None
        exit_points = [agents[-1].name] if agents else []
        
        orchestration = OrchestrationGraph(
            pattern=pattern,
            agents=agents,
            handoffs=handoffs,
            entry_point=entry_point,
            exit_points=exit_points,
        )
        
        rationale = _generate_rationale(spec_intake, pattern, agents)
        
        if job_updater:
            job_updater(progress=35, status_text="Architecture design complete")
        
        logger.info("Architecture phase complete: %s pattern, %d agents", 
                   pattern.value, len(agents))
        
        return ArchitectureResult(
            success=True,
            orchestration=orchestration,
            rationale=rationale,
        )
    
    except Exception as e:
        logger.error("Architecture phase failed: %s", e)
        return ArchitectureResult(success=False, error=str(e))


def _determine_orchestration_pattern(spec: SpecIntakeResult) -> OrchestrationPattern:
    """Determine the best orchestration pattern based on requirements."""
    goals_text = " ".join(spec.goals).lower()
    constraints_text = " ".join(spec.constraints).lower()
    
    if "parallel" in goals_text or "concurrent" in goals_text:
        return OrchestrationPattern.PARALLEL
    
    if "hierarchy" in goals_text or "supervisor" in goals_text:
        return OrchestrationPattern.HIERARCHICAL
    
    if "event" in goals_text or "reactive" in goals_text or "async" in goals_text:
        return OrchestrationPattern.EVENT_DRIVEN
    
    if "complex" in constraints_text or len(spec.goals) > 5:
        return OrchestrationPattern.HYBRID
    
    return OrchestrationPattern.SEQUENTIAL


def _design_agent_roles(
    spec: SpecIntakeResult,
    pattern: OrchestrationPattern,
) -> list:
    """Design agent roles based on goals and pattern."""
    agents = []
    
    if spec.human_approval_points:
        agents.append(AgentRole(
            name="Coordinator",
            description="Coordinates workflow and handles human approval checkpoints",
            capabilities=["workflow_management", "human_interaction"],
            tools=["notification", "approval_gate"],
            inputs=["task_request"],
            outputs=["approved_task", "rejected_task"],
        ))
    
    for i, goal in enumerate(spec.goals[:5]):
        goal_lower = goal.lower()
        
        if "research" in goal_lower or "analyze" in goal_lower:
            agents.append(AgentRole(
                name=f"Researcher_{i}",
                description=f"Research and analysis for: {goal}",
                capabilities=["research", "analysis", "summarization"],
                tools=["web_search", "document_reader"],
                inputs=["research_query"],
                outputs=["research_report"],
            ))
        elif "write" in goal_lower or "generate" in goal_lower or "create" in goal_lower:
            agents.append(AgentRole(
                name=f"Generator_{i}",
                description=f"Content generation for: {goal}",
                capabilities=["generation", "writing", "formatting"],
                tools=["llm", "template_engine"],
                inputs=["generation_request"],
                outputs=["generated_content"],
            ))
        elif "review" in goal_lower or "validate" in goal_lower:
            agents.append(AgentRole(
                name=f"Reviewer_{i}",
                description=f"Review and validation for: {goal}",
                capabilities=["review", "validation", "quality_check"],
                tools=["validator", "linter"],
                inputs=["content_to_review"],
                outputs=["review_report"],
            ))
        else:
            agents.append(AgentRole(
                name=f"Worker_{i}",
                description=f"Execute task: {goal}",
                capabilities=["task_execution"],
                tools=["general_tools"],
                inputs=["task_input"],
                outputs=["task_output"],
            ))
    
    if not agents:
        agents.append(AgentRole(
            name="MainAgent",
            description="Primary agent for executing the specified goals",
            capabilities=["general_task_execution"],
            tools=["llm", "tools"],
            inputs=["request"],
            outputs=["response"],
        ))
    
    return agents


def _define_handoffs(agents: list, pattern: OrchestrationPattern) -> list:
    """Define handoff rules between agents."""
    handoffs = []
    
    if pattern == OrchestrationPattern.SEQUENTIAL:
        for i in range(len(agents) - 1):
            handoffs.append(HandoffRule(
                from_agent=agents[i].name,
                to_agent=agents[i + 1].name,
                condition=f"After {agents[i].name} completes",
                data_passed=agents[i].outputs,
            ))
    
    elif pattern == OrchestrationPattern.HIERARCHICAL:
        if agents:
            supervisor = agents[0]
            for worker in agents[1:]:
                handoffs.append(HandoffRule(
                    from_agent=supervisor.name,
                    to_agent=worker.name,
                    condition=f"Supervisor delegates to {worker.name}",
                    data_passed=["task_assignment"],
                ))
                handoffs.append(HandoffRule(
                    from_agent=worker.name,
                    to_agent=supervisor.name,
                    condition=f"{worker.name} reports completion",
                    data_passed=worker.outputs,
                ))
    
    elif pattern == OrchestrationPattern.PARALLEL:
        if len(agents) >= 2:
            handoffs.append(HandoffRule(
                from_agent="Coordinator",
                to_agent="all_workers",
                condition="Fan-out to parallel workers",
                data_passed=["task_partition"],
            ))
            handoffs.append(HandoffRule(
                from_agent="all_workers",
                to_agent="Coordinator",
                condition="Fan-in from parallel workers",
                data_passed=["partial_results"],
            ))
    
    return handoffs


def _generate_rationale(
    spec: SpecIntakeResult,
    pattern: OrchestrationPattern,
    agents: list,
) -> str:
    """Generate rationale for architecture decisions."""
    rationale_parts = [
        f"Selected {pattern.value} orchestration pattern based on the following factors:",
    ]
    
    if pattern == OrchestrationPattern.SEQUENTIAL:
        rationale_parts.append("- Goals appear to have linear dependencies")
        rationale_parts.append("- Simpler coordination, easier debugging")
    elif pattern == OrchestrationPattern.PARALLEL:
        rationale_parts.append("- Goals can be executed independently")
        rationale_parts.append("- Parallel execution improves throughput")
    elif pattern == OrchestrationPattern.HIERARCHICAL:
        rationale_parts.append("- Complex coordination requires supervision")
        rationale_parts.append("- Human approval points suggest oversight needed")
    elif pattern == OrchestrationPattern.EVENT_DRIVEN:
        rationale_parts.append("- Reactive behavior required")
        rationale_parts.append("- Asynchronous processing beneficial")
    
    rationale_parts.append(f"\nDesigned {len(agents)} agent roles to cover {len(spec.goals)} goals.")
    
    if spec.constraints:
        rationale_parts.append(f"\nConstraints considered: {', '.join(spec.constraints[:3])}")
    
    return "\n".join(rationale_parts)
