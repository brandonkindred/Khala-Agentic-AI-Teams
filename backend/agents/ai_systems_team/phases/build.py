"""
Build Phase - Package final output into implementation-ready artifacts.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ..models import (
    AgentBlueprint,
    ArchitectureResult,
    BuildResult,
    CapabilitiesResult,
    EvaluationResult,
    RolloutPlan,
    RolloutStage,
    SafetyResult,
    SpecIntakeResult,
)

logger = logging.getLogger(__name__)


def run_build(
    project_name: str,
    spec_intake: SpecIntakeResult,
    architecture: ArchitectureResult,
    capabilities: CapabilitiesResult,
    evaluation: EvaluationResult,
    safety: SafetyResult,
    output_dir: Optional[str] = None,
    job_updater: Optional[Callable[..., None]] = None,
) -> BuildResult:
    """
    Package all phase outputs into a complete blueprint.
    
    Args:
        project_name: Name of the AI system project
        spec_intake: Result from spec intake phase
        architecture: Result from architecture phase
        capabilities: Result from capabilities phase
        evaluation: Result from evaluation phase
        safety: Result from safety phase
        output_dir: Directory to write artifacts
        job_updater: Callback for progress updates
    
    Returns:
        BuildResult with artifact list and rollout plan
    """
    logger.info("Starting build phase for: %s", project_name)
    
    if job_updater:
        job_updater(current_phase="build", progress=92, status_text="Creating rollout plan")
    
    try:
        rollout_plan = _create_rollout_plan(spec_intake, safety)
        
        if job_updater:
            job_updater(progress=95, status_text="Generating artifacts")
        
        artifacts = []
        
        if output_dir:
            artifacts = _write_artifacts(
                project_name=project_name,
                output_dir=output_dir,
                spec_intake=spec_intake,
                architecture=architecture,
                capabilities=capabilities,
                evaluation=evaluation,
                safety=safety,
                rollout_plan=rollout_plan,
            )
        
        if job_updater:
            job_updater(progress=100, status_text="Build complete")
        
        logger.info("Build phase complete: %d artifacts generated", len(artifacts))
        
        return BuildResult(
            success=True,
            artifacts=artifacts,
            rollout_plan=rollout_plan,
            finalized_at=datetime.now(timezone.utc),
        )
    
    except Exception as e:
        logger.error("Build phase failed: %s", e)
        return BuildResult(success=False, error=str(e))


def _create_rollout_plan(spec: SpecIntakeResult, safety: SafetyResult) -> RolloutPlan:
    """Create a rollout plan with staged deployment."""
    stages = [
        RolloutStage(
            name="pilot",
            description="Limited deployment to internal users or test environment",
            criteria_to_advance="All acceptance tests pass, no critical issues for 1 week",
            rollback_criteria="Any critical bug or safety violation",
        ),
        RolloutStage(
            name="staged",
            description="Gradual rollout to 10% of users",
            criteria_to_advance="Error rate < 1%, positive user feedback, KPIs met for 2 weeks",
            rollback_criteria="Error rate > 5% or any safety checkpoint failures",
        ),
        RolloutStage(
            name="production",
            description="Full deployment to all users",
            criteria_to_advance="N/A - final stage",
            rollback_criteria="Major incident or compliance violation",
        ),
    ]
    
    if safety.checkpoints and any(cp.requires_human_approval for cp in safety.checkpoints):
        stages.insert(1, RolloutStage(
            name="human_review",
            description="Extended period with enhanced human oversight",
            criteria_to_advance="Human approval rate > 95% for 100 reviews",
            rollback_criteria="Human approval rate < 80%",
        ))
    
    return RolloutPlan(stages=stages)


def _write_artifacts(
    project_name: str,
    output_dir: str,
    spec_intake: SpecIntakeResult,
    architecture: ArchitectureResult,
    capabilities: CapabilitiesResult,
    evaluation: EvaluationResult,
    safety: SafetyResult,
    rollout_plan: RolloutPlan,
) -> list:
    """Write blueprint artifacts to disk."""
    artifacts = []
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    blueprint_data = {
        "project_name": project_name,
        "version": "1.0.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "spec_intake": spec_intake.model_dump() if spec_intake.success else None,
        "architecture": architecture.model_dump() if architecture.success else None,
        "capabilities": capabilities.model_dump() if capabilities.success else None,
        "evaluation": evaluation.model_dump() if evaluation.success else None,
        "safety": safety.model_dump() if safety.success else None,
        "rollout_plan": rollout_plan.model_dump() if rollout_plan else None,
    }
    
    blueprint_path = output_path / "blueprint.json"
    blueprint_path.write_text(json.dumps(blueprint_data, indent=2, default=str), encoding="utf-8")
    artifacts.append(str(blueprint_path))
    
    readme_content = _generate_readme(
        project_name=project_name,
        spec_intake=spec_intake,
        architecture=architecture,
        capabilities=capabilities,
        evaluation=evaluation,
        safety=safety,
        rollout_plan=rollout_plan,
    )
    readme_path = output_path / "README.md"
    readme_path.write_text(readme_content, encoding="utf-8")
    artifacts.append(str(readme_path))
    
    if architecture.orchestration:
        agents_data = [agent.model_dump() for agent in architecture.orchestration.agents]
        agents_path = output_path / "agent_roster.json"
        agents_path.write_text(json.dumps(agents_data, indent=2), encoding="utf-8")
        artifacts.append(str(agents_path))
    
    if evaluation.harness:
        tests_data = {
            "acceptance_tests": [t.model_dump() for t in evaluation.harness.acceptance_tests],
            "adversarial_tests": evaluation.harness.adversarial_tests,
            "kpis": [k.model_dump() for k in evaluation.harness.kpis],
            "pass_threshold": evaluation.harness.pass_threshold,
        }
        tests_path = output_path / "evaluation_harness.json"
        tests_path.write_text(json.dumps(tests_data, indent=2), encoding="utf-8")
        artifacts.append(str(tests_path))
    
    if safety.checkpoints:
        safety_data = {
            "checkpoints": [cp.model_dump() for cp in safety.checkpoints],
            "guardrails": safety.guardrails,
            "policy_requirements": safety.policy_requirements,
        }
        safety_path = output_path / "safety_config.json"
        safety_path.write_text(json.dumps(safety_data, indent=2), encoding="utf-8")
        artifacts.append(str(safety_path))
    
    return artifacts


def _generate_readme(
    project_name: str,
    spec_intake: SpecIntakeResult,
    architecture: ArchitectureResult,
    capabilities: CapabilitiesResult,
    evaluation: EvaluationResult,
    safety: SafetyResult,
    rollout_plan: RolloutPlan,
) -> str:
    """Generate README documentation for the blueprint."""
    sections = [
        f"# {project_name} - AI Agent System Blueprint",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        "## Overview",
        "",
    ]
    
    if spec_intake.goals:
        sections.append("### Goals")
        for goal in spec_intake.goals:
            sections.append(f"- {goal}")
        sections.append("")
    
    if spec_intake.constraints:
        sections.append("### Constraints")
        for constraint in spec_intake.constraints:
            sections.append(f"- {constraint}")
        sections.append("")
    
    if architecture.orchestration:
        sections.append("## Architecture")
        sections.append("")
        sections.append(f"**Orchestration Pattern**: {architecture.orchestration.pattern.value}")
        sections.append("")
        sections.append("### Agent Roster")
        for agent in architecture.orchestration.agents:
            sections.append(f"- **{agent.name}**: {agent.description}")
        sections.append("")
        if architecture.rationale:
            sections.append("### Design Rationale")
            sections.append(architecture.rationale)
            sections.append("")
    
    if capabilities.tool_contracts:
        sections.append("## Tools")
        sections.append("")
        for tool in capabilities.tool_contracts:
            sections.append(f"### {tool.name}")
            sections.append(tool.description)
            sections.append("")
    
    if safety.checkpoints:
        sections.append("## Safety Checkpoints")
        sections.append("")
        for cp in safety.checkpoints:
            human = " (requires human approval)" if cp.requires_human_approval else ""
            sections.append(f"- **{cp.name}**{human}: {cp.description}")
        sections.append("")
    
    if rollout_plan.stages:
        sections.append("## Rollout Plan")
        sections.append("")
        for stage in rollout_plan.stages:
            sections.append(f"### {stage.name.title()}")
            sections.append(stage.description)
            sections.append(f"- **Advance when**: {stage.criteria_to_advance}")
            sections.append(f"- **Rollback if**: {stage.rollback_criteria}")
            sections.append("")
    
    return "\n".join(sections)
