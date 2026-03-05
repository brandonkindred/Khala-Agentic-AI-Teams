"""Models for the Document Generator Agent."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class GenerateDocRequest(BaseModel):
    """Request to generate a document."""

    user_id: str
    doc_type: str
    topic: str
    context: Dict[str, Any] = Field(default_factory=dict)
    format: str = "markdown"


class GenerateChecklistRequest(BaseModel):
    """Request to generate a checklist."""

    user_id: str
    task: str
    context: Dict[str, Any] = Field(default_factory=dict)
    include_time_estimates: bool = False


class GenerateTemplateRequest(BaseModel):
    """Request to generate a template."""

    user_id: str
    template_type: str
    purpose: str
    fields: List[str] = Field(default_factory=list)


class GeneratedDocument(BaseModel):
    """A generated document."""

    doc_id: str
    doc_type: str
    title: str
    content: str
    format: str = "markdown"
    created_at: str


class GeneratedChecklist(BaseModel):
    """A generated checklist."""

    checklist_id: str
    title: str
    items: List[Dict[str, Any]]
    total_time_estimate: Optional[str] = None
    created_at: str


class GeneratedTemplate(BaseModel):
    """A generated template."""

    template_id: str
    template_type: str
    title: str
    content: str
    placeholders: List[str] = Field(default_factory=list)
    created_at: str


class SOPRequest(BaseModel):
    """Request to generate a Standard Operating Procedure."""

    user_id: str
    process_name: str
    description: str
    steps: List[str] = Field(default_factory=list)
    include_safety: bool = False
    include_troubleshooting: bool = False
