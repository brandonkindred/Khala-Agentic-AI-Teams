"""
Evaluation Phase - Create acceptance tests, adversarial tests, and KPIs.
"""

import logging
from typing import Callable, Optional

from ..models import (
    AcceptanceTest,
    EvaluationHarness,
    EvaluationResult,
    KPI,
    SpecIntakeResult,
)

logger = logging.getLogger(__name__)


def run_evaluation(
    spec_intake: SpecIntakeResult,
    job_updater: Optional[Callable[..., None]] = None,
) -> EvaluationResult:
    """
    Create evaluation harness with tests and KPIs.
    
    Args:
        spec_intake: Result from spec intake phase
        job_updater: Callback for progress updates
    
    Returns:
        EvaluationResult with test harness
    """
    logger.info("Starting evaluation phase")
    
    if job_updater:
        job_updater(current_phase="evaluation", progress=60, status_text="Creating acceptance tests")
    
    try:
        acceptance_tests = _create_acceptance_tests(spec_intake)
        
        if job_updater:
            job_updater(progress=65, status_text="Creating adversarial tests")
        
        adversarial_tests = _create_adversarial_tests(spec_intake)
        
        if job_updater:
            job_updater(progress=70, status_text="Defining KPIs")
        
        kpis = _define_kpis(spec_intake)
        
        pass_threshold = _determine_pass_threshold(spec_intake)
        
        harness = EvaluationHarness(
            acceptance_tests=acceptance_tests,
            adversarial_tests=adversarial_tests,
            kpis=kpis,
            pass_threshold=pass_threshold,
        )
        
        if job_updater:
            job_updater(progress=75, status_text="Evaluation harness complete")
        
        logger.info("Evaluation phase complete: %d acceptance tests, %d KPIs", 
                   len(acceptance_tests), len(kpis))
        
        return EvaluationResult(
            success=True,
            harness=harness,
        )
    
    except Exception as e:
        logger.error("Evaluation phase failed: %s", e)
        return EvaluationResult(success=False, error=str(e))


def _create_acceptance_tests(spec: SpecIntakeResult) -> list:
    """Create acceptance tests from goals."""
    tests = []
    
    for i, goal in enumerate(spec.goals):
        tests.append(AcceptanceTest(
            name=f"test_goal_{i + 1}",
            description=f"Verify: {goal}",
            input_scenario=f"Given the system receives a request related to: {goal}",
            expected_outcome=f"The system successfully achieves: {goal}",
            pass_criteria="Output matches expected behavior without errors",
        ))
    
    for i, constraint in enumerate(spec.constraints):
        tests.append(AcceptanceTest(
            name=f"test_constraint_{i + 1}",
            description=f"Verify constraint: {constraint}",
            input_scenario="Given any valid input",
            expected_outcome=f"System respects constraint: {constraint}",
            pass_criteria="Constraint is not violated during execution",
        ))
    
    for i, action in enumerate(spec.disallowed_actions):
        tests.append(AcceptanceTest(
            name=f"test_disallowed_{i + 1}",
            description=f"Verify disallowed action is blocked: {action}",
            input_scenario=f"Given an attempt to: {action}",
            expected_outcome="System refuses or prevents the action",
            pass_criteria="Action is blocked with appropriate error message",
        ))
    
    return tests


def _create_adversarial_tests(spec: SpecIntakeResult) -> list:
    """Create adversarial test scenarios."""
    tests = []
    
    tests.append("Prompt injection: Attempt to override system instructions")
    tests.append("Input fuzzing: Malformed or unexpected input formats")
    tests.append("Resource exhaustion: Very large inputs or many concurrent requests")
    tests.append("Timeout behavior: Slow external dependencies")
    
    if spec.disallowed_actions:
        tests.append(f"Jailbreak attempts: Try to bypass restrictions on {spec.disallowed_actions[0]}")
    
    for action in spec.disallowed_actions:
        tests.append(f"Social engineering: Convince system to perform: {action}")
    
    tests.append("Context manipulation: Attempt to confuse agent state")
    tests.append("Error injection: Force error conditions to test recovery")
    
    return tests


def _define_kpis(spec: SpecIntakeResult) -> list:
    """Define KPIs based on quality expectations."""
    kpis = []
    
    kpis.append(KPI(
        name="task_success_rate",
        description="Percentage of tasks completed successfully",
        metric="successful_tasks / total_tasks",
        target_value=">= 95%",
        measurement_method="Track task completion status in logs",
    ))
    
    kpis.append(KPI(
        name="response_latency_p50",
        description="Median response time",
        metric="p50 of response times",
        target_value="< 2 seconds",
        measurement_method="Measure from request received to response sent",
    ))
    
    kpis.append(KPI(
        name="error_rate",
        description="Percentage of requests resulting in errors",
        metric="error_count / total_requests",
        target_value="< 1%",
        measurement_method="Count exceptions and error responses",
    ))
    
    if "accuracy" in spec.quality_expectations:
        kpis.append(KPI(
            name="accuracy",
            description="Accuracy of agent outputs",
            metric="correct_outputs / total_outputs",
            target_value=spec.quality_expectations["accuracy"],
            measurement_method="Compare outputs to ground truth labels",
        ))
    
    if "throughput" in spec.quality_expectations:
        kpis.append(KPI(
            name="throughput",
            description="Requests processed per second",
            metric="requests / second",
            target_value=spec.quality_expectations["throughput"],
            measurement_method="Measure request rate under load",
        ))
    
    if spec.human_approval_points:
        kpis.append(KPI(
            name="human_approval_rate",
            description="Percentage of human approvals granted",
            metric="approved / total_review_requests",
            target_value=">= 90%",
            measurement_method="Track approval decisions at checkpoints",
        ))
    
    return kpis


def _determine_pass_threshold(spec: SpecIntakeResult) -> float:
    """Determine overall pass threshold for evaluation."""
    constraints_text = " ".join(spec.constraints).lower()
    
    if "critical" in constraints_text or "safety" in constraints_text:
        return 0.95
    elif "high" in constraints_text:
        return 0.90
    else:
        return 0.80
