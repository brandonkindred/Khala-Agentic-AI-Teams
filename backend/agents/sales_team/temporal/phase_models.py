"""Serializable state passed between the sales Temporal workflow and its activities.

The fine-grained ``SalesWorkflow`` fans out one activity per prospect per stage.
Rather than threading a growing result payload through every activity (which
would balloon toward Temporal's payload ceiling), the accumulating
``SalesPipelineResult`` lives in **workflow state**, and every activity receives
only this small, **constant-size** :class:`SalesRunContext` plus the specific
item(s) it operates on.

``SalesRunContext`` is a Pydantic model so it round-trips across the
activity/workflow boundary via ``model_dump(mode="json")`` / ``model_validate``
— the same convention the software-engineering team's ``phase_models`` use.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from sales_team.models import SalesPipelineRequest


class SalesRunContext(BaseModel):
    """Immutable, constant-size per-run context handed to every sales activity.

    Built once by ``sales_prepare`` (the only place the outcome store is read)
    and passed verbatim to every downstream activity, which reconstructs a
    ``_RunContext`` from it via ``orchestrator.build_run_context``.

    Invariants:
        - Every field is a pure function of the request plus the once-loaded
          learning insights; nothing here grows as the pipeline progresses.

    Attributes:
        request: The validated pipeline request (carries ``config``,
            ``entry_stage``, ``icp``, case studies, …).
        job_id: The job-store id this run writes status to.
        insights_ctx: The learning-insights prompt block, loaded once in
            ``sales_prepare`` and injected into every agent prompt.
        insights_version: Version of the applied learning insights, for the
            finalize summary note (``None`` when no insights were loaded).
        insights_total_outcomes: Number of outcomes the insights were built
            from; drives the "learning applied" vs "no learning history" note.
        stopped: ``True`` when the job was already terminal at prepare time
            (missing/cancelled/interrupted). The workflow short-circuits the
            entire run when this is set — no stages run, no COMPLETED written.
    """

    request: SalesPipelineRequest
    job_id: str
    insights_ctx: str = ""
    insights_version: Optional[int] = None
    insights_total_outcomes: int = 0
    stopped: bool = False
