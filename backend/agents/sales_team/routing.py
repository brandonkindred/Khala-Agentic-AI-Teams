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

from typing import Any, Callable, Dict, Iterable, List, Mapping, Tuple, TypeVar

from .models import PipelineStage, Prospect, QualificationScore

_T = TypeVar("_T")

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

# Single source of truth for the (entry_pct, exit_pct) progress band each stage
# reports to the job store. Both execution modes read from here — the thread
# orchestrator via ``ctx.update`` and the Temporal workflow via its progress
# gates — so the numbers cannot drift between modes. Keys are plain stage
# strings (``PipelineStage`` values plus the non-stage ``"coaching"`` step) so
# the deterministic workflow can index with raw request strings.
STAGE_PROGRESS: Dict[str, Tuple[int, int]] = {
    "prospecting": (5, 15),
    "outreach": (20, 35),
    "qualification": (40, 50),
    "nurturing": (55, 62),
    "discovery": (65, 75),
    "proposal": (78, 87),
    "negotiation": (90, 95),
    "coaching": (97, 97),
}


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


def _partition(
    qualified: List[_T],
    prospects: List[Any],
    action_of: Callable[[_T], str],
    prospect_of: Callable[[_T], Any],
) -> Tuple[List[Any], List[Any]]:
    """Shared advance/nurture partition core.

    Preconditions:
        - ``action_of``/``prospect_of`` are pure accessors for a score's
          recommended action string and embedded prospect.

    Postconditions:
        - Empty ``qualified`` → ``([], list(prospects))``.
        - Otherwise ``(nurture, advance)`` prospects; ``disqualify`` scores
          drop out of both lists.
    """
    if not qualified:
        return [], list(prospects)
    nurture = [
        prospect_of(q)
        for q in qualified
        if not is_advance(action_of(q)) and not is_disqualify(action_of(q))
    ]
    advance = [prospect_of(q) for q in qualified if is_advance(action_of(q))]
    return nurture, advance


def partition_qualified(
    qualified: List[QualificationScore],
    prospects: List[Prospect],
) -> Tuple[List[Prospect], List[Prospect]]:
    """Split scored leads into ``(nurture_prospects, qualified_prospects)``.

    Object-based variant used by the thread orchestrator; the Temporal
    workflow applies the identical rule on plain dicts via
    :func:`partition_qualified_dicts`. Both delegate to one core so the
    routing rule cannot drift between execution modes.

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
    return _partition(qualified, prospects, lambda q: q.recommended_action, lambda q: q.prospect)


def partition_qualified_dicts(
    qualified: List[Mapping[str, Any]],
    prospects: List[Mapping[str, Any]],
) -> Tuple[List[Any], List[Any]]:
    """Dict-based :func:`partition_qualified` for the Temporal workflow.

    Preconditions:
        - Each entry in ``qualified`` is a serialized ``QualificationScore``
          (has ``recommended_action`` and ``prospect`` keys).

    Postconditions:
        - Identical routing semantics to :func:`partition_qualified` (shared
          core), operating on and returning plain dicts. Pure and
          sandbox-safe — callable from inside a Temporal workflow.
    """
    return _partition(
        qualified, prospects, lambda q: q["recommended_action"], lambda q: q["prospect"]
    )


def index_by_prospect_id(items: Iterable[_T], prospect_of: Callable[[_T], Any]) -> Dict[str, _T]:
    """Index score/proposal-like objects by their embedded prospect's id.

    Preconditions:
        - ``prospect_of`` returns the embedded prospect (object with ``.id``)
          or ``None``.

    Postconditions:
        - Entries whose prospect is missing or has an empty ``id`` are skipped
          (never keyed under ``""``); on duplicate ids the last entry wins,
          matching the pre-decomposition comprehension behaviour.
    """
    out: Dict[str, _T] = {}
    for item in items:
        prospect = prospect_of(item)
        if prospect is not None and prospect.id:
            out[prospect.id] = item
    return out


def index_dicts_by_prospect_id(items: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Dict-based :func:`index_by_prospect_id` for the Temporal workflow.

    Preconditions:
        - Each item is a serialized model with a ``prospect`` key (a dict with
          an ``id`` key, or ``None``).

    Postconditions:
        - Same skip-empty/skip-missing semantics as the object variant; pure
          and sandbox-safe.
    """
    out: Dict[str, Any] = {}
    for item in items:
        prospect = item.get("prospect")
        if prospect and prospect.get("id"):
            out[prospect["id"]] = item
    return out
