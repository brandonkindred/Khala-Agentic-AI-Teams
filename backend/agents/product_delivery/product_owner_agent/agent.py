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
        return GroomResult(
            product_id=product_id,
            method=method,
            ranked=ranked,
            rationale=f"Scored {len(ranked)} stories using {method.upper()}.",
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
        except Exception:
            logger.exception("ProductOwnerAgent: LLM call failed; returning unscored backlog")
            return {"items": []}

    def _compute_ranked(
        self,
        method: GroomMethod,
        stories: list[Story],
        payload: dict[str, Any],
    ) -> tuple[list[RankedBacklogItem], list[tuple[str, float | None, float | None]]]:
        by_id = {s.id: s for s in stories}
        ranked: list[RankedBacklogItem] = []
        persist_rows: list[tuple[str, float | None, float | None]] = []
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            items = []

        for raw in items:
            if not isinstance(raw, dict):
                continue
            sid = raw.get("id")
            story = by_id.get(sid) if isinstance(sid, str) else None
            if story is None:
                continue
            inputs = raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {}
            rationale = str(raw.get("rationale") or "")

            wsjf_value: float | None = None
            rice_value: float | None = None
            try:
                if method == "wsjf":
                    wsjf_value = wsjf_score(
                        WSJFInputs(
                            user_business_value=float(inputs.get("user_business_value", 0)),
                            time_criticality=float(inputs.get("time_criticality", 0)),
                            risk_reduction_or_opportunity_enablement=float(
                                inputs.get("risk_reduction_or_opportunity_enablement", 0)
                            ),
                            job_size=float(
                                inputs.get(
                                    "job_size",
                                    story.estimate_points or 1.0,
                                )
                            ),
                        )
                    )
                    score = wsjf_value
                else:
                    rice_value = rice_score(
                        RICEInputs(
                            reach=float(inputs.get("reach", 0)),
                            impact=float(inputs.get("impact", 0)),
                            confidence=float(inputs.get("confidence", 0)),
                            effort=float(
                                inputs.get(
                                    "effort",
                                    (story.estimate_points or 4.0) / 4.0,
                                )
                            ),
                        )
                    )
                    score = rice_value
            except (TypeError, ValueError):
                logger.warning(
                    "ProductOwnerAgent: malformed inputs for story %s; skipping",
                    story.id,
                )
                continue

            ranked.append(
                RankedBacklogItem(
                    kind="story",
                    id=story.id,
                    title=story.title,
                    score=score,
                    wsjf_score=wsjf_value,
                    rice_score=rice_value,
                    rationale=rationale,
                )
            )
            persist_rows.append((story.id, wsjf_value, rice_value))
        return ranked, persist_rows
