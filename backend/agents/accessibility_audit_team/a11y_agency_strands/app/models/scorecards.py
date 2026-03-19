from pydantic import BaseModel


class Scorecard(BaseModel):
    component_score: float
    page_score: float
    site_score: float
    priority_score: float
