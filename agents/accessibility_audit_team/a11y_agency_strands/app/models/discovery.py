from typing import List

from pydantic import BaseModel


class ClientProfile(BaseModel):
    organization: str
    goals: List[str]
    business_metrics: List[str]


class ScopeDefinition(BaseModel):
    conformance_target: str
    legal_requirements: List[str]
    site_characteristics: List[str]
    timeline_constraints: str


class SamplingPlan(BaseModel):
    tier1_pages: List[str]
    tier2_pages: List[str]
    tier3_pages: List[str]
    priority_journeys: List[str]
