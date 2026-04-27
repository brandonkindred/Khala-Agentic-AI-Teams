"""Postgres data-access layer for the Product Delivery team.

Mirrors :mod:`agent_console.store` in shape:

* stateless class — pool lives in ``shared_postgres``;
* one public method per operation, decorated with ``@timed_query``;
* methods translate Postgres errors into typed domain exceptions;
* :func:`get_store` returns the process-wide singleton.

The store does not manage transactions across operations; each method is
its own transaction (``shared_postgres.get_conn`` commits on clean exit,
rolls back on error). Multi-row writes that need atomicity (e.g. the
grooming endpoint persisting scores for many stories) call the store
inside an explicit transaction at the route layer.

Internal layout:

* ``_ROW_SPECS`` is the single source of truth for "what kind of row is
  this": the table name, the parent FK label used in error messages,
  and the Pydantic model used to project a row dict back to a typed
  return value. Both the create-shim methods and the status / score
  update methods key off it.

* ``_*_COLS`` constants hold each table's projection column list once,
  so a typo can't drop a column from one query while leaving another
  query's projection intact.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, TypeVar
from uuid import uuid4

from psycopg import errors as psycopg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Json
from pydantic import BaseModel

from shared_postgres import get_conn, is_postgres_enabled
from shared_postgres.metrics import timed_query

from .models import (
    AcceptanceCriterion,
    BacklogTree,
    Epic,
    EpicNode,
    FeedbackItem,
    Initiative,
    InitiativeNode,
    Product,
    Story,
    StoryNode,
    Task,
)

logger = logging.getLogger(__name__)
_STORE = "product_delivery"


# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------


class ProductDeliveryStorageUnavailable(RuntimeError):
    """Postgres isn't configured, unreachable, or the pool is shut down."""


class UnknownProductDeliveryEntity(LookupError):
    """A foreign-key target (product/initiative/epic/story) does not exist."""


class CrossProductFeedbackLink(ValueError):
    """A feedback item tried to link to a story under a different product.

    The schema's two FKs (``product_id`` and ``linked_story_id``) cannot
    enforce this on their own — the story is reachable only via
    ``epic → initiative → product``. We validate at the store layer so
    triage stays scoped to the right product.
    """


# ---------------------------------------------------------------------------
# Row specs + per-table column constants — single source of truth.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RowSpec:
    table: str
    model: type[BaseModel]
    parent_fk: str  # human label for FK-violation errors


_ROW_SPECS: dict[str, _RowSpec] = {
    "initiative": _RowSpec("product_delivery_initiatives", Initiative, "product"),
    "epic": _RowSpec("product_delivery_epics", Epic, "initiative"),
    "story": _RowSpec("product_delivery_stories", Story, "epic"),
    "task": _RowSpec("product_delivery_tasks", Task, "story"),
    "ac": _RowSpec("product_delivery_acceptance_criteria", AcceptanceCriterion, "story"),
}

_STATUS_KINDS = frozenset({"initiative", "epic", "story", "task"})
_SCORED_KINDS = frozenset({"initiative", "epic", "story"})

# Status string bound (matches ``models.StatusStr``) — store-level
# enforcement so non-route callers can't slip overlong statuses past
# the API validator and break read-side projection later.
_STATUS_MAX_LEN = 40


def _validate_status(value: str) -> str:
    if not value or len(value) > _STATUS_MAX_LEN:
        raise ValueError(f"status must be 1..{_STATUS_MAX_LEN} chars; got {len(value)}")
    return value


def _validate_optional_finite_score(value: float | None, *, label: str) -> float | None:
    """Mirror ``models.FiniteScore`` at the store layer for non-route callers."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number, not a boolean")
    if not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _validate_estimate_points(value: float | None) -> float | None:
    """Mirror ``models.PositiveFiniteEstimate`` at the store layer."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("estimate_points must be a number, not a boolean")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("estimate_points must be a finite positive number")
    return float(value)


