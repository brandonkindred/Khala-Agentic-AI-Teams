"""Orchestrator for the AI Systems Team workflow.

Coordinates the execution of all phases via a Strands sequential Graph
to generate an AI agent system blueprint. Supports skip-resume for
completed phases.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .models import (
    AgentBlueprint,
    Phase,
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

# Phase name to Phase enum mapping
_PHASE_NAME_MAP = {
    "spec_intake": Phase.SPEC_INTAKE,
    "architecture": Phase.ARCHITECTURE,
    "capabilities": Phase.CAPABILITIES,
    "evaluation": Phase.EVALUATION,
    "safety": Phase.SAFETY,
    "build": Phase.BUILD,
}


class AISystemsOrchestrator:
    """Orchestrates the AI system generation workflow via a Strands Graph."""

    PHASE_ORDER: List[Phase] = [
        Phase.SPEC_INTAKE,
        Phase.ARCHITECTURE,
        Phase.CAPABILITIES,
        Phase.EVALUATION,
        Phase.SAFETY,
        Phase.BUILD,
    ]

    def __init__(self) -> None:
        self._blueprints: Dict[str, AgentBlueprint] = {}

    def run_workflow(
        self,
        project_name: str,
        spec_path: str,
        constraints: Optional[Dict[str, Any]] = None,
        output_dir: Optional[str] = None,
        job_updater: Optional[Callable[..., None]] = None,
        resume_blueprint: Optional[AgentBlueprint] = None,
    ) -> AgentBlueprint:
        """Run the complete AI system generation workflow.

        Uses a Strands sequential Graph for phase orchestration. Completed
        phases from ``resume_blueprint`` are skipped and their results reused.
        """
        logger.info("Starting AI system workflow for: %s", project_name)

        constraints = constraints or {}
        skip = set(resume_blueprint.completed_phases) if resume_blueprint else set()
        if skip:
            logger.info("Resuming workflow — skipping completed phases: %s", [p.value for p in skip])

        blueprint = resume_blueprint.model_copy() if resume_blueprint else AgentBlueprint(
            project_name=project_name,
            created_at=datetime.now(timezone.utc),
        )
        blueprint.error = None
        blueprint.success = False

        def _checkpoint() -> None:
            if job_updater:
                try:
                    job_updater(blueprint_snapshot=blueprint.model_dump(mode="json"))
                except Exception:
                    pass

        try:
            # -- SPEC_INTAKE --
            if Phase.SPEC_INTAKE in skip and blueprint.spec_intake:
                spec_intake = blueprint.spec_intake
                logger.info("Skipping SPEC_INTAKE (already completed)")
            else:
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

                if Phase.SPEC_INTAKE not in blueprint.completed_phases:
                    blueprint.completed_phases.append(Phase.SPEC_INTAKE)
                _checkpoint()

            # -- ARCHITECTURE --
            if Phase.ARCHITECTURE in skip and blueprint.architecture:
                architecture = blueprint.architecture
                logger.info("Skipping ARCHITECTURE (already completed)")
            else:
                architecture = run_architecture(
                    spec_intake=spec_intake,
                    job_updater=job_updater,
                )
                blueprint.architecture = architecture
                blueprint.current_phase = Phase.ARCHITECTURE

                if not architecture.success:
                    blueprint.error = architecture.error
                    return blueprint

                if Phase.ARCHITECTURE not in blueprint.completed_phases:
                    blueprint.completed_phases.append(Phase.ARCHITECTURE)
                _checkpoint()

            # -- CAPABILITIES --
            if Phase.CAPABILITIES in skip and blueprint.capabilities:
                capabilities = blueprint.capabilities
                logger.info("Skipping CAPABILITIES (already completed)")
            else:
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

                if Phase.CAPABILITIES not in blueprint.completed_phases:
                    blueprint.completed_phases.append(Phase.CAPABILITIES)
                _checkpoint()

            # -- EVALUATION --
            if Phase.EVALUATION in skip and blueprint.evaluation:
                evaluation = blueprint.evaluation
                logger.info("Skipping EVALUATION (already completed)")
            else:
                evaluation = run_evaluation(
                    spec_intake=spec_intake,
                    job_updater=job_updater,
                )
                blueprint.evaluation = evaluation
                blueprint.current_phase = Phase.EVALUATION

                if not evaluation.success:
                    blueprint.error = evaluation.error
                    return blueprint

                if Phase.EVALUATION not in blueprint.completed_phases:
                    blueprint.completed_phases.append(Phase.EVALUATION)
                _checkpoint()

            # -- SAFETY --
            if Phase.SAFETY in skip and blueprint.safety:
                safety = blueprint.safety
                logger.info("Skipping SAFETY (already completed)")
            else:
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

                if Phase.SAFETY not in blueprint.completed_phases:
                    blueprint.completed_phases.append(Phase.SAFETY)
                _checkpoint()

            # -- BUILD --
            if Phase.BUILD in skip and blueprint.build:
                logger.info("Skipping BUILD (already completed)")
            else:
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

                if Phase.BUILD not in blueprint.completed_phases:
                    blueprint.completed_phases.append(Phase.BUILD)
                _checkpoint()

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
