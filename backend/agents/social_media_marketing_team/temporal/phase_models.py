"""Serializable DTOs threaded between the social marketing Temporal activities.

Each pipeline phase (consensus -> content plan -> platform -> experiment ->
finalize) runs as its own ``@activity.defn``; results cross the activity boundary
as JSON-native dicts. These Pydantic models give those payloads a typed shape --
activities emit ``model_dump(mode="json")`` and downstream activities rebuild with
``model_validate`` (mirrors ``blogging/temporal/phase_models.py`` and
``software_engineering_team/temporal/phase_models.py``).

Serialization note: ``CampaignProposal.channel_mix_strategy`` is a
``Dict[Platform, str]`` and ``Platform`` is a ``str`` enum. Activities must dump the
domain models with ``mode="json"`` so the enum keys/values become JSON string keys;
Temporal's default JSON ``DataConverter`` cannot encode enum-instance dict keys. The
``proposal``/``content_plan``/``experiment_plan`` fields below therefore hold the
JSON-mode dumps, which ``model_validate`` coerces back into the domain models.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ConsensusStageResult(BaseModel):
    """Output of the consensus activity (``build_consensus_proposal``).

    ``proposal`` is the JSON-mode dump of the ``CampaignProposal`` reached by the
    collaboration loop; ``goals`` is the JSON-mode dump of the resolved
    ``BrandGoals`` so downstream stages need not re-fetch the brand. ``status`` is
    ``PASS`` unless the stage aborted, in which case downstream stages
    short-circuit.

    Invariants:
        - ``status == "PASS"`` implies ``proposal`` and ``goals`` are populated.
    """

    proposal: Dict[str, Any] = Field(default_factory=dict)
    goals: Dict[str, Any] = Field(default_factory=dict)
    brand_name: str = ""
    status: str = "PASS"


class ContentPlanStageResult(BaseModel):
    """Output of the content-plan activity (``_load_winners`` + ``_plan_content``).

    ``content_plan`` is the JSON-mode dump of the ``ContentPlan``;
    ``winners_retrieved`` is the count of Winning-Posts-Bank exemplars loaded for
    the run (threaded into the final ``TeamOutput``).
    """

    content_plan: Dict[str, Any] = Field(default_factory=dict)
    winners_retrieved: int = 0
    status: str = "PASS"


class PlatformStageResult(BaseModel):
    """Output of the platform activity (``build_platform_plans``).

    ``platform_execution_plans`` is a list of JSON-mode ``PlatformExecutionPlan``
    dumps, one per platform specialist.
    """

    platform_execution_plans: List[Dict[str, Any]] = Field(default_factory=list)
    status: str = "PASS"


class ExperimentStageResult(BaseModel):
    """Output of the experiment activity (``build_experiment``).

    ``experiment_plan`` is the JSON-mode dump of the ``ExperimentPlan`` (``None``
    only when the stage was skipped).
    """

    experiment_plan: Optional[Dict[str, Any]] = None
    status: str = "PASS"