_AUDIT_COLS = "author, created_at, updated_at"
_PRODUCT_COLS = f"id, name, description, vision, {_AUDIT_COLS}"
_SCORED_HEAD = "title, summary, status, wsjf_score, rice_score"
_INITIATIVE_COLS = f"id, product_id, {_SCORED_HEAD}, {_AUDIT_COLS}"
_EPIC_COLS = f"id, initiative_id, {_SCORED_HEAD}, {_AUDIT_COLS}"
_STORY_COLS = (
    f"id, epic_id, title, user_story, status, wsjf_score, rice_score, "
    f"estimate_points, {_AUDIT_COLS}"
)
# Same projection as ``_STORY_COLS`` but every column qualified with the
# ``s.`` alias for the ``list_stories_for_product`` JOIN. Maintained
# explicitly (rather than via string rewriting) so substring matches
# inside identifiers like ``epic_id`` can't corrupt the SQL.
_STORY_COLS_ALIASED = (
    "s.id, s.epic_id, s.title, s.user_story, s.status, s.wsjf_score, "
    "s.rice_score, s.estimate_points, s.author, s.created_at, s.updated_at"
)
_TASK_COLS = f"id, story_id, title, description, status, owner, {_AUDIT_COLS}"
_AC_COLS = f"id, story_id, text, satisfied, {_AUDIT_COLS}"
_FEEDBACK_COLS = (
    f"id, product_id, source, raw_payload, severity, status, linked_story_id, {_AUDIT_COLS}"
)


_T = TypeVar("_T", bound=BaseModel)


