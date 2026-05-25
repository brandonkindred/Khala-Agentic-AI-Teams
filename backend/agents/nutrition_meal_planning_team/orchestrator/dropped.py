"""SPEC-007 W7 two-pass guardrail pipeline for the orchestrator.

Replaces the single-pass ``_record_suggestions`` logic with:

1. ``check_recommendation`` on every suggestion.
2. On rejection, ``regenerate_single`` up to ``MAX_REGEN_RETRIES`` times.
3. Still rejected after retries → ``DroppedSuggestion``.
4. Concurrent regeneration across independent suggestions.

Gated by ``NUTRITION_GUARDRAIL`` env var (off by default).
"""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict as _asdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Sequence, Union

from ..guardrail import GUARDRAIL_VERSION, check_recommendation
from ..ingredient_kb import KB_VERSION
from ..models import (
    ClientProfile,
    DroppedSuggestion,
    MealRecommendation,
    MealRecommendationWithId,
)

if TYPE_CHECKING:
    from ..agents.meal_planning_agent import MealPlanningAgent
    from ..shared.guardrail_audit_store import GuardrailAuditStore
    from ..shared.meal_feedback_store import MealFeedbackStore

logger = logging.getLogger(__name__)

MAX_REGEN_RETRIES = 2
_MAX_GUARDRAIL_WORKERS = 8


@dataclass
class RecordedSuggestions:
    """Result of the two-pass guardrail pipeline.

    Invariants:
        ``recorded`` contains only suggestions that passed
        ``check_recommendation``.
        ``dropped`` contains only suggestions that exhausted
        ``MAX_REGEN_RETRIES`` without passing.
        ``len(recorded) + len(dropped) == len(original_suggestions)``.
    """

    recorded: List[MealRecommendationWithId] = field(default_factory=list)
    dropped: List[DroppedSuggestion] = field(default_factory=list)
    flags_by_recommendation: dict[str, list[str]] = field(default_factory=dict)
    restrictions_best_effort: bool = False


def _restriction_snapshot_hash(profile: ClientProfile) -> str:
    payload = profile.restriction_resolution.model_dump_json(exclude_none=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _is_best_effort(profile: ClientProfile) -> bool:
    rr = profile.restriction_resolution
    has_raw = bool(profile.allergies_and_intolerances or profile.dietary_needs)
    return not rr.resolved and has_raw


_Passed = tuple  # (rec_with_id, flags_list)
_Dropped = DroppedSuggestion
_Outcome = Union[_Passed, _Dropped]


def _process_one(
    client_id: str,
    profile: ClientProfile,
    suggestion: MealRecommendation,
    meal_planning_agent: MealPlanningAgent,
    meal_feedback_store: MealFeedbackStore,
    guardrail_audit_store: GuardrailAuditStore,
    snap_hash: str,
) -> _Outcome:
    """Check one suggestion, regenerate on rejection, return outcome.

    Preconditions:
        ``suggestion`` is a valid ``MealRecommendation``.
        ``profile`` carries ``restriction_resolution`` (may be empty for best-effort).
    Postconditions:
        Returns a 2-tuple ``(MealRecommendationWithId, flags_list)`` on pass,
        or a ``DroppedSuggestion`` on exhaustion.
    """
    current_rec = suggestion
    last_result = None

    for attempt in range(MAX_REGEN_RETRIES + 1):
        result = check_recommendation(profile, current_rec)
        last_result = result

        if result.passed:
            parsed_dicts = [_asdict(p) for p in result.parsed_ingredients]
            flag_dicts = [_asdict(f) for f in result.flags]
            rec_id = meal_feedback_store.record_recommendation(
                client_id,
                current_rec.model_dump(),
                guardrail_version=GUARDRAIL_VERSION,
                parsed_ingredients=parsed_dicts,
                flags=flag_dicts,
                restriction_snapshot_hash=snap_hash,
            )
            clinical_flags = [
                f"{v.reason.value}:{v.tag}" if v.tag else v.reason.value for v in result.flags
            ]
            rec_with_id = MealRecommendationWithId(
                **current_rec.model_dump(),
                recommendation_id=rec_id,
                clinical_flags=clinical_flags,
                parsed_ingredients_present=all(
                    p.canonical_id is not None for p in result.parsed_ingredients
                ),
            )
            return (
                rec_with_id,
                [f"{v.reason.value}:{v.tag}" if v.tag else v.reason.value for v in result.flags],
            )

        for v in result.violations:
            try:
                guardrail_audit_store.record_rejection(
                    client_id,
                    current_rec.model_dump(),
                    v.reason.value,
                    guardrail_version=GUARDRAIL_VERSION,
                    ingredient_raw=v.ingredient_raw,
                    canonical_id=v.canonical_id,
                    tag=v.tag,
                    detail=v.detail,
                    kb_version=KB_VERSION,
                )
            except Exception:
                logger.exception("Failed to log guardrail rejection for %s", suggestion.name)

        if attempt < MAX_REGEN_RETRIES:
            replacement = meal_planning_agent.regenerate_single(
                profile, current_rec, result.violations
            )
            if replacement is None:
                break
            current_rec = replacement

    violations: Sequence = last_result.violations if last_result else ()
    return DroppedSuggestion(
        name=suggestion.name,
        reasons=[f"{v.reason.value}:{v.tag}" if v.tag else v.reason.value for v in violations],
        detail=[v.detail for v in violations],
    )


def run_guardrail_pipeline(
    client_id: str,
    profile: ClientProfile,
    suggestions: list[MealRecommendation],
    meal_planning_agent: MealPlanningAgent,
    meal_feedback_store: MealFeedbackStore,
    guardrail_audit_store: GuardrailAuditStore,
) -> RecordedSuggestions:
    """Run the two-pass guardrail pipeline across all suggestions concurrently.

    Preconditions:
        ``is_guardrail_enabled()`` is True (caller gates).
        ``suggestions`` is a non-empty list of ``MealRecommendation``.
    Postconditions:
        Returns ``RecordedSuggestions`` with all suggestions accounted for.
        Individual suggestion failures do not abort the pipeline.
    """
    snap_hash = _restriction_snapshot_hash(profile)
    best_effort = _is_best_effort(profile)

    recorded: list[MealRecommendationWithId] = []
    dropped: list[DroppedSuggestion] = []
    flags_by_rec: dict[str, list[str]] = {}

    with ThreadPoolExecutor(
        max_workers=min(len(suggestions), _MAX_GUARDRAIL_WORKERS) or 1
    ) as executor:
        future_to_idx = {
            executor.submit(
                _process_one,
                client_id,
                profile,
                s,
                meal_planning_agent,
                meal_feedback_store,
                guardrail_audit_store,
                snap_hash,
            ): i
            for i, s in enumerate(suggestions)
        }

        results: dict[int, _Outcome] = {}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception:
                logger.exception(
                    "Guardrail pipeline panic for suggestion %d (%s); dropping",
                    idx,
                    suggestions[idx].name,
                )
                results[idx] = DroppedSuggestion(
                    name=suggestions[idx].name,
                    reasons=["internal_error"],
                    detail=["Unexpected error during guardrail processing"],
                )

    for idx in sorted(results):
        outcome = results[idx]
        if isinstance(outcome, tuple):
            rec_with_id, flag_list = outcome
            recorded.append(rec_with_id)
            if flag_list:
                flags_by_rec[rec_with_id.recommendation_id] = flag_list
        else:
            dropped.append(outcome)

    return RecordedSuggestions(
        recorded=recorded,
        dropped=dropped,
        flags_by_recommendation=flags_by_rec,
        restrictions_best_effort=best_effort,
    )
