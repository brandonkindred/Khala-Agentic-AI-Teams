"""Neutral, team-agnostic software-development pipeline models.

The Pydantic models that describe the shared development contract — tasks,
task assignments, the planning hierarchy, architecture, and tool
recommendations — used by both the software-engineering team and the coding team
(the latter feeds SE's v2 code teams, which speak these types). Promoted out of
``software_engineering_team.shared.models`` so neither team imports the other's
internals.

Layout:
    - ``models`` — the pydantic models (was ``software_engineering_team/shared/models.py``).

Preconditions:
    - ``backend/agents`` is on ``sys.path`` (the ``shared_*`` convention).
Postconditions:
    - Pure data models; importing has no side effects. ``pydantic`` is the only
      third-party dependency.
"""

from __future__ import annotations

from shared_dev_models.models import (
    ArchitectureComponent,
    Epic,
    Initiative,
    LicenseType,
    PlanningHierarchy,
    PricingTier,
    ProductRequirements,
    StoryPlan,
    SystemArchitecture,
    Task,
    TaskAssignment,
    TaskPlan,
    TaskStatus,
    TaskType,
    TaskUpdate,
    ToolRecommendation,
    model_to_dict,
)

__all__ = [
    "TaskType",
    "PricingTier",
    "LicenseType",
    "ToolRecommendation",
    "TaskStatus",
    "ProductRequirements",
    "ArchitectureComponent",
    "SystemArchitecture",
    "Task",
    "TaskUpdate",
    "TaskAssignment",
    "TaskPlan",
    "StoryPlan",
    "Epic",
    "Initiative",
    "PlanningHierarchy",
    "model_to_dict",
]
