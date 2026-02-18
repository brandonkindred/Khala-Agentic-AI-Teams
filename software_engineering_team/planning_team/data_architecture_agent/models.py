"""Models for the Data Architecture and Engineering agent."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DataArchitectureInput(BaseModel):
    """Input for the Data Architecture agent."""

    spec_content: str = ""
    architecture_overview: str = ""
    requirements_title: str = ""
    plan_dir: Optional[Any] = None


class DataArchitectureOutput(BaseModel):
    """Output from the Data Architecture agent."""

    schema_doc: str = ""
    migration_strategy: str = ""
    analytics_taxonomy: str = ""
    data_lifecycle_policy: str = ""
    summary: str = ""
