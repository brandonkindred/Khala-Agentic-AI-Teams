"""Pure pipeline gating + routing helpers, shared by the thread orchestrator and
the Temporal workflow.

These functions are the single source of truth for two decisions that both
execution modes must make identically:

- **which stages run** for a given ``entry_stage`` (``stage_should_run``), and
- **how qualified leads route** to advance-vs-nurture (``partition_qualified`` /
  the ``is_advance`` / ``is_disqualify`` predicates).

They live **outside** ``sales_team/temporal/`` on purpose: ``orchestrator.py``
(the thread path) imports them without pulling in ``temporalio``, and the
Temporal workflow imports them under ``workflow.unsafe.imports_passed_through()``
so the exact same rules run in the deterministic workflow sandbox. Every
function here is pure — no I/O, no clock, no randomness — so it is safe to call
from a Temporal workflow and trivially testable.

The module only depends on ``sales_team.models`` (stdlib + pydantic), which is
import-clean (no ``os.getenv`` at import), keeping it sandbox-safe.
"""

from __future__ import annotations

from typing import List, Tuple

from .models import PipelineStage, Prospect, QualificationScore

# The ordered pipeline stages. ``entry_stage`` selects the first stage to run;
# every stage at or after it in this list runs (subject to per-stage data
# gates). The terminal ``CLOSED_WON`` / ``CLOSED_LOST`` states are intentionally
# absent — they are outcomes recorded after the pipeline, not runnable stages,
# so an ``entry_stage`` of either yields "no stages run".
PIPELINE_STAGE_ORDER: List[PipelineStage] = [
    PipelineStage.PROSPECTING,
    PipelineStage.OUTREACH,
    PipelineStage.QUALIFICATION,
    PipelineStage.NURTURING,
    PipelineStage.DISCOVERY,
    PipelineStage.PROPOSAL,
    PipelineStage.NEGOTIATION,
]


def stage_should_run(stage: "PipelineStage | str", entry: "PipelineStage | str") -> bool:
    """Return whether ``stage`` runs when the pipeline enters at ``entry``.

    ``PipelineStage`` is a ``str, Enum``, so a raw string (e.g. the workflow's
    ``request["entry_stage"]``) compares equal to the corresponding enum member
    and indexes ``PIPELINE_STAGE_ORDER`` correctly — callers may pass either.

    Preconditions:
        - none — an unknown stage/entry (including the terminal ``closed_*``
          states) is handled, not asserted.

    Postconditions:
        - Returns ``True`` iff both ``stage`` and ``entry`` are in
          ``PIPELINE_STAGE_ORDER`` and ``stage``'s index is >= ``entry``'s.
        - Returns ``False`` if either value is outside the ordered set.
    """
    try:
        return PIPELINE_STAGE_ORDER.index(stage) >= PIPELINE_STAGE_ORDER.index(entry)
    except ValueError:
        return False


def is_advance(recommended_action: str) -> bool:
    """Whether a qualifier's ``recommended_action`` routes a lead to *advance*.

    Preconditions:
        - ``recommended_action`` is a string (may be empty).

    Postconditions:
        - Returns ``True`` iff the (case-insensitive) action starts with
          ``"advance"``.
    """
    return recommended_action.lower().startswith("advance")


def is_disqualify(recommended_action: str) -> bool:
    """Whether a qualifier's ``recommended_action`` routes a lead to *disqualify*.

    Preconditions:
        - ``recommended_action`` is a string (may be empty).

    Postconditions:
        - Returns ``True`` iff the (case-insensitive) action starts with
          ``"disqualify"``.
    """
    return recommended_action.lower().startswith("disqualify")


def partition_qualified(
    qualified: List[QualificationScore],
    prospects: List[Prospect],
) -> Tuple[List[Prospect], List[Prospect]]:
    """Split scored leads into ``(nurture_prospects, qualified_prospects)``.

    This is the object-based routing used by the thread orchestrator; the
    Temporal workflow applies the identical rule on plain dicts via
    :func:`is_advance` / :func:`is_disqualify`.

    Preconditions:
        - ``qualified`` is a list of ``QualificationScore`` (may be empty).
        - ``prospects`` is the full prospect list the scores were derived from.

    Postconditions:
        - When ``qualified`` is empty, returns ``([], list(prospects))`` — with
          no qualification signal, every prospect is treated as still-qualified
          (matching the pre-decomposition orchestrator behaviour).
        - Otherwise ``qualified_prospects`` are the prospects of ``advance``
          scores and ``nurture_prospects`` are the prospects of scores that are
          neither ``advance`` nor ``disqualify``; ``disqualify`` scores drop out
          of both lists.
    """
    if not qualified:
        return [], list(prospects)
    advance = [q for q in qualified if is_advance(q.recommended_action)]
    nurture_prospects = [
        q.prospect
        for q in qualified
        if not is_advance(q.recommended_action) and not is_disqualify(q.recommended_action)
    ]
    qualified_prospects = [q.prospect for q in advance]
    return nurture_prospects, qualified_prospects
