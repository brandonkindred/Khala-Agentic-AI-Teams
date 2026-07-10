"""Serializable DTOs threaded between the blogging pipeline's Temporal activities.

Each pipeline phase (planning -> draft -> gates -> finalize) runs as its own
``@activity.defn``; results cross the activity boundary as JSON-native dicts. These
Pydantic models give those payloads a typed shape — activities emit
``model_dump(mode="json")`` and downstream activities rebuild with ``model_validate``
(mirrors ``software_engineering_team/temporal/phase_models.py``).

Large artifacts (drafts, plans) already live under the job's ``work_dir`` on the
shared volume, so only the light state needed to resume the next phase travels
through Temporal here: the planning result, the current draft, elicited stories,
and the pipeline status.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class PlanningStageResult(BaseModel):
    """Output of the planning activity (``run_planning_stage``).

    Invariants:
        - ``status == "PASS"`` unless the job was cancelled/failed while awaiting
          outline approval, in which case ``status == "FAIL"`` and the draft/gates
          activities short-circuit.
    """

    planning_phase_result: Dict[str, Any] = Field(default_factory=dict)
    elicited_stories_text: Optional[str] = None
    status: str = "PASS"


class DraftStageResult(BaseModel):
    """Output of the draft activity (``run_draft_stage``).

    ``draft`` is the ``WriterOutput`` model dump (``None`` only when the stage was
    skipped because a prior stage aborted). ``elicited_stories_text`` may have been
    extended by post-draft story elicitation, so the gates activity reads it from
    here rather than from the planning result.
    """

    draft: Optional[Dict[str, Any]] = None
    elicited_stories_text: Optional[str] = None
    status: str = "PASS"


class GatesStageResult(BaseModel):
    """Output of the gates activity (``run_gates_stage``).

    ``status`` is the terminal pipeline status: ``PASS`` when all quality gates
    passed (or gates were skipped) and ``NEEDS_HUMAN_REVIEW`` when the rewrite loop
    exhausted its budget. ``draft`` is the final ``WriterOutput`` model dump.
    """

    draft: Optional[Dict[str, Any]] = None
    status: str = "PASS"
