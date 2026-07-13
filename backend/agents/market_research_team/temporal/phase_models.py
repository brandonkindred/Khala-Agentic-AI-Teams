"""Serializable state passed between the market-research workflow and its activities.

The fine-grained ``MarketResearchWorkflow`` fans the pipeline out into one
Temporal activity per stage (and one UX activity per transcript). Rather than
threading the (potentially large) transcript payload through every activity's
input, ``market_research_prepare`` returns a small, **constant-size**
:class:`MarketResearchRunContext` that carries only the mission/human-review
fields the downstream stages need — the transcripts themselves are loaded once
by the ``market_research_ingest`` activity and flow between activities as
explicit ``(source, text)`` / ``InterviewInsight`` arguments.

``MarketResearchRunContext`` is a Pydantic model, so it round-trips across the
activity/workflow boundary via ``model_dump(mode="json")`` / ``model_validate``
— the same convention the sales and software-engineering teams' ``phase_models``
use.
"""

from __future__ import annotations

from pydantic import BaseModel

from market_research_team.models import RunMarketResearchRequest


class MarketResearchRunContext(BaseModel):
    """Immutable, constant-size per-run context handed to every stage activity.

    Built once by ``market_research_prepare`` and passed verbatim to the
    downstream activities, which reconstruct a ``ResearchMission`` /
    ``HumanReview`` from ``request`` via ``pipeline.prepare``.

    Invariants:
        - ``request`` has its ``transcripts`` / ``transcript_folder_path``
          stripped: the (possibly large) transcript payload is loaded once by
          ``market_research_ingest`` and never re-carried inside every
          activity's ctx, which would otherwise amplify workflow history.
        - Nothing here grows as the pipeline progresses — the accumulating
          result (insights, signals, recommendation, scripts) lives in the
          workflow's own state, not the ctx.

    Attributes:
        request: The validated run request with transcripts stripped (carries
            ``product_concept``/``target_users``/``business_goal``/``topology``
            and the ``human_approved``/``human_feedback`` gate).
        job_id: The job-store id this run writes status to.
        stopped: ``True`` when the job was already terminal at prepare time
            (missing/cancelled/interrupted/completed). The workflow
            short-circuits the whole run when this is set — no stages run, no
            COMPLETED written.
    """

    request: RunMarketResearchRequest
    job_id: str
    stopped: bool = False
