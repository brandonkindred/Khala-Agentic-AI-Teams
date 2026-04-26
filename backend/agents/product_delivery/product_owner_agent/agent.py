"""ProductOwnerAgent — ranks a product's backlog.

Flow:

1. Pull every story under ``product_id`` from the store.
2. Ask the LLM to produce *scoring inputs* (never the score itself; see
   :mod:`product_delivery.product_owner_agent.prompts`).
3. Compute WSJF or RICE scores deterministically via
   :mod:`product_delivery.scoring`.
4. Optionally persist the scores back onto each story row.
5. Return a ranked :class:`GroomResult`.

Tests inject a stub ``llm_client`` (any object with a ``complete_json``
method). In production we use ``llm_service.get_client("product_owner")``.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Protocol

from product_delivery.models import (
    GroomMethod,
    GroomResult,
    RankedBacklogItem,
    Story,
)
from product_delivery.product_owner_agent.prompts import (
    SYSTEM_PROMPT,
    build_user_prompt,
)
from product_delivery.scoring import RICEInputs, WSJFInputs, rice_score, wsjf_score
from product_delivery.store import ProductDeliveryStore

logger = logging.getLogger(__name__)

_MISSING_RATIONALE = "LLM did not score this story; needs manual review."
_MALFORMED_RATIONALE = "LLM emitted malformed scoring inputs; needs manual review."


class LLMScoringUnavailable(RuntimeError):
    """The LLM call inside grooming failed end-to-end (transport, parse,
    or model error). The route maps this to 503 so callers can retry —
    a 200 with all-fallback zero-scores would silently de-prioritise
    the entire backlog during a transient outage.
    """


def _to_float(value: Any, fallback: float) -> float:
    """Coerce an LLM-supplied value to a finite ``float`` with a typed fallback.

    Handles three lossy LLM behaviours:

    * explicit ``null`` → returns the fallback (``float(None)`` would
      otherwise raise ``TypeError`` and skip the whole story);
    * non-finite ``"NaN"`` / ``"Infinity"`` → returns the fallback
      (those would later break Starlette's JSON encoder if persisted);
    * malformed strings → propagates ``ValueError``/``TypeError`` for
      the caller's exception handler to log + skip.
    """
    if value is None:
        return float(fallback)
    coerced = float(value)
    if not math.isfinite(coerced):
        return float(fallback)
    return coerced


def _score_for(
    method: GroomMethod, inputs: dict[str, Any], story: Story
) -> tuple[float, float | None, float | None]:
    """Compute (score, wsjf_value, rice_value) for one story.

    Centralises the WSJF/RICE branching so the loop in ``_compute_ranked``
    doesn't have to. The two unused per-method values stay ``None`` so
    ``RankedBacklogItem`` and ``persist_rows`` keep their typed shape.
    """
    if method == "wsjf":
        wsjf = wsjf_score(
            WSJFInputs(
                user_business_value=_to_float(inputs.get("user_business_value"), 0.0),
                time_criticality=_to_float(inputs.get("time_criticality"), 0.0),
                risk_reduction_or_opportunity_enablement=_to_float(
                    inputs.get("risk_reduction_or_opportunity_enablement"), 0.0
                ),
                job_size=_to_float(inputs.get("job_size"), story.estimate_points or 1.0),
            )
        )
        return wsjf, wsjf, None
    rice = rice_score(
        RICEInputs(
            reach=_to_float(inputs.get("reach"), 0.0),
            impact=_to_float(inputs.get("impact"), 0.0),
            confidence=_to_float(inputs.get("confidence"), 0.0),
            effort=_to_float(inputs.get("effort"), (story.estimate_points or 4.0) / 4.0),
        )
    )
    return rice, None, rice


def _index_payload(payload: Any) -> dict[str, dict[str, Any]]:
    """Build ``{story_id: raw_item}`` from the LLM response.

    Filters out non-dict items + items without a string id, and warns on
    duplicate ids (first occurrence wins so persisted scores stay
    deterministic instead of depending on which copy lands last).
    """
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        sid = raw.get("id")
        if not isinstance(sid, str):
            continue
        if sid in indexed:
            logger.warning(
                "ProductOwnerAgent: duplicate story id %s in LLM payload; ignoring repeat",
                sid,
            )
            continue
        indexed[sid] = raw
    return indexed


class _LLMLike(Protocol):
    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = ...,
        system_prompt: str | None = ...,
        **kwargs: Any,
    ) -> dict[str, Any]: ...


class ProductOwnerAgent:
    """Stateless: depends on a store + an LLM client only."""

    def __init__(
        self,
        store: ProductDeliveryStore,
        llm_client: _LLMLike,
    ) -> None:
        self._store = store
        self._llm = llm_client

    def groom(
        self,
        *,
        product_id: str,
        method: GroomMethod = "wsjf",
        persist: bool = True,
    ) -> GroomResult:
        stories = self._store.list_stories_for_product(product_id)
        if not stories:
            return GroomResult(
                product_id=product_id,
                method=method,
                ranked=[],
                rationale="No stories under this product yet.",
            )

        scoring_payload = self._call_llm(method, stories)
        ranked, persist_rows = self._compute_ranked(method, stories, scoring_payload)

        if persist and persist_rows:
            self._store.bulk_update_story_scores(persist_rows)

        ranked.sort(key=lambda r: r.score, reverse=True)
        # Report the actual scored count (= persist_rows length) — not
        # `len(ranked)`, which also includes synthetic fallback rows for
        # missing/malformed LLM outputs. Otherwise downstream automation
        # that parses the rationale overcounts scoring success.
        scored = len(persist_rows)
        total = len(stories)
        if scored == total:
            rationale = f"Scored {scored} stories using {method.upper()}."
        else:
            rationale = (
                f"Scored {scored}/{total} stories using {method.upper()}; "
                f"{total - scored} surfaced with score=0 for manual review."
            )
        return GroomResult(
            product_id=product_id,
            method=method,
            ranked=ranked,
            rationale=rationale,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _call_llm(self, method: GroomMethod, stories: list[Story]) -> dict[str, Any]:
        stories_payload = json.dumps(
            [
                {
                    "id": s.id,
                    "title": s.title,
                    "user_story": s.user_story,
                    "estimate_points": s.estimate_points,
                    "status": s.status,
                }
                for s in stories
            ],
            indent=2,
        )
        prompt = build_user_prompt(method, stories_payload)
        try:
            return self._llm.complete_json(
                prompt,
                temperature=0.0,
                system_prompt=SYSTEM_PROMPT,
            )
        except Exception as exc:
            # End-to-end LLM failure (network, model, JSON parse). Convert
            # to a typed domain error so the route can return 503 and
            # callers retry, instead of returning 200 with every story
            # zero-scored. Per-story missing/malformed payloads are still
            # handled gracefully downstream in `_compute_ranked`.
            logger.exception("ProductOwnerAgent: LLM call failed; surfacing as 503")
            raise LLMScoringUnavailable(f"LLM scoring call failed: {exc}") from exc

    def _compute_ranked(
        self,
        method: GroomMethod,
        stories: list[Story],
        payload: dict[str, Any],
    ) -> tuple[list[RankedBacklogItem], list[tuple[str, float | None, float | None]]]:
        """Build the ranked list + the persist payload.

        Iterates ``stories`` (each id seen once, no dedup needed) and
        looks up the LLM's scoring inputs by id. Stories the LLM
        omitted get a synthetic ``score=0`` row with a manual-review
        rationale so downstream planning never silently loses work;
        synthetic rows are *not* persisted (the existing scores on the
        row stay intact).
        """
        payload_by_id = _index_payload(payload)
        ranked: list[RankedBacklogItem] = []
        persist_rows: list[tuple[str, float | None, float | None]] = []

        def _fallback(story: Story, rationale: str) -> RankedBacklogItem:
            """Synthetic row for a story we couldn't score. Surfaced so
            downstream planning sees the gap; not persisted, so existing
            scores on the row stay intact."""
            return RankedBacklogItem(
                kind="story",
                id=story.id,
                title=story.title,
                score=0.0,
                wsjf_score=0.0 if method == "wsjf" else None,
                rice_score=0.0 if method == "rice" else None,
                rationale=rationale,
            )

        for story in stories:
            raw = payload_by_id.get(story.id)
            if raw is None:
                logger.warning(
                    "ProductOwnerAgent: story %s missing from LLM output; including with score=0",
                    story.id,
                )
                ranked.append(_fallback(story, _MISSING_RATIONALE))
                continue

            inputs = raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {}
            try:
                score, wsjf_value, rice_value = _score_for(method, inputs, story)
            except (TypeError, ValueError):
                # Malformed inputs (un-coerceable strings, etc.). Surface
                # the story with score=0 + a malformed-inputs rationale
                # rather than dropping it silently — downstream planning
                # would otherwise lose this work item entirely.
                logger.warning(
                    "ProductOwnerAgent: malformed inputs for story %s; including with score=0",
                    story.id,
                )
                ranked.append(_fallback(story, _MALFORMED_RATIONALE))
                continue

            ranked.append(
                RankedBacklogItem(
                    kind="story",
                    id=story.id,
                    title=story.title,
                    score=score,
                    wsjf_score=wsjf_value,
                    rice_score=rice_value,
                    rationale=str(raw.get("rationale") or ""),
                )
            )
            persist_rows.append((story.id, wsjf_value, rice_value))

        return ranked, persist_rows
