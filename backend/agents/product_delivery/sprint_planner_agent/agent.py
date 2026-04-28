"""SprintPlannerAgent — capacity-aware story selection.

Picks the highest-WSJF stories from a product's backlog that fit within
a target capacity, then writes them into the sprint as
``product_delivery_sprint_stories`` rows. The whole operation is
deterministic — no LLM call — so the agent is just a thin shell over
:meth:`ProductDeliveryStore.select_sprint_scope`. Keeping the agent as
a separate class (rather than calling the store directly from the
route) leaves room for #371's ``ReleaseManagerAgent`` to follow the
same convention and for a future "explain why this story was skipped"
feature without re-shaping callers.
"""

from __future__ import annotations

import logging

from product_delivery.models import SprintPlanResult
from product_delivery.store import ProductDeliveryStore, UnknownProductDeliveryEntity

logger = logging.getLogger(__name__)


class SprintPlannerAgent:
    """Stateless: depends on a store. No LLM."""

    def __init__(self, store: ProductDeliveryStore) -> None:
        self._store = store

    def plan(
        self,
        *,
        sprint_id: str,
        capacity_points: float | None = None,
    ) -> SprintPlanResult:
        """Run the greedy fit and persist picks into the sprint.

        ``capacity_points``:
          * ``None`` → read the sprint row's stored ``capacity_points``
            (the "default" planning path).
          * float → override the sprint row's capacity for this run.
            Useful for what-if planning before committing to a target
            capacity.

        Raises ``UnknownProductDeliveryEntity`` (→ 404 via the route's
        global handler) when the sprint id is missing.
        """
        if capacity_points is None:
            sprint = self._store.get_sprint(sprint_id)
            if sprint is None:
                raise UnknownProductDeliveryEntity(f"unknown sprint: {sprint_id}")
            capacity_points = sprint.capacity_points
        return self._store.select_sprint_scope(sprint_id=sprint_id, capacity_points=capacity_points)
