"""Release Manager agent — closes the sprint loop (#371 / Phase 3 of #243).

The agent ships a release for a sprint whose planned stories have all
reached a terminal status: it composes markdown notes via the
``technical_writers.release_notes_agent``, writes them under
``plan/releases/<version>.md``, records a ``product_delivery_releases``
row, and promotes Integration / DevOps / QA failures into
``feedback_items`` tagged with the originating sprint so the next groom
sees them as candidate backlog seeds.
"""

from product_delivery.release_manager_agent.agent import ReleaseManagerAgent

__all__ = ["ReleaseManagerAgent"]
