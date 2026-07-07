"""Adapters to call other teams (PRA, Market Research, AI Systems)."""

from .ai_systems import (
    get_ai_systems_build_status,
    start_ai_systems_build,
    wait_for_ai_systems_build_completion,
)
from .market_research import (
    market_research_to_evidence,
    request_market_research,
)
from .product_analysis import (
    get_product_analysis_status,
    run_product_analysis,
    submit_product_analysis_answers,
    wait_for_product_analysis_completion,
)

__all__ = [
    "run_product_analysis",
    "get_product_analysis_status",
    "submit_product_analysis_answers",
    "wait_for_product_analysis_completion",
    "request_market_research",
    "market_research_to_evidence",
    "start_ai_systems_build",
    "get_ai_systems_build_status",
    "wait_for_ai_systems_build_completion",
]
