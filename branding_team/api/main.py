"""FastAPI endpoints for running the branding strategy team."""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, Field

from branding_team.models import BrandCheckRequest, BrandingMission, HumanReview, TeamOutput
from branding_team.orchestrator import BrandingTeamOrchestrator

app = FastAPI(title="Branding Team API", version="1.0.0")


class RunBrandingTeamRequest(BaseModel):
    company_name: str = Field(..., min_length=2)
    company_description: str = Field(..., min_length=10)
    target_audience: str = Field(..., min_length=3)
    values: list[str] = Field(default_factory=list)
    differentiators: list[str] = Field(default_factory=list)
    desired_voice: str = Field(default="clear, confident, human")
    existing_brand_material: list[str] = Field(default_factory=list)
    wiki_path: str | None = None
    brand_checks: list[BrandCheckRequest] = Field(default_factory=list)
    human_approved: bool = False
    human_feedback: str = ""


@app.post("/branding/run", response_model=TeamOutput)
def run_branding_team(payload: RunBrandingTeamRequest) -> TeamOutput:
    orchestrator = BrandingTeamOrchestrator()
    mission = BrandingMission(
        company_name=payload.company_name,
        company_description=payload.company_description,
        target_audience=payload.target_audience,
        values=payload.values,
        differentiators=payload.differentiators,
        desired_voice=payload.desired_voice,
        existing_brand_material=payload.existing_brand_material,
        wiki_path=payload.wiki_path,
    )
    human_review = HumanReview(approved=payload.human_approved, feedback=payload.human_feedback)
    return orchestrator.run(mission=mission, human_review=human_review, brand_checks=payload.brand_checks)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
