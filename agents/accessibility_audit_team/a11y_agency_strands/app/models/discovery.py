from pydantic import BaseModel


class ClientProfile(BaseModel):
    organization: str
    goals: list[str]
    business_metrics: list[str]


class ScopeDefinition(BaseModel):
    conformance_target: str
    legal_requirements: list[str]
    site_characteristics: list[str]
    timeline_constraints: str


class SamplingPlan(BaseModel):
    tier1_pages: list[str]
    tier2_pages: list[str]
    tier3_pages: list[str]
    priority_journeys: list[str]