def _bucket_by(rows: list[dict[str, Any]], key: str, model: type[_T]) -> dict[str, list[_T]]:
    """Group raw row dicts by `parent_id` and validate them through ``model``.

    Used by ``get_backlog_tree`` to assemble the nested tree from the
    flat per-level fetches without a per-parent SQL query.
    """
    buckets: defaultdict[str, list[_T]] = defaultdict(list)
    for row in rows:
        buckets[row[key]].append(model.model_validate(row))
    return buckets


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ProductDeliveryStore:
    """Stateless DAL. Construct once per process; pool is shared."""

    # ------------------------------------------------------------------
    # Generic insert — used by all create_* shims below.
    # ------------------------------------------------------------------

    def _insert(self, kind: str, **fields: Any) -> Any:
        """INSERT a row and return the validated Pydantic model.

        Auto-fills ``id`` (if not supplied), ``created_at``, ``updated_at``.
        Translates a foreign-key violation into ``UnknownProductDeliveryEntity``
        with the parent label from :data:`_ROW_SPECS`.
        """
        spec = _ROW_SPECS[kind]
        now = _now()
        fields.setdefault("id", _new_id())
        fields.setdefault("author", fields.get("author"))  # required by callers
        fields["created_at"] = now
        fields["updated_at"] = now
        cols = ", ".join(fields)
        placeholders = ", ".join(["%s"] * len(fields))
        # Adapt JSONB-typed columns. Currently only `raw_payload` on
        # feedback_items needs it; future kinds add their key to this set.
        adapted = {k: (Json(v) if k == "raw_payload" else v) for k, v in fields.items()}
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {spec.table} ({cols}) VALUES ({placeholders})",
                    list(adapted.values()),
                )
        except psycopg_errors.ForeignKeyViolation as exc:
            raise UnknownProductDeliveryEntity(f"{spec.parent_fk} does not exist") from exc
        return spec.model.model_validate(fields)

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="create_product")
    def create_product(self, *, name: str, description: str, vision: str, author: str) -> Product:
        now = _now()
        pid = _new_id()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO product_delivery_products ({_PRODUCT_COLS})
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (pid, name, description, vision, author, now, now),
            )
        return Product(
            id=pid,
            name=name,
            description=description,
            vision=vision,
            author=author,
            created_at=now,
            updated_at=now,
        )

    @timed_query(store=_STORE, op="list_products")
    def list_products(self) -> list[Product]:
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_PRODUCT_COLS} FROM product_delivery_products ORDER BY created_at DESC"
            )
            return [Product.model_validate(row) for row in cur.fetchall()]

    @timed_query(store=_STORE, op="get_product")
    def get_product(self, product_id: str) -> Product | None:
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_PRODUCT_COLS} FROM product_delivery_products WHERE id = %s",
                (product_id,),
            )
            row = cur.fetchone()
            return Product.model_validate(row) if row else None

    # ------------------------------------------------------------------
    # Initiatives / Epics / Stories / Tasks / Acceptance criteria —
    # thin shims over `_insert`. Public signatures are unchanged so the
    # route layer and tests stay identical.
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="create_initiative")
    def create_initiative(
        self, *, product_id: str, title: str, summary: str, status: str, author: str
    ) -> Initiative:
        return self._insert(
            "initiative",
            product_id=product_id,
            title=title,
            summary=summary,
            status=status,
            author=author,
        )

    @timed_query(store=_STORE, op="create_epic")
    def create_epic(
        self, *, initiative_id: str, title: str, summary: str, status: str, author: str
    ) -> Epic:
        return self._insert(
            "epic",
            initiative_id=initiative_id,
            title=title,
            summary=summary,
            status=status,
            author=author,
        )

    @timed_query(store=_STORE, op="create_story")
    def create_story(
        self,
        *,
        epic_id: str,
        title: str,
        user_story: str,
        status: str,
        estimate_points: float | None,
        author: str,
    ) -> Story:
        return self._insert(
            "story",
            epic_id=epic_id,
            title=title,
            user_story=user_story,
            status=_validate_status(status),
            estimate_points=_validate_estimate_points(estimate_points),
            author=author,
        )

    @timed_query(store=_STORE, op="create_task")
    def create_task(
        self,
        *,
        story_id: str,
        title: str,
        description: str,
        status: str,
        owner: str | None,
        author: str,
    ) -> Task:
        return self._insert(
            "task",
            story_id=story_id,
            title=title,
            description=description,
            status=status,
            owner=owner,
            author=author,
        )

    @timed_query(store=_STORE, op="create_acceptance_criterion")
    def create_acceptance_criterion(
        self, *, story_id: str, text: str, satisfied: bool, author: str
    ) -> AcceptanceCriterion:
        return self._insert(
            "ac",
            story_id=story_id,
            text=text,
            satisfied=satisfied,
            author=author,
        )

    # ------------------------------------------------------------------
    # Status / score updates.
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="update_status")
    def update_status(self, *, kind: str, entity_id: str, status: str) -> bool:
        if kind not in _STATUS_KINDS:
            raise ValueError(f"unknown kind for status update: {kind!r}")
        # Mirror the API-level `StatusStr` bound here so non-route
        # callers can't slip overlong/empty statuses into the row and
        # break read-side projection later.
        status = _validate_status(status)
        table = _ROW_SPECS[kind].table
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE {table} SET status = %s, updated_at = %s WHERE id = %s",
                (status, _now(), entity_id),
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="update_scores")
    def update_scores(
        self,
        *,
        kind: str,
        entity_id: str,
        wsjf_score: float | None,
        rice_score: float | None,
    ) -> bool:
        if kind not in _SCORED_KINDS:
            raise ValueError(f"unknown kind for score update: {kind!r}")
        # Store-level guard against NaN/±Infinity/booleans — mirrors
        # `models.FiniteScore` so non-route callers can't write
        # non-finite values that later break JSON serialisation or
        # corrupt persisted ranking data.
        wsjf_score = _validate_optional_finite_score(wsjf_score, label="wsjf_score")
        rice_score = _validate_optional_finite_score(rice_score, label="rice_score")
        table = _ROW_SPECS[kind].table
        sets: list[str] = []
        params: list[Any] = []
        if wsjf_score is not None:
            sets.append("wsjf_score = %s")
            params.append(wsjf_score)
        if rice_score is not None:
            sets.append("rice_score = %s")
            params.append(rice_score)
        if not sets:
            return False
        sets.append("updated_at = %s")
        params.append(_now())
        params.append(entity_id)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE {table} SET {', '.join(sets)} WHERE id = %s",
                params,
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="bulk_update_story_scores")
    def bulk_update_story_scores(
        self,
        rows: Iterable[tuple[str, float | None, float | None]],
    ) -> int:
        """Persist a batch of (story_id, wsjf, rice) updates in one transaction.

        Uses ``executemany`` so the whole batch ships in a single
        client/server round trip. Returns the number of rows actually
        updated (psycopg's ``rowcount`` after ``executemany`` is the
        sum of per-statement row counts). Validates each per-row score
        the same way ``update_scores`` does.
        """
        rows_list = list(rows)
        if not rows_list:
            return 0
        validated: list[tuple[str, float | None, float | None]] = []
        for sid, w, r in rows_list:
            validated.append(
                (
                    sid,
                    _validate_optional_finite_score(w, label="wsjf_score"),
                    _validate_optional_finite_score(r, label="rice_score"),
                )
            )
        now = _now()
        with self._conn() as conn, conn.cursor() as cur:
            cur.executemany(
                """UPDATE product_delivery_stories
                   SET wsjf_score = COALESCE(%s, wsjf_score),
                       rice_score = COALESCE(%s, rice_score),
                       updated_at = %s
                   WHERE id = %s""",
                [(w, r, now, sid) for sid, w, r in validated],
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # Backlog read — single-product nested tree.
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="get_backlog_tree")
    def get_backlog_tree(self, product_id: str) -> BacklogTree | None:
        """Nested backlog projection for a single product.

        Five queries total — one per level of the hierarchy plus the
        product row — instead of N+1 fan-out. Children are bucketed by
        parent id in Python and the tree is assembled in a single pass.
        Cost is O(rows), not O(stories) round trips, so a product with
        a few hundred stories no longer thrashes the connection pool.
        """
        product = self.get_product(product_id)
        if product is None:
            return None
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""SELECT {_INITIATIVE_COLS}
                   FROM product_delivery_initiatives WHERE product_id = %s
                   ORDER BY created_at""",
                (product_id,),
            )
            initiatives_raw = cur.fetchall()
            if not initiatives_raw:
                return BacklogTree(product=product, initiatives=[])
            initiative_ids = [i["id"] for i in initiatives_raw]

            cur.execute(
                f"""SELECT {_EPIC_COLS}
                   FROM product_delivery_epics
                   WHERE initiative_id = ANY(%s)
                   ORDER BY created_at""",
                (initiative_ids,),
            )
            epics_raw = cur.fetchall()
            epic_ids = [e["id"] for e in epics_raw]

            stories_raw: list[dict[str, Any]] = []
            tasks_raw: list[dict[str, Any]] = []
            acs_raw: list[dict[str, Any]] = []
            if epic_ids:
                cur.execute(
                    f"""SELECT {_STORY_COLS}
                       FROM product_delivery_stories
                       WHERE epic_id = ANY(%s)
                       ORDER BY created_at""",
                    (epic_ids,),
                )
                stories_raw = cur.fetchall()
                story_ids = [s["id"] for s in stories_raw]
                if story_ids:
                    cur.execute(
                        f"""SELECT {_TASK_COLS}
                           FROM product_delivery_tasks
                           WHERE story_id = ANY(%s)
                           ORDER BY created_at""",
                        (story_ids,),
                    )
                    tasks_raw = cur.fetchall()
                    cur.execute(
                        f"""SELECT {_AC_COLS}
                           FROM product_delivery_acceptance_criteria
                           WHERE story_id = ANY(%s)
                           ORDER BY created_at""",
                        (story_ids,),
                    )
                    acs_raw = cur.fetchall()

        # Bucket children by parent id, preserving fetch order (which is
        # already created_at thanks to the ORDER BY above).
        tasks_by_story = _bucket_by(tasks_raw, "story_id", Task)
        acs_by_story = _bucket_by(acs_raw, "story_id", AcceptanceCriterion)
        stories_by_epic: defaultdict[str, list[StoryNode]] = defaultdict(list)
        for srow in stories_raw:
            stories_by_epic[srow["epic_id"]].append(
                StoryNode.model_validate(
                    {
                        **srow,
                        "tasks": tasks_by_story.get(srow["id"], []),
                        "acceptance_criteria": acs_by_story.get(srow["id"], []),
                    }
                )
            )
        epics_by_initiative: defaultdict[str, list[EpicNode]] = defaultdict(list)
        for erow in epics_raw:
            epics_by_initiative[erow["initiative_id"]].append(
                EpicNode.model_validate({**erow, "stories": stories_by_epic.get(erow["id"], [])})
            )
        initiatives = [
            InitiativeNode.model_validate(
                {**irow, "epics": epics_by_initiative.get(irow["id"], [])}
            )
            for irow in initiatives_raw
        ]
        return BacklogTree(product=product, initiatives=initiatives)

    @timed_query(store=_STORE, op="list_stories_for_product")
    def list_stories_for_product(self, product_id: str) -> list[Story]:
        """Flat list of every story under a product. Used by the grooming agent.

        Raises ``UnknownProductDeliveryEntity`` if the product doesn't
        exist (the existence check + the story SELECT run in a single
        connection so concurrent deletes can't make the route return
        ``200 []`` for a missing product).
        """
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT 1 FROM product_delivery_products WHERE id = %s",
                (product_id,),
            )
            if cur.fetchone() is None:
                raise UnknownProductDeliveryEntity(f"unknown product: {product_id}")
            cur.execute(
                f"""SELECT {_STORY_COLS_ALIASED}
                   FROM product_delivery_stories s
                   JOIN product_delivery_epics e ON e.id = s.epic_id
                   JOIN product_delivery_initiatives i ON i.id = e.initiative_id
                   WHERE i.product_id = %s
                   ORDER BY s.created_at""",
                (product_id,),
            )
            return [Story.model_validate(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="create_feedback_item")
    def create_feedback_item(
        self,
        *,
        product_id: str,
        source: str,
        raw_payload: dict[str, Any],
        severity: str,
        linked_story_id: str | None,
        author: str,
    ) -> FeedbackItem:
        # Validate product + cross-product linkage first, then insert. We
        # don't go through `_insert` because feedback_items needs the
        # validation-then-insert sequence inside one transaction (so a
        # concurrent delete of the linking chain can't slip past us).
        # The INSERT is also wrapped in a FK-violation handler so a
        # concurrent delete between validation and insert surfaces as
        # 404, not a raw 500.
        now = _now()
        fid = _new_id()
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM product_delivery_products WHERE id = %s",
                    (product_id,),
                )
                if cur.fetchone() is None:
                    raise UnknownProductDeliveryEntity(f"product {product_id!r} does not exist")
                if linked_story_id is not None:
                    cur.execute(
                        """SELECT i.product_id
                           FROM product_delivery_stories s
                           JOIN product_delivery_epics e ON e.id = s.epic_id
                           JOIN product_delivery_initiatives i ON i.id = e.initiative_id
                           WHERE s.id = %s""",
                        (linked_story_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise UnknownProductDeliveryEntity(
                            f"story {linked_story_id!r} does not exist"
                        )
                    if row[0] != product_id:
                        raise CrossProductFeedbackLink(
                            f"story {linked_story_id!r} belongs to product "
                            f"{row[0]!r}, not {product_id!r}"
                        )
                cur.execute(
                    f"""INSERT INTO product_delivery_feedback_items ({_FEEDBACK_COLS})
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        fid,
                        product_id,
                        source,
                        Json(raw_payload),
                        severity,
                        "open",
                        linked_story_id,
                        author,
                        now,
                        now,
                    ),
                )
        except psycopg_errors.ForeignKeyViolation as exc:
            # Race: a concurrent caller deleted the product or story
            # between the validation SELECT and our INSERT. Surface as
            # the same 404 the eager validation would have produced.
            # Don't leak the raw psycopg/SQL detail to the HTTP client —
            # `from exc` keeps the cause chained for server logs.
            logger.warning(
                "create_feedback_item: FK race for product=%s story=%s: %s",
                product_id,
                linked_story_id,
                exc,
            )
            raise UnknownProductDeliveryEntity(
                "product or linked story disappeared mid-write"
            ) from exc
        return FeedbackItem(
            id=fid,
            product_id=product_id,
            source=source,
            raw_payload=raw_payload,
            severity=severity,
            status="open",
            linked_story_id=linked_story_id,
            author=author,
            created_at=now,
            updated_at=now,
        )

    @timed_query(store=_STORE, op="list_feedback")
    def list_feedback(
        self,
        product_id: str,
        *,
        status: str | None = None,
    ) -> list[FeedbackItem]:
        """List feedback items under a product.

        Raises ``UnknownProductDeliveryEntity`` when the product doesn't
        exist — the existence check + the feedback SELECT run in a
        single connection so a concurrent delete can't make the route
        return ``200 []`` for a missing product.
        """
        sql = f"SELECT {_FEEDBACK_COLS} FROM product_delivery_feedback_items WHERE product_id = %s"
        params: list[Any] = [product_id]
        if status is not None:
            sql += " AND status = %s"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT 1 FROM product_delivery_products WHERE id = %s",
                (product_id,),
            )
            if cur.fetchone() is None:
                raise UnknownProductDeliveryEntity(f"unknown product: {product_id}")
            cur.execute(sql, params)
            return [FeedbackItem.model_validate(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _conn(self):
        if not is_postgres_enabled():
            raise ProductDeliveryStorageUnavailable(
                "POSTGRES_HOST is not configured; product_delivery storage is unavailable."
            )
        try:
            return get_conn()
        except Exception as exc:  # pragma: no cover — infra paths
            raise ProductDeliveryStorageUnavailable(str(exc)) from exc


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _new_id() -> str:
    return uuid4().hex


# Process-wide singleton. The store has no constructor side effects and
# the connection pool lives in ``shared_postgres``, so a module-level
# instance is sufficient — no `lru_cache` dance, no `cache_clear()`
# calls in tests.
_STORE_INSTANCE = ProductDeliveryStore()


def get_store() -> ProductDeliveryStore:
    return _STORE_INSTANCE
