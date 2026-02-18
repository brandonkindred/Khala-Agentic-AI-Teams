from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class FrontendArchitectureInput(BaseModel):
    spec_content: str = ""
    architecture_overview: str = ""
    ui_ux_doc: str = ""
    requirements_title: str = ""
    plan_dir: Optional[Any] = None


class FrontendArchitectureOutput(BaseModel):
    architecture_doc: str = ""
    design_system: str = ""
    api_client_patterns: str = ""
    test_strategy: str = ""
    summary: str = ""
