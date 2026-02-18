from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class UiUxDesignInput(BaseModel):
    spec_content: str = ""
    requirements_title: str = ""
    features_doc: str = ""
    plan_dir: Optional[Any] = None


class UiUxDesignOutput(BaseModel):
    user_journeys: str = ""
    wireframes: str = ""
    component_inventory: str = ""
    accessibility_requirements: str = ""
    summary: str = ""
