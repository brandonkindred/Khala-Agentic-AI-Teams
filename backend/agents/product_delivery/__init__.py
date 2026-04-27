"""Product Delivery — persistent backlog, grooming, and feedback intake.

Phase 1 of issue #243 (Principal-Engineer SDLC review, recommendation #1).
The team owns a product hierarchy that survives across SE jobs:

* Postgres-backed ``products → initiatives → epics → stories → tasks``
  plus ``acceptance_criteria`` and ``feedback_items``.
* :class:`ProductOwnerAgent` ranks the backlog with WSJF and RICE.
* CRUD + grooming routes live under ``/api/product-delivery`` (mounted
  in-process by ``unified_api``; this team is *not* a proxy team).

Sprints, releases, the SE pipeline integration, the ReleaseManagerAgent,
and the Agent Console UI tabs ship in follow-up issues.
"""

from product_delivery.author import resolve_author
from product_delivery.models import (
    AcceptanceCriterion,
    BacklogTree,
    Epic,
    FeedbackItem,
    GroomRequest,
    GroomResult,
    Initiative,
    Product,
    RankedBacklogItem,
    Story,
    Task,
)
from product_delivery.scoring import rice_score, wsjf_score
from product_delivery.store import (
    CrossProductFeedbackLink,
    ProductDeliveryStorageUnavailable,
    ProductDeliveryStore,
    UnknownProductDeliveryEntity,
    get_store,
)

__all__ = [
    "AcceptanceCriterion",
    "BacklogTree",
    "CrossProductFeedbackLink",
    "Epic",
    "FeedbackItem",
    "GroomRequest",
    "GroomResult",
    "Initiative",
    "Product",
    "ProductDeliveryStorageUnavailable",
    "ProductDeliveryStore",
    "RankedBacklogItem",
    "Story",
    "Task",
    "UnknownProductDeliveryEntity",
    "get_store",
    "resolve_author",
    "rice_score",
    "wsjf_score",
]
