"""
Orchestrator for the AI Systems Team workflow.

Coordinates the execution of all phases to generate an AI agent system blueprint.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .models import (
    AgentBlueprint,
    ArchitectureResult,
    BuildResult,
    CapabilitiesResult,
    EvaluationResult,
    Phase,
    SafetyResult,
    SpecIntakeResult,
)
from .phases import (
    run_architecture,
    run_build,
    run_capabilities,
    run_evaluation,
    run_safety,
    run_spec_intake,
)

logger = logging.getLogger(__name__)


class AISystemsOrchestrator:
    """Orchestrates the AI system generation workflow."""

    PHASE_ORDER: List[Phase] = [
        Phase.SPEC_INTAKE,
        Phase.ARCHITECTURE,
        Phase.CAPABILITIES,
        Phase.EVALUATION,
        Phase.SAFETY,
        Phase.BUILD,
    ]

    def __init__(self) -> None:
        """Initialize the orchestrator."""
        self._blueprints: Dict[str, AgentBlueprint] = {}

    def run_workflow(
        self,
        project_name: str,
        spec_path: str,
        constraints: Optional[Dict[str, Any]] = None,
        output_dir: Optional[str] = None,
        job_updater: Optional[Callable[..., None]] = None,
    ) -> AgentBlueprint:
        """
        Run the complete AI system generation workflow.
        
        Args:
            project_name: Name for the AI system project
            spec_path: Path to the specification file
            constraints: Additional constraints
            output_dir: Directory to output artifacts
            job_updater: Callback for progress updates
        
        Returns:
            AgentBlueprint with complete system design
        """
        logger.info("Starting AI system workflow for: %s", project_name)
        
        constraints = constraints or {}
        
        blueprint = AgentBlueprint(
            project_name=project_name,
            created_at=datetime.now(timezone.utc),
        )
        
        try:
            spec_intake = run_spec_intake(
                spec_path=spec_path,
                constraints=constraints,
                job_updater=job_updater,
            )
            blueprint.spec_intake = spec_intake
            blueprint.current_phase = Phase.SPEC_INTAKE
            
            if not spec_intake.success:
                blueprint.error = spec_intake.error
                return blueprint
            
            blueprint.completed_phases.append(Phase.SPEC_INTAKE)
            
            architecture = run_architecture(
                spec_intake=spec_intake,
                job_updater=job_updater,
            )
            blueprint.architecture = architecture
            blueprint.current_phase = Phase.ARCHITECTURE
            
            if not architecture.success:
                blueprint.error = architecture.error
                return blueprint
            
            blueprint.completed_phases.append(Phase.ARCHITECTURE)
            
            capabilities = run_capabilities(
                spec_intake=spec_intake,
                architecture=architecture,
                job_updater=job_updater,
            )
            blueprint.capabilities = capabilities
            blueprint.current_phase = Phase.CAPABILITIES
            
            if not capabilities.success:
                blueprint.error = capabilities.error
                return blueprint
            
            blueprint.completed_phases.append(Phase.CAPABILITIES)
            
            evaluation = run_evaluation(
                spec_intake=spec_intake,
                job_updater=job_updater,
            )
            blueprint.evaluation = evaluation
            blueprint.current_phase = Phase.EVALUATION
            
            if not evaluation.success:
                blueprint.error = evaluation.error
                return blueprint
            
            blueprint.completed_phases.append(Phase.EVALUATION)
            
            safety = run_safety(
                spec_intake=spec_intake,
                architecture=architecture,
                job_updater=job_updater,
            )
            blueprint.safety = safety
            blueprint.current_phase = Phase.SAFETY
            
            if not safety.success:
                blueprint.error = safety.error
                return blueprint
            
            blueprint.completed_phases.append(Phase.SAFETY)
            
            build = run_build(
                project_name=project_name,
                spec_intake=spec_intake,
                architecture=architecture,
                capabilities=capabilities,
                evaluation=evaluation,
                safety=safety,
                output_dir=output_dir,
                job_updater=job_updater,
            )
            blueprint.build = build
            blueprint.current_phase = Phase.BUILD
            
            if not build.success:
                blueprint.error = build.error
                return blueprint
            
            blueprint.completed_phases.append(Phase.BUILD)
            blueprint.success = True
            
            self._blueprints[project_name] = blueprint
            
            logger.info("AI system workflow complete for: %s", project_name)
            return blueprint
        
        except Exception as e:
            logger.error("Workflow failed: %s", e)
            blueprint.error = str(e)
            return blueprint

    def get_blueprint(self, project_name: str) -> Optional[AgentBlueprint]:
        """Get a previously generated blueprint by project name."""
        return self._blueprints.get(project_name)

    def list_blueprints(self) -> List[str]:
        """List all generated blueprint project names."""
        return list(self._blueprints.keys())
