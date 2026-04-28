"""Sprint Planner agent — capacity-aware story selection from the backlog.

Phase 2 of #243. The agent is a thin shell over
``ProductDeliveryStore.select_sprint_scope`` — deterministic greedy fit,
no LLM call. Mirroring ``ProductOwnerAgent``'s "internal utility"
treatment, it is not exposed via the Agent Console catalog today.
"""

from product_delivery.sprint_planner_agent.agent import SprintPlannerAgent

__all__ = ["SprintPlannerAgent"]
