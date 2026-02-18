from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ObservabilityPlanningInput(BaseModel):
    architecture_overview: str = ""
    infrastructure_doc: str = ""
    devops_doc: str = ""
    requirements_title: str = ""
    plan_dir: Optional[Any] = None


class ObservabilityPlanningOutput(BaseModel):
    slos_slis: str = ""
    logging_metrics_tracing: str = ""
    alerting_runbooks: str = ""
    capacity_plan: str = ""
    summary: str = ""
