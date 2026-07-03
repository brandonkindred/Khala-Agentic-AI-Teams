# shared_dev_models

Neutral, team-agnostic **software-development pipeline models**.

The Pydantic types that describe the shared development contract — `Task`,
`TaskStatus`, `TaskType`, `TaskAssignment`, the `Initiative → Epic → Story →
TaskPlan` `PlanningHierarchy`, `SystemArchitecture`, `ToolRecommendation` — are
spoken by both the software-engineering team and the coding team (whose v2 workers
hand tasks to SE's code-v2 team leads). This package is their single home.

## Layout

| Module | Was | Responsibility |
|---|---|---|
| `models` | `software_engineering_team/shared/models.py` | All shared dev-pipeline Pydantic models + `model_to_dict`. |

## Usage

```python
from shared_dev_models import Task, TaskStatus, TaskType, PlanningHierarchy
```

## Compatibility

`software_engineering_team/shared/models.py` remains as a thin compatibility
shim that re-exports this package's `models` module object (so
`software_engineering_team.shared.models.Task is shared_dev_models.models.Task` —
`isinstance` and identity are preserved for the many existing SE importers). New
code, and all of coding_team, imports `shared_dev_models` directly.
