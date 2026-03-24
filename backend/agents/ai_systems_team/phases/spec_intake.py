"""
Spec Intake Phase - Parse and extract goals, constraints, and requirements from the spec.
"""

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ..models import SpecIntakeResult

logger = logging.getLogger(__name__)


def run_spec_intake(
    spec_path: str,
    constraints: Dict[str, Any],
    job_updater: Optional[Callable[..., None]] = None,
) -> SpecIntakeResult:
    """
    Parse the specification file and extract structured requirements.

    Args:
        spec_path: Path to the specification file
        constraints: Additional constraints from the request
        job_updater: Callback for progress updates

    Returns:
        SpecIntakeResult with extracted goals, constraints, etc.
    """
    logger.info("Starting spec intake phase for: %s", spec_path)

    if job_updater:
        job_updater(current_phase="spec_intake", progress=5, status_text="Reading specification")

    try:
        spec_file = Path(spec_path)
        if not spec_file.exists():
            return SpecIntakeResult(
                success=False,
                error=f"Specification file not found: {spec_path}",
            )

        spec_content = spec_file.read_text(encoding="utf-8")

        if job_updater:
            job_updater(progress=10, status_text="Parsing specification")

        goals = _extract_goals(spec_content)
        non_goals = _extract_non_goals(spec_content)
        assumptions = _extract_assumptions(spec_content)
        spec_constraints = _extract_constraints(spec_content, constraints)
        allowed_actions = _extract_allowed_actions(spec_content)
        disallowed_actions = _extract_disallowed_actions(spec_content)
        human_approval_points = _extract_human_approval_points(spec_content)
        quality_expectations = _extract_quality_expectations(spec_content)

        if job_updater:
            job_updater(progress=15, status_text="Spec intake complete")

        logger.info(
            "Spec intake complete: %d goals, %d constraints extracted",
            len(goals),
            len(spec_constraints),
        )

        return SpecIntakeResult(
            success=True,
            goals=goals,
            non_goals=non_goals,
            assumptions=assumptions,
            constraints=spec_constraints,
            allowed_actions=allowed_actions,
            disallowed_actions=disallowed_actions,
            human_approval_points=human_approval_points,
            quality_expectations=quality_expectations,
        )

    except Exception as e:
        logger.error("Spec intake failed: %s", e)
        return SpecIntakeResult(success=False, error=str(e))


def _extract_goals(content: str) -> list:
    """Extract goals from spec content."""
    goals = []
    lines = content.split("\n")
    in_goals_section = False

    for line in lines:
        lower = line.lower().strip()
        if "goal" in lower and (":" in line or "#" in line):
            in_goals_section = True
            continue
        if in_goals_section:
            if line.strip().startswith(("-", "*", "•")):
                goals.append(line.strip().lstrip("-*• "))
            elif line.strip() and not line.startswith("#"):
                if any(kw in lower for kw in ["non-goal", "constraint", "assumption"]):
                    in_goals_section = False

    if not goals:
        goals.append("Build an AI agent system as specified")

    return goals


def _extract_non_goals(content: str) -> list:
    """Extract non-goals from spec content."""
    non_goals = []
    lines = content.split("\n")
    in_section = False

    for line in lines:
        lower = line.lower().strip()
        if "non-goal" in lower or "out of scope" in lower:
            in_section = True
            continue
        if in_section:
            if line.strip().startswith(("-", "*", "•")):
                non_goals.append(line.strip().lstrip("-*• "))
            elif line.strip().startswith("#"):
                in_section = False

    return non_goals


def _extract_assumptions(content: str) -> list:
    """Extract assumptions from spec content."""
    assumptions = []
    lines = content.split("\n")
    in_section = False

    for line in lines:
        lower = line.lower().strip()
        if "assumption" in lower:
            in_section = True
            continue
        if in_section:
            if line.strip().startswith(("-", "*", "•")):
                assumptions.append(line.strip().lstrip("-*• "))
            elif line.strip().startswith("#"):
                in_section = False

    return assumptions


def _extract_constraints(content: str, request_constraints: Dict[str, Any]) -> list:
    """Extract constraints from spec content and merge with request constraints."""
    constraints = []
    lines = content.split("\n")
    in_section = False

    for line in lines:
        lower = line.lower().strip()
        if "constraint" in lower and ":" in line:
            in_section = True
            continue
        if in_section:
            if line.strip().startswith(("-", "*", "•")):
                constraints.append(line.strip().lstrip("-*• "))
            elif line.strip().startswith("#"):
                in_section = False

    for key, value in request_constraints.items():
        constraints.append(f"{key}: {value}")

    return constraints


def _extract_allowed_actions(content: str) -> list:
    """Extract allowed actions from spec content."""
    actions = []
    lines = content.split("\n")
    in_section = False

    for line in lines:
        lower = line.lower().strip()
        if "allowed action" in lower or "permitted" in lower:
            in_section = True
            continue
        if in_section:
            if line.strip().startswith(("-", "*", "•")):
                actions.append(line.strip().lstrip("-*• "))
            elif line.strip().startswith("#"):
                in_section = False

    return actions


def _extract_disallowed_actions(content: str) -> list:
    """Extract disallowed actions from spec content."""
    actions = []
    lines = content.split("\n")
    in_section = False

    for line in lines:
        lower = line.lower().strip()
        if "disallowed" in lower or "prohibited" in lower or "not allowed" in lower:
            in_section = True
            continue
        if in_section:
            if line.strip().startswith(("-", "*", "•")):
                actions.append(line.strip().lstrip("-*• "))
            elif line.strip().startswith("#"):
                in_section = False

    return actions


def _extract_human_approval_points(content: str) -> list:
    """Extract human-in-the-loop requirements."""
    points = []
    lines = content.split("\n")
    in_section = False

    for line in lines:
        lower = line.lower().strip()
        if "human" in lower and ("approval" in lower or "review" in lower or "loop" in lower):
            in_section = True
            continue
        if in_section:
            if line.strip().startswith(("-", "*", "•")):
                points.append(line.strip().lstrip("-*• "))
            elif line.strip().startswith("#"):
                in_section = False

    return points


def _extract_quality_expectations(content: str) -> Dict[str, str]:
    """Extract quality expectations (accuracy, throughput, reliability)."""
    expectations = {}
    lines = content.split("\n")

    keywords = ["accuracy", "throughput", "latency", "reliability", "uptime", "sla"]

    for line in lines:
        lower = line.lower()
        for kw in keywords:
            if kw in lower:
                parts = line.split(":", 1)
                if len(parts) == 2:
                    expectations[kw] = parts[1].strip()

    return expectations
