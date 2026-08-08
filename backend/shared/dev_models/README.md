# shared.dev_models

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
from shared.dev_models import Task, TaskStatus, TaskType, PlanningHierarchy
```

## Compatibility

The `software_engineering_team/shared/models.py` compatibility shim has been
removed. All SE and coding_team importers now import `shared.dev_models` /
`shared.dev_models.models` directly.
