"""Postgres data-access layer for the Product Delivery team.

Mirrors :mod:`agent_console.store` in shape:

* stateless class — pool lives in ``shared_postgres``;
* one public method per operation, decorated with ``@timed_query``;
* methods translate Postgres errors into typed domain exceptions;
* :func:`get_store` returns a process-wide singleton via ``lru_cache``.

The store does not manage transactions across operations; each method is
its own transaction (``shared_postgres.get_conn`` commits on clean exit,
rolls back on error). Multi-row writes that need atomicity (e.g. the
grooming endpoint persisting scores for many stories) call the store
inside an explicit transaction at the route layer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Iterable
from uuid import uuid4

from psycopg import errors as psycopg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Json

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


class ProductDeliveryStorageUnavailable(RuntimeError):
    """Postgres isn't configured, unreachable, or the pool is shut down."""


class UnknownProductDeliveryEntity(LookupError):
    """A foreign-key target (product/initiative/epic/story) does not exist."""


class ProductDeliveryStore:
    """Stateless DAL. Construct once per process; pool is shared."""

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="create_product")
    def create_product(self, *, name: str, description: str, vision: str, author: str) -> Product:
        now = _now()
        pid = _new_id()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO product_delivery_products
                      (id, name, description, vision, author, created_at, updated_at)
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
                """SELECT id, name, description, vision, author, created_at, updated_at
                   FROM product_delivery_products
                   ORDER BY created_at DESC"""
            )
            return [Product.model_validate(row) for row in cur.fetchall()]

    @timed_query(store=_STORE, op="get_product")
    def get_product(self, product_id: str) -> Product | None:
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT id, name, description, vision, author, created_at, updated_at
                   FROM product_delivery_products WHERE id = %s""",
                (product_id,),
            )
            row = cur.fetchone()
            return Product.model_validate(row) if row else None

    # ------------------------------------------------------------------
    # Initiatives / Epics / Stories — uniform CRUD via _create_scored.
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="create_initiative")
    def create_initiative(
        self,
        *,
        product_id: str,
        title: str,
        summary: str,
        status: str,
        author: str,
    ) -> Initiative:
        now = _now()
        iid = _new_id()
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO product_delivery_initiatives
                          (id, product_id, title, summary, status, author,
                           created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (iid, product_id, title, summary, status, author, now, now),
                )
        except psycopg_errors.ForeignKeyViolation as exc:
            raise UnknownProductDeliveryEntity(f"product {product_id!r} does not exist") from exc
        return Initiative(
            id=iid,
            product_id=product_id,
            title=title,
            summary=summary,
            status=status,
            author=author,
            created_at=now,
            updated_at=now,
        )

    @timed_query(store=_STORE, op="create_epic")
    def create_epic(
        self,
        *,
        initiative_id: str,
        title: str,
        summary: str,
        status: str,
        author: str,
    ) -> Epic:
        now = _now()
        eid = _new_id()
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO product_delivery_epics
                          (id, initiative_id, title, summary, status, author,
                           created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (eid, initiative_id, title, summary, status, author, now, now),
                )
        except psycopg_errors.ForeignKeyViolation as exc:
            raise UnknownProductDeliveryEntity(
                f"initiative {initiative_id!r} does not exist"
            ) from exc
        return Epic(
            id=eid,
            initiative_id=initiative_id,
            title=title,
            summary=summary,
            status=status,
            author=author,
            created_at=now,
            updated_at=now,
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
        now = _now()
        sid = _new_id()
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO product_delivery_stories
                          (id, epic_id, title, user_story, status,
                           estimate_points, author, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        sid,
                        epic_id,
                        title,
                        user_story,
                        status,
                        estimate_points,
                        author,
                        now,
                        now,
                    ),
                )
        except psycopg_errors.ForeignKeyViolation as exc:
            raise UnknownProductDeliveryEntity(f"epic {epic_id!r} does not exist") from exc
        return Story(
            id=sid,
            epic_id=epic_id,
            title=title,
            user_story=user_story,
            status=status,
            estimate_points=estimate_points,
            author=author,
            created_at=now,
            updated_at=now,
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
        now = _now()
        tid = _new_id()
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO product_delivery_tasks
                          (id, story_id, title, description, status, owner,
                           author, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (tid, story_id, title, description, status, owner, author, now, now),
                )
        except psycopg_errors.ForeignKeyViolation as exc:
            raise UnknownProductDeliveryEntity(f"story {story_id!r} does not exist") from exc
        return Task(
            id=tid,
            story_id=story_id,
            title=title,
            description=description,
            status=status,
            owner=owner,
            author=author,
            created_at=now,
            updated_at=now,
        )

    @timed_query(store=_STORE, op="create_acceptance_criterion")
    def create_acceptance_criterion(
        self,
        *,
        story_id: str,
        text: str,
        satisfied: bool,
        author: str,
    ) -> AcceptanceCriterion:
        now = _now()
        aid = _new_id()
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO product_delivery_acceptance_criteria
                          (id, story_id, text, satisfied, author,
                           created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (aid, story_id, text, satisfied, author, now, now),
                )
        except psycopg_errors.ForeignKeyViolation as exc:
            raise UnknownProductDeliveryEntity(f"story {story_id!r} does not exist") from exc
        return AcceptanceCriterion(
            id=aid,
            story_id=story_id,
            text=text,
            satisfied=satisfied,
            author=author,
            created_at=now,
            updated_at=now,
        )

    # ------------------------------------------------------------------
    # Status / score updates — generic helpers keep route code small.
    # ------------------------------------------------------------------

    _SCORED_TABLES = {
        "initiative": "product_delivery_initiatives",
        "epic": "product_delivery_epics",
        "story": "product_delivery_stories",
    }
    _STATUS_TABLES = {
        **_SCORED_TABLES,
        "task": "product_delivery_tasks",
    }

    @timed_query(store=_STORE, op="update_status")
    def update_status(self, *, kind: str, entity_id: str, status: str) -> bool:
        table = self._STATUS_TABLES.get(kind)
        if table is None:
            raise ValueError(f"unknown kind for status update: {kind!r}")
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
        table = self._SCORED_TABLES.get(kind)
        if table is None:
            raise ValueError(f"unknown kind for score update: {kind!r}")
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

    def bulk_update_story_scores(
        self,
        rows: Iterable[tuple[str, float | None, float | None]],
    ) -> int:
        """Persist a batch of (story_id, wsjf, rice) updates in one transaction.

        Returns the number of rows actually updated. Used by the grooming
        route so a re-groom is atomic from the client's perspective.
        """
        rows_list = list(rows)
        if not rows_list:
            return 0
        now = _now()
        with self._conn() as conn, conn.cursor() as cur:
            updated = 0
            for sid, wsjf, rice in rows_list:
                cur.execute(
                    """UPDATE product_delivery_stories
                       SET wsjf_score = COALESCE(%s, wsjf_score),
                           rice_score = COALESCE(%s, rice_score),
                           updated_at = %s
                       WHERE id = %s""",
                    (wsjf, rice, now, sid),
                )
                updated += cur.rowcount
            return updated

    # ------------------------------------------------------------------
    # Backlog read — single-product nested tree.
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="get_backlog_tree")
    def get_backlog_tree(self, product_id: str) -> BacklogTree | None:
        product = self.get_product(product_id)
        if product is None:
            return None
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT id, product_id, title, summary, status, wsjf_score, rice_score,
                          author, created_at, updated_at
                   FROM product_delivery_initiatives WHERE product_id = %s
                   ORDER BY created_at""",
                (product_id,),
            )
            initiatives_raw = cur.fetchall()
            initiatives: list[InitiativeNode] = []
            for irow in initiatives_raw:
                cur.execute(
                    """SELECT id, initiative_id, title, summary, status, wsjf_score, rice_score,
                              author, created_at, updated_at
                       FROM product_delivery_epics WHERE initiative_id = %s
                       ORDER BY created_at""",
                    (irow["id"],),
                )
                epics_raw = cur.fetchall()
                epics: list[EpicNode] = []
                for erow in epics_raw:
                    cur.execute(
                        """SELECT id, epic_id, title, user_story, status, wsjf_score, rice_score,
                                  estimate_points, author, created_at, updated_at
                           FROM product_delivery_stories WHERE epic_id = %s
                           ORDER BY created_at""",
                        (erow["id"],),
                    )
                    stories_raw = cur.fetchall()
                    stories: list[StoryNode] = []
                    for srow in stories_raw:
                        cur.execute(
                            """SELECT id, story_id, title, description, status, owner,
                                      author, created_at, updated_at
                               FROM product_delivery_tasks WHERE story_id = %s
                               ORDER BY created_at""",
                            (srow["id"],),
                        )
                        tasks = [Task.model_validate(t) for t in cur.fetchall()]
                        cur.execute(
                            """SELECT id, story_id, text, satisfied, author,
                                      created_at, updated_at
                               FROM product_delivery_acceptance_criteria
                               WHERE story_id = %s ORDER BY created_at""",
                            (srow["id"],),
                        )
                        acs = [AcceptanceCriterion.model_validate(a) for a in cur.fetchall()]
                        stories.append(
                            StoryNode.model_validate(
                                {**srow, "tasks": tasks, "acceptance_criteria": acs}
                            )
                        )
                    epics.append(EpicNode.model_validate({**erow, "stories": stories}))
                initiatives.append(InitiativeNode.model_validate({**irow, "epics": epics}))
        return BacklogTree(product=product, initiatives=initiatives)

    @timed_query(store=_STORE, op="list_stories_for_product")
    def list_stories_for_product(self, product_id: str) -> list[Story]:
        """Flat list of every story under a product. Used by the grooming agent."""
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT s.id, s.epic_id, s.title, s.user_story, s.status,
                          s.wsjf_score, s.rice_score, s.estimate_points,
                          s.author, s.created_at, s.updated_at
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
        now = _now()
        fid = _new_id()
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO product_delivery_feedback_items
                          (id, product_id, source, raw_payload, severity, status,
                           linked_story_id, author, created_at, updated_at)
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
            raise UnknownProductDeliveryEntity(f"product {product_id!r} does not exist") from exc
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
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            if status is None:
                cur.execute(
                    """SELECT id, product_id, source, raw_payload, severity, status,
                              linked_story_id, author, created_at, updated_at
                       FROM product_delivery_feedback_items
                       WHERE product_id = %s
                       ORDER BY created_at DESC""",
                    (product_id,),
                )
            else:
                cur.execute(
                    """SELECT id, product_id, source, raw_payload, severity, status,
                              linked_story_id, author, created_at, updated_at
                       FROM product_delivery_feedback_items
                       WHERE product_id = %s AND status = %s
                       ORDER BY created_at DESC""",
                    (product_id, status),
                )
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


@lru_cache(maxsize=1)
def get_store() -> ProductDeliveryStore:
    return ProductDeliveryStore()
