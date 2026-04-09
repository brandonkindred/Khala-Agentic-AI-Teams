"""FastAPI endpoints for the market research and concept viability team."""

from __future__ import annotations

from typing import List, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from market_research_team.models import HumanReview, ResearchMission, TeamOutput, TeamTopology
from market_research_team.orchestrator import MarketResearchOrchestrator
from shared_observability import init_otel, instrument_fastapi_app

init_otel(service_name="market-research-team", team_key="market_research")

app = FastAPI(title="Market Research Team API", version="1.0.0")
instrument_fastapi_app(app, team_key="market_research")


class RunMarketResearchRequest(BaseModel):
    product_concept: str = Field(..., min_length=3, max_length=50_000)
    target_users: str = Field(..., min_length=3, max_length=10_000)
    business_goal: str = Field(..., min_length=3, max_length=10_000)
    topology: TeamTopology = TeamTopology.UNIFIED
    transcript_folder_path: Optional[str] = None
    transcripts: List[str] = Field(default_factory=list)
    human_approved: bool = False
    human_feedback: str = ""


@app.post("/market-research/run", response_model=TeamOutput)
def run_market_research(payload: RunMarketResearchRequest) -> TeamOutput:
    orchestrator = MarketResearchOrchestrator()
    mission = ResearchMission(
        product_concept=payload.product_concept,
        target_users=payload.target_users,
        business_goal=payload.business_goal,
        topology=payload.topology,
        transcript_folder_path=payload.transcript_folder_path,
        transcripts=payload.transcripts,
    )
    human_review = HumanReview(approved=payload.human_approved, feedback=payload.human_feedback)
    return orchestrator.run(mission, human_review)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
