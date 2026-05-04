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

import psycopg
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
    Release,
    Sprint,
    SprintPlanResult,
    SprintWithStories,
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


class CrossProductSprintAssignment(ValueError):
    """A story was added to a sprint under a different product.

    Same shape as :class:`CrossProductFeedbackLink`: the two FKs on
    ``product_delivery_sprint_stories`` (``sprint_id`` → sprint and
    ``story_id`` → story) can't enforce that the story's owning product
    matches the sprint's. Validated at the store layer so a sprint-
    scoped SE run can't pull in unrelated work via a manual assignment.
    Mapped to HTTP 400 by the route's exception handler.
    """


class StoryAlreadyPlanned(ValueError):
    """A story was already planned into another sprint.

    Phase 2 of #243 enforces a one-sprint-per-story invariant via
    ``UNIQUE(story_id)`` on ``product_delivery_sprint_stories``.
    Two concurrent ``select_sprint_scope`` calls could both pass the
    ``NOT EXISTS`` candidate filter and race to insert the same story —
    the unique index closes that window, and the violation lands here.
    Mapped to HTTP 409 at the route layer.
    """


class DuplicateReleaseVersion(ValueError):
    """A release row already exists for the same ``(sprint_id, version)`` pair.

    Phase 3 of #243 / PR #424 Codex review. Schema-level
    ``UNIQUE(sprint_id, version)`` on ``product_delivery_releases``
    enforces the audit invariant that a version string is the
    on-disk filename for ``plan/releases/<version>.md``. Two ships
    racing on the same explicit version, or an operator retrying
    ``POST /releases`` with the same version, surface here so the
    route returns 409 (re-pick a fresh version) instead of silently
    creating a second row that points at the same notes file.
    """


class SprintNotComplete(ValueError):
    """A release was requested for a sprint that still has open stories.

    Phase 3 of #243 / issue #371. The ReleaseManagerAgent guards on
    ``count_open_stories_in_sprint(sprint_id) == 0`` before writing a
    release row + notes file; trying to ship a sprint whose planned
    stories haven't all reached a terminal status raises this so the
    SE-pipeline hook can log + skip without poisoning the release
    history with a "shipped" record for incomplete work. Mapped to
    HTTP 409 at the route layer.
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


# Title bound — matches every ``*Create.title``/``ProductCreate.name``
# Pydantic field on the API side. Store-level mirror so non-route
# callers can't slip empty/oversized identifiers past validation and
# break read-side projection later.
_TITLE_MAX_LEN = 200


def _validate_status(value: Any) -> str:
    # `isinstance(value, str)` first: non-route callers handing in a
    # non-string (int, dict, None) used to trip ``len(value)`` with a
    # raw ``TypeError`` that fell past the route's exception map and
    # surfaced as 500. Surface a domain ``ValueError`` instead so the
    # route translates it cleanly to 422.
    #
    # Then strip+normalise: the API-side `_reject_blank_str` now
    # *strips* leading/trailing whitespace too, so accidental inputs
    # like ``"open "`` no longer become a distinct persisted state
    # that misses exact-match filters (``GET /feedback?status=open``).
    if not isinstance(value, str):
        raise ValueError(f"status must be a string; got {type(value).__name__}")
    stripped = value.strip()
    if not stripped or len(stripped) > _STATUS_MAX_LEN:
        raise ValueError(
            f"status must be 1..{_STATUS_MAX_LEN} non-blank chars; got {len(stripped)}"
        )
    return stripped


def _validate_title(value: Any, *, label: str = "title") -> str:
    """Mirror the API-level ``min_length=1, max_length=200`` bound.

    Used by ``create_product`` / ``create_initiative`` / ``create_epic``
    / ``create_story`` / ``create_task`` so internal callers (the
    ProductOwnerAgent, future workflow code, etc.) can't bypass route
    validation and persist empty / oversized / whitespace-only titles
    that the HTTP contract rejects. Strips leading/trailing whitespace
    so ``"S "`` and ``"S"`` don't diverge between API and store paths.
    """
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string; got {type(value).__name__}")
    stripped = value.strip()
    if not stripped or len(stripped) > _TITLE_MAX_LEN:
        raise ValueError(
            f"{label} must be 1..{_TITLE_MAX_LEN} non-blank chars; got {len(stripped)}"
        )
    return stripped


def _validate_text(value: Any, *, label: str, max_len: int | None = None) -> str:
    """Generic non-blank string validator used by feedback ``source`` /
    acceptance-criterion ``text``.

    Matches the API-side ``_reject_blank_str`` contract: rejects empty,
    rejects non-strings (so non-route callers handing in ``None`` or
    a dict don't trigger ``TypeError`` past the route's exception map),
    rejects whitespace-only, and strips leading/trailing whitespace
    on the way out. ``max_len`` mirrors the API ``Field(max_length=…)``
    when set; ``None`` means "no upper bound" (acceptance-criterion
    text is unbounded at the API).
    """
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string; got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{label} must not be blank or whitespace-only")
    if max_len is not None and len(stripped) > max_len:
        raise ValueError(f"{label} must be at most {max_len} chars; got {len(stripped)}")
    return stripped


_FEEDBACK_SOURCE_MAX_LEN = 120

# Story statuses that the sprint planner treats as terminal — finished
# or abandoned work that shouldn't be re-planned into a future sprint.
# Status is a free-form ``TEXT`` column (no enum) so we use a defensive
# default set that covers the conventions across the codebase
# (`done`/`completed`/`cancelled`/`closed`). Phase 2 ships lowercase
# values; PATCH /status normalises whitespace but not case, so callers
# storing `Done` instead of `done` would slip past — keeping this
# strict-lowercase for now matches the rest of the team's data and a
# future status-enum migration can normalise on the way in.
#
# Exposed as a public name so cross-team callers (e.g. the SE
# orchestrator's synthesized-spec path) use the same definition the
# planner does — keeps planning vs. execution behavior in lockstep.
TERMINAL_STORY_STATUSES: frozenset[str] = frozenset({"done", "completed", "cancelled", "closed"})
_TERMINAL_STORY_STATUSES = TERMINAL_STORY_STATUSES  # backwards-compatible alias


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
    "id, product_id, source, raw_payload, severity, status, linked_story_id, "
    f"sprint_id, {_AUDIT_COLS}"
)
_SPRINT_COLS = f"id, product_id, name, capacity_points, starts_at, ends_at, status, {_AUDIT_COLS}"
_RELEASE_COLS = f"id, sprint_id, version, notes_path, shipped_at, {_AUDIT_COLS}"
# Alias-qualified projection for ``list_releases_for_product``'s JOIN
# (every column exists on both ``product_delivery_releases`` and
# ``product_delivery_sprints`` after the join, so unqualified names
# would surface a "column reference is ambiguous" error from psycopg).
_RELEASE_COLS_ALIASED = (
    "r.id, r.sprint_id, r.version, r.notes_path, r.shipped_at, r.author, r.created_at, r.updated_at"
)


def _validate_capacity_points(value: float | None) -> float:
    """Mirror ``models.CapacityPoints`` at the store layer.

    ``None`` collapses to 0.0 so internal callers (e.g. the route layer
    passing through a default-less request body) don't have to repeat
    the dance. ``select_sprint_scope`` already treats 0.0 as "fit
    nothing", which is the safe default — the alternative (raising on
    None) would break ergonomic use from the planner agent.
    """
    if value is None:
        return 0.0
    if isinstance(value, bool):
        raise ValueError("capacity_points must be a number, not a boolean")
    if not math.isfinite(value):
        raise ValueError("capacity_points must be a finite number")
    if value < 0:
        raise ValueError("capacity_points must be >= 0")
    return float(value)


def _validate_sprint_window(starts_at: datetime | None, ends_at: datetime | None) -> None:
    """Mirror ``CreateSprintRequest._validate_window`` at the store layer.

    Codex flagged that non-route callers (workflow code, future
    backfill scripts) could persist inverted windows that the API
    contract rejects, and downstream code assuming chronological
    windows would silently misbehave. Equal timestamps are tolerated
    — same call as the model validator.

    The model uses ``AwareDatetime`` so the route layer rejects naive
    timestamps with a 422; this helper repeats the tz-awareness check
    per-endpoint so a non-route caller passing a single-ended naive
    bound (e.g. ``starts_at=datetime.utcnow(), ends_at=None``) still
    raises a typed ``ValueError`` instead of silently inserting and
    then crashing in the post-commit ``Sprint(...)`` validation
    (Codex review on PR #396 — that path leaked invalid persisted
    rows because the early-return ``return when None`` skipped the
    naive-bound validation entirely).
    """
    if starts_at is not None and starts_at.tzinfo is None:
        raise ValueError("starts_at must be timezone-aware")
    if ends_at is not None and ends_at.tzinfo is None:
        raise ValueError("ends_at must be timezone-aware")
    if starts_at is not None and ends_at is not None and ends_at < starts_at:
        raise ValueError("ends_at must be on or after starts_at")


_T = TypeVar("_T", bound=BaseModel)


def _begin_repeatable_read(cur: Any) -> None:
    """Pin the current transaction to ``REPEATABLE READ`` snapshot isolation.

    psycopg3 in non-autocommit mode auto-begins a transaction on the
    first statement; ``SET TRANSACTION ISOLATION LEVEL …`` issued before
    any data-touching statement applies to that transaction. Used by
    the multi-statement read methods (``get_backlog_tree``,
    ``list_stories_for_product``, ``list_feedback``) so an existence
    check + the matching read see the same snapshot — under the
    default ``READ COMMITTED`` each statement gets its own snapshot
    and a concurrent delete between them can return ``200 []`` for a
    product that no longer exists.
    """
    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")


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
        # Mirror `ProductCreate(name=Field(min_length=1, max_length=200))`
        # so non-route callers can't persist empty / oversized names
        # that the HTTP contract rejects.
        name = _validate_title(name, label="name")
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
            title=_validate_title(title),
            summary=summary,
            status=_validate_status(status),
            author=author,
        )

    @timed_query(store=_STORE, op="create_epic")
    def create_epic(
        self, *, initiative_id: str, title: str, summary: str, status: str, author: str
    ) -> Epic:
        return self._insert(
            "epic",
            initiative_id=initiative_id,
            title=_validate_title(title),
            summary=summary,
            status=_validate_status(status),
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
            title=_validate_title(title),
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
            title=_validate_title(title),
            description=description,
            status=_validate_status(status),
            owner=owner,
            author=author,
        )

    @timed_query(store=_STORE, op="create_acceptance_criterion")
    def create_acceptance_criterion(
        self, *, story_id: str, text: str, satisfied: bool, author: str
    ) -> AcceptanceCriterion:
        # Mirror the API-side `_reject_blank_str` so non-route callers
        # can't persist whitespace-only criteria that would inflate
        # criterion counts and undermine the satisfied/total ratio.
        return self._insert(
            "ac",
            story_id=story_id,
            text=_validate_text(text, label="text"),
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

        Five queries total — product row + one per level of the hierarchy
        — instead of N+1 fan-out. Children are bucketed by parent id in
        Python and the tree is assembled in a single pass. Cost is
        O(rows), not O(stories) round trips, so a product with a few
        hundred stories no longer thrashes the connection pool.

        All five SELECTs run inside a single ``REPEATABLE READ``
        transaction so the projection comes from one consistent snapshot
        — otherwise concurrent writes between the per-level fetches
        could yield stale product metadata next to vanished children
        (or vice versa).
        """
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            _begin_repeatable_read(cur)
            cur.execute(
                f"SELECT {_PRODUCT_COLS} FROM product_delivery_products WHERE id = %s",
                (product_id,),
            )
            product_row = cur.fetchone()
            if product_row is None:
                return None
            product = Product.model_validate(product_row)
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
        exist. The existence check + the story SELECT run inside a
        single ``REPEATABLE READ`` transaction so they observe the same
        snapshot — under the default ``READ COMMITTED`` a concurrent
        delete between the two statements could otherwise return
        ``200 []`` for a missing product.
        """
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            _begin_repeatable_read(cur)
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
        sprint_id: str | None = None,
    ) -> FeedbackItem:
        # Validate product + cross-product linkage first, then insert. We
        # don't go through `_insert` because feedback_items needs the
        # validation-then-insert sequence inside one transaction (so a
        # concurrent delete of the linking chain can't slip past us).
        # The INSERT is also wrapped in a FK-violation handler so a
        # concurrent delete between validation and insert surfaces as
        # 404, not a raw 500.
        #
        # Mirror the API-side validators so non-route callers can't
        # bypass the HTTP contract:
        #   * `source` — `min_length=1, max_length=120, non-blank`
        #   * `raw_payload` — must be a dict (it's serialised as JSONB,
        #     and `FeedbackItem.raw_payload: dict[str, Any]` will fail
        #     model-validate on a non-dict, but only *after* the row is
        #     committed — a 500 with a poisoned row already in the DB).
        source = _validate_text(source, label="source", max_len=_FEEDBACK_SOURCE_MAX_LEN)
        if not isinstance(raw_payload, dict):
            raise ValueError(f"raw_payload must be a dict; got {type(raw_payload).__name__}")
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
                # Validate sprint provenance up front so a bad id doesn't
                # silently land as a NULL FK after ON DELETE SET NULL —
                # we want the caller to see the 404, not a row with
                # missing context. Also enforce that the sprint belongs
                # to the same product (Codex review on PR #424): the
                # schema FKs only constrain the two ids individually,
                # so without this check a feedback row for product A
                # could be tagged with a sprint under product B,
                # polluting sprint-scoped grooming and reporting.
                if sprint_id is not None:
                    cur.execute(
                        "SELECT product_id FROM product_delivery_sprints WHERE id = %s",
                        (sprint_id,),
                    )
                    sprint_row = cur.fetchone()
                    if sprint_row is None:
                        raise UnknownProductDeliveryEntity(f"sprint {sprint_id!r} does not exist")
                    if sprint_row[0] != product_id:
                        raise CrossProductFeedbackLink(
                            f"sprint {sprint_id!r} belongs to product "
                            f"{sprint_row[0]!r}, not {product_id!r}"
                        )
                cur.execute(
                    f"""INSERT INTO product_delivery_feedback_items ({_FEEDBACK_COLS})
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        fid,
                        product_id,
                        source,
                        Json(raw_payload),
                        severity,
                        "open",
                        linked_story_id,
                        sprint_id,
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
            sprint_id=sprint_id,
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
        exist. The existence check + the feedback SELECT run inside a
        single ``REPEATABLE READ`` transaction so they observe the same
        snapshot — under the default ``READ COMMITTED`` a concurrent
        delete between the two statements could otherwise return
        ``200 []`` for a missing product.

        ``status`` is normalised through ``_validate_status`` (strip +
        non-blank + length bound) before the SQL filter so a query like
        ``GET /feedback?status=open%20`` (trailing space) matches stored
        rows whose status was likewise normalised on write. Without this,
        the filter did exact matching against the raw query input and
        returned an empty list even when matching rows existed —
        Codex flagged this as a triage-view false-negative.
        """
        sql = f"SELECT {_FEEDBACK_COLS} FROM product_delivery_feedback_items WHERE product_id = %s"
        params: list[Any] = [product_id]
        if status is not None:
            sql += " AND status = %s"
            params.append(_validate_status(status))
        sql += " ORDER BY created_at DESC"
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            _begin_repeatable_read(cur)
            cur.execute(
                "SELECT 1 FROM product_delivery_products WHERE id = %s",
                (product_id,),
            )
            if cur.fetchone() is None:
                raise UnknownProductDeliveryEntity(f"unknown product: {product_id}")
            cur.execute(sql, params)
            return [FeedbackItem.model_validate(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Sprints (Phase 2 of #243)
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="create_sprint")
    def create_sprint(
        self,
        *,
        product_id: str,
        name: str,
        capacity_points: float | None,
        starts_at: datetime | None,
        ends_at: datetime | None,
        status: str,
        author: str,
    ) -> Sprint:
        # Mirror the API-side bounds at the store boundary so non-route
        # callers can't slip past validation. Same pattern as `create_story`.
        name = _validate_title(name, label="name")
        status = _validate_status(status)
        capacity = _validate_capacity_points(capacity_points)
        _validate_sprint_window(starts_at, ends_at)
        now = _now()
        sid = _new_id()
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    f"""INSERT INTO product_delivery_sprints ({_SPRINT_COLS})
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        sid,
                        product_id,
                        name,
                        capacity,
                        starts_at,
                        ends_at,
                        status,
                        author,
                        now,
                        now,
                    ),
                )
        except psycopg_errors.ForeignKeyViolation as exc:
            raise UnknownProductDeliveryEntity(f"product {product_id!r} does not exist") from exc
        return Sprint(
            id=sid,
            product_id=product_id,
            name=name,
            capacity_points=capacity,
            starts_at=starts_at,
            ends_at=ends_at,
            status=status,
            author=author,
            created_at=now,
            updated_at=now,
        )

    @timed_query(store=_STORE, op="get_sprint")
    def get_sprint(self, sprint_id: str) -> Sprint | None:
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_SPRINT_COLS} FROM product_delivery_sprints WHERE id = %s",
                (sprint_id,),
            )
            row = cur.fetchone()
            return Sprint.model_validate(row) if row else None

    @timed_query(store=_STORE, op="list_sprints_for_product")
    def list_sprints_for_product(self, product_id: str) -> list[Sprint]:
        """List sprints under a product, newest first.

        Single transaction with the existence check + the SELECT (same
        ``REPEATABLE READ`` pattern as ``list_stories_for_product``) so a
        concurrent product delete can't slip past as ``200 []``.
        """
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            _begin_repeatable_read(cur)
            cur.execute(
                "SELECT 1 FROM product_delivery_products WHERE id = %s",
                (product_id,),
            )
            if cur.fetchone() is None:
                raise UnknownProductDeliveryEntity(f"unknown product: {product_id}")
            cur.execute(
                f"""SELECT {_SPRINT_COLS}
                   FROM product_delivery_sprints
                   WHERE product_id = %s
                   ORDER BY created_at DESC""",
                (product_id,),
            )
            return [Sprint.model_validate(row) for row in cur.fetchall()]

    @timed_query(store=_STORE, op="update_sprint_status")
    def update_sprint_status(self, *, sprint_id: str, status: str) -> bool:
        status = _validate_status(status)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE product_delivery_sprints SET status = %s, updated_at = %s WHERE id = %s",
                (status, _now(), sprint_id),
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="add_story_to_sprint")
    def add_story_to_sprint(self, *, sprint_id: str, story_id: str) -> bool:
        """Idempotent for the same ``(sprint_id, story_id)`` pair.

        Returns True when the row was inserted, False when the same
        pair already existed (PK conflict handled via
        ``ON CONFLICT DO NOTHING``).

        Validates that the story belongs to the sprint's product
        (transitively via ``epic → initiative → product``) before
        inserting — schema FKs only constrain the two ids individually,
        so without this check a manual assignment could mix stories from
        unrelated products into a sprint and surface them through
        ``GET /sprints/{id}`` and the SE pipeline's synthesized spec.
        Raises :class:`CrossProductSprintAssignment` (→ 400) on mismatch.

        Schema-level invariant: a story can be in **at most one** sprint
        at a time (``UNIQUE(story_id)`` on the join table — see
        ``postgres/__init__.py``). Trying to plant the same story into a
        *different* sprint trips that constraint and raises
        ``StoryAlreadyPlanned`` (mapped to 409 by the route layer).

        FK violations on either side surface as
        ``UnknownProductDeliveryEntity`` via the route's global handler.
        Existence + product check + insert all share one transaction so
        a concurrent sprint or story delete can't slip past as a 500.
        """
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT s_prod.id AS sprint_product, st_prod.id AS story_product
                       FROM product_delivery_sprints sp
                       CROSS JOIN product_delivery_stories st
                       JOIN product_delivery_products s_prod ON s_prod.id = sp.product_id
                       JOIN product_delivery_epics e ON e.id = st.epic_id
                       JOIN product_delivery_initiatives i ON i.id = e.initiative_id
                       JOIN product_delivery_products st_prod ON st_prod.id = i.product_id
                       WHERE sp.id = %s AND st.id = %s""",
                    (sprint_id, story_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise UnknownProductDeliveryEntity(
                        f"sprint {sprint_id!r} or story {story_id!r} does not exist"
                    )
                sprint_product, story_product = row
                if sprint_product != story_product:
                    raise CrossProductSprintAssignment(
                        f"story {story_id!r} belongs to product {story_product!r}, "
                        f"not the sprint's product {sprint_product!r}"
                    )
                cur.execute(
                    """INSERT INTO product_delivery_sprint_stories
                          (sprint_id, story_id, planned_at)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (sprint_id, story_id) DO NOTHING""",
                    (sprint_id, story_id, _now()),
                )
                return cur.rowcount > 0
        except psycopg_errors.ForeignKeyViolation as exc:
            # Race: a concurrent caller deleted the sprint or story
            # between the existence check and our insert. Surface as
            # the same 404 the eager validation would have produced.
            raise UnknownProductDeliveryEntity(
                f"sprint {sprint_id!r} or story {story_id!r} does not exist"
            ) from exc
        except psycopg_errors.UniqueViolation as exc:
            # Story is already planned into a *different* sprint — the
            # one-sprint-per-story invariant fired. Surface as a typed
            # domain error so the route returns 409 instead of 500.
            raise StoryAlreadyPlanned(
                f"story {story_id!r} is already planned into another sprint"
            ) from exc

    @timed_query(store=_STORE, op="count_open_stories_in_sprint")
    def count_open_stories_in_sprint(self, sprint_id: str) -> int:
        """Return how many planned stories are NOT in a terminal status.

        Phase 3 (#371). Used by the SE-pipeline release hook as the
        "is this sprint shippable?" guard — if the count is zero, the
        ReleaseManagerAgent writes a release row + notes file.
        Raises ``UnknownProductDeliveryEntity`` (→ 404) when the sprint
        id is missing so the caller can distinguish "still has work" from
        "you sent a bad id".

        Status compare uses ``LOWER(TRIM(...))`` so a row stored as
        ``"Done "`` (legacy / manual entry) still counts as terminal —
        same normalisation ``select_sprint_scope`` does.
        """
        with self._conn() as conn, conn.cursor() as cur:
            _begin_repeatable_read(cur)
            cur.execute(
                "SELECT 1 FROM product_delivery_sprints WHERE id = %s",
                (sprint_id,),
            )
            if cur.fetchone() is None:
                raise UnknownProductDeliveryEntity(f"unknown sprint: {sprint_id}")
            cur.execute(
                """SELECT COUNT(*)::int
                   FROM product_delivery_sprint_stories ss
                   JOIN product_delivery_stories s ON s.id = ss.story_id
                   WHERE ss.sprint_id = %s
                     AND LOWER(TRIM(s.status)) <> ALL(%s)""",
                (sprint_id, list(_TERMINAL_STORY_STATUSES)),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    @timed_query(store=_STORE, op="get_product_id_for_sprint")
    def get_product_id_for_sprint(self, sprint_id: str) -> str | None:
        """Return the sprint's product id, or ``None`` if the sprint is missing.

        Phase 3 (#371). The SE-pipeline hook needs ``product_id`` to
        open a ``release-manager-error`` feedback item on the
        agent's outer try/except, but at that point the agent itself
        has already failed — so we want a tiny, no-throw query rather
        than re-running the whole sprint projection. Returns ``None``
        instead of raising so the caller's error path stays simple.
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT product_id FROM product_delivery_sprints WHERE id = %s",
                (sprint_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    @timed_query(store=_STORE, op="list_planned_story_ids")
    def list_planned_story_ids(self, sprint_id: str) -> list[str]:
        """Story ids planned into ``sprint_id``, ordered by plan time.

        ``select_sprint_scope`` writes every newly-selected story with
        the same ``planned_at`` timestamp, so the secondary
        ``story_id`` ordering is needed to keep reads deterministic
        across calls (Codex review on PR #396).
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT story_id FROM product_delivery_sprint_stories
                   WHERE sprint_id = %s
                   ORDER BY planned_at, story_id""",
                (sprint_id,),
            )
            return [row[0] for row in cur.fetchall()]

    @timed_query(store=_STORE, op="get_sprint_with_stories")
    def get_sprint_with_stories(self, sprint_id: str) -> SprintWithStories | None:
        """Sprint header + planned stories + per-story acceptance criteria.

        Three SELECTs in a single ``REPEATABLE READ`` transaction so
        the sprint header, the story projection, and the AC fan-out
        all observe the same snapshot. Without this, concurrent
        backlog edits between the two reads could mix old stories
        with new ACs (or vice versa) in the SE-pipeline's
        synthesized spec — Codex flagged that on PR #396.

        ACs are fetched in one batch with ``story_id = ANY(%s)`` and
        bucketed in Python — same N+1-avoidance pattern as
        ``get_backlog_tree``.
        """
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            _begin_repeatable_read(cur)
            cur.execute(
                f"SELECT {_SPRINT_COLS} FROM product_delivery_sprints WHERE id = %s",
                (sprint_id,),
            )
            sprint_row = cur.fetchone()
            if sprint_row is None:
                return None
            sprint = Sprint.model_validate(sprint_row)
            cur.execute(
                f"""SELECT {_STORY_COLS_ALIASED}
                   FROM product_delivery_sprint_stories ss
                   JOIN product_delivery_stories s ON s.id = ss.story_id
                   WHERE ss.sprint_id = %s
                   ORDER BY s.wsjf_score DESC NULLS LAST, s.created_at ASC""",
                (sprint_id,),
            )
            stories = [Story.model_validate(row) for row in cur.fetchall()]
            acs_by_story: dict[str, list[AcceptanceCriterion]] = {}
            if stories:
                story_ids = [s.id for s in stories]
                cur.execute(
                    f"""SELECT {_AC_COLS}
                       FROM product_delivery_acceptance_criteria
                       WHERE story_id = ANY(%s)
                       ORDER BY created_at""",
                    (story_ids,),
                )
                for row in cur.fetchall():
                    ac = AcceptanceCriterion.model_validate(row)
                    acs_by_story.setdefault(ac.story_id, []).append(ac)
        return SprintWithStories(
            sprint=sprint, stories=stories, acceptance_criteria_by_story_id=acs_by_story
        )

    @timed_query(store=_STORE, op="select_sprint_scope")
    def select_sprint_scope(
        self, *, sprint_id: str, capacity_points: float | None = None
    ) -> SprintPlanResult:
        """Capacity-aware story selection, then commit picks into ``sprint_stories``.

        Greedy 0/1 fit:

        * Candidates are stories under the sprint's product that aren't
          already planned into *any* sprint **and** whose status is not
          a terminal state (see ``_TERMINAL_STORY_STATUSES``). SQL
          ``ORDER BY wsjf_score DESC NULLS LAST, created_at ASC`` makes
          the WSJF tie-break deterministic and pushes null-WSJF stories
          to the tail.
        * Stories with ``estimate_points IS NULL`` count as 0 for fit but
          stay in the candidate pool so they don't disappear from the
          plan.
        * Negative ``capacity_points`` is rejected upstream by
          ``CapacityPoints`` / ``_validate_capacity_points``; 0.0 is
          legal (zero-capacity sprint → empty plan, no inserts, no error).
        * Re-running the planner on a partially-planned sprint accounts
          for the existing scope: the budget for new picks is
          ``capacity - already_used`` and the returned
          ``used_capacity`` / ``remaining_capacity`` reflect the sprint's
          true total load (existing + newly-selected). Without this,
          repeated calls would each fit a fresh full-capacity batch and
          silently over-commit.

        All writes happen inside a single transaction so a partial
        failure can't leave the sprint half-planned.
        """
        capacity = _validate_capacity_points(capacity_points)
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            # IMPORTANT: do **not** open a REPEATABLE READ snapshot here
            # (Codex review on PR #396). Combined with `SELECT ... FOR
            # UPDATE` on the sprint row, REPEATABLE READ would lock T2
            # behind T1, but T2's snapshot would have been established
            # at its first data-touching statement *before* T1 committed
            # — so once T2 acquires the lock, its existing-load /
            # candidate / post-insert SUM queries would still be reading
            # from the stale snapshot, missing T1's just-committed rows.
            # Stay on the connection's default isolation (READ COMMITTED)
            # and rely on the row lock to serialise concurrent plans:
            # after T2 acquires the lock, every subsequent SELECT here
            # sees the latest committed state, including T1's inserts.
            # Cross-sprint races on individual stories are still
            # protected by `UNIQUE(story_id)` on the join table.
            cur.execute(
                "SELECT product_id FROM product_delivery_sprints WHERE id = %s FOR UPDATE",
                (sprint_id,),
            )
            sprint_row = cur.fetchone()
            if sprint_row is None:
                raise UnknownProductDeliveryEntity(f"unknown sprint: {sprint_id}")
            product_id = sprint_row["product_id"]
            # Existing planned load on this sprint — drives the
            # remaining-budget calculation so repeated /plan calls
            # don't double-commit.
            cur.execute(
                """SELECT COALESCE(SUM(s.estimate_points), 0)::float AS used,
                          COUNT(*)::int AS n
                   FROM product_delivery_sprint_stories ss
                   JOIN product_delivery_stories s ON s.id = ss.story_id
                   WHERE ss.sprint_id = %s""",
                (sprint_id,),
            )
            existing_row = cur.fetchone() or {"used": 0.0, "n": 0}
            existing_used = float(existing_row["used"] or 0.0)
            existing_count = int(existing_row["n"] or 0)
            # Candidates: every story under the product not already in any
            # sprint, excluding terminal-status rows so done/cancelled
            # work doesn't get re-planned. ``NOT EXISTS`` is preferable
            # to ``NOT IN`` so NULL-bearing rows never leak the wrong
            # answer. ``LOWER(TRIM(s.status))`` matches the
            # ``.strip().lower()`` normalisation the orchestrator and
            # preflight helpers do (Codex review on PR #396) — without
            # the trim, a legacy / manually-entered row stored as
            # ``"done "`` would slip past the planner's exclusion but
            # still be rejected at execution time, creating planning /
            # execution drift.
            cur.execute(
                f"""SELECT {_STORY_COLS_ALIASED}
                   FROM product_delivery_stories s
                   JOIN product_delivery_epics e ON e.id = s.epic_id
                   JOIN product_delivery_initiatives i ON i.id = e.initiative_id
                   WHERE i.product_id = %s
                     AND LOWER(TRIM(s.status)) <> ALL(%s)
                     AND NOT EXISTS (
                       SELECT 1 FROM product_delivery_sprint_stories ss
                       WHERE ss.story_id = s.id
                     )
                   ORDER BY s.wsjf_score DESC NULLS LAST, s.created_at ASC""",
                (product_id, list(_TERMINAL_STORY_STATUSES)),
            )
            candidates = [Story.model_validate(row) for row in cur.fetchall()]
            remaining_budget = max(0.0, capacity - existing_used)
            intended: list[Story] = []
            skipped: list[Story] = []
            running_cost = 0.0
            for story in candidates:
                # Treat unestimated stories as size-0: they're in the
                # plan, but don't consume budget. Avoids dropping
                # newly-created stories that haven't been pointed yet.
                #
                # Single accumulator keeps this loop O(n) — Codex
                # flagged that re-summing ``intended`` per candidate
                # was quadratic and would matter on larger backlogs.
                cost = float(story.estimate_points) if story.estimate_points is not None else 0.0
                if running_cost + cost <= remaining_budget:
                    intended.append(story)
                    running_cost += cost
                else:
                    skipped.append(story)
            now = _now()
            inserted_ids: set[str] = set()
            if intended:
                # `ON CONFLICT (sprint_id, story_id) DO NOTHING` only
                # absorbs PK collisions (same pair re-inserted). The
                # `UNIQUE(story_id)` constraint catches a different
                # case: a concurrent planner planted one of these
                # stories into a *different* sprint between our
                # candidate read and our INSERT. Surface that as
                # `StoryAlreadyPlanned` so the route returns 409 and
                # the caller knows to re-plan with a fresh candidate
                # set, instead of leaving the run with a 500.
                #
                # ``RETURNING story_id`` gives us the rows we *actually*
                # inserted (Codex review on PR #396): under concurrent
                # plans on the same sprint, a PK conflict silently
                # skips a row, and we'd otherwise misreport that row
                # as newly selected. Using RETURNING means the result
                # reflects persisted state, not intended state.
                # ``executemany`` doesn't aggregate RETURNING across
                # statements, so we issue per-row inserts in the same
                # transaction — N is bounded by the candidate cap, so
                # the extra round-trips are negligible compared to the
                # backlog read above.
                try:
                    for story in intended:
                        cur.execute(
                            """INSERT INTO product_delivery_sprint_stories
                                  (sprint_id, story_id, planned_at)
                               VALUES (%s, %s, %s)
                               ON CONFLICT (sprint_id, story_id) DO NOTHING
                               RETURNING story_id""",
                            (sprint_id, story.id, now),
                        )
                        row = cur.fetchone()
                        if row is not None:
                            # row is a dict (cursor uses dict_row); the
                            # column is `story_id` regardless of factory.
                            inserted_ids.add(row["story_id"] if isinstance(row, dict) else row[0])
                except psycopg_errors.UniqueViolation as exc:
                    raise StoryAlreadyPlanned(
                        "concurrent planner already claimed one of the selected stories; "
                        "re-run plan with a refreshed candidate set"
                    ) from exc
            # Recompute the sprint's total load from persisted state so
            # the response reflects the database's true state for this
            # sprint (Codex review on PR #396). Under READ COMMITTED
            # plus the row lock acquired above, this SUM sees every
            # committed row on the sprint — including any concurrent
            # plan that landed before us (it would have committed
            # before releasing the lock to us) and our own writes
            # earlier in this transaction.
            cur.execute(
                """SELECT COALESCE(SUM(s.estimate_points), 0)::float AS used
                   FROM product_delivery_sprint_stories ss
                   JOIN product_delivery_stories s ON s.id = ss.story_id
                   WHERE ss.sprint_id = %s""",
                (sprint_id,),
            )
            total_row = cur.fetchone() or {"used": 0.0}
            total_used = float(total_row["used"] or 0.0)
        # Anything intended but not persisted (PK conflict from a racing
        # concurrent plan on the same sprint) goes to the skipped bucket
        # so the response stays accurate.
        for story in intended:
            if story.id not in inserted_ids:
                skipped.append(story)
        new_used = sum(
            float(s.estimate_points) if s.estimate_points is not None else 0.0
            for s in intended
            if s.id in inserted_ids
        )
        rationale = (
            f"Selected {len(inserted_ids)} new stories totaling {new_used:g} points "
            f"({existing_count} already planned for {existing_used:g} points; "
            f"capacity {capacity:g}); skipped {len(skipped)} for capacity."
        )
        return SprintPlanResult(
            sprint_id=sprint_id,
            selected_story_ids=[s.id for s in intended if s.id in inserted_ids],
            skipped_story_ids=[s.id for s in skipped],
            used_capacity=total_used,
            remaining_capacity=max(0.0, capacity - total_used),
            rationale=rationale,
        )

    # ------------------------------------------------------------------
    # Releases (Phase 2 — table CRUD only; routes ship in #371)
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="create_release")
    def create_release(
        self,
        *,
        sprint_id: str,
        version: str,
        notes_path: str | None,
        shipped_at: datetime | None,
        author: str,
    ) -> Release:
        version = _validate_text(version, label="version", max_len=80)
        # Mirror ``Release.shipped_at: AwareDatetime`` at the store
        # boundary so non-route callers can't slip a naive timestamp
        # past — TIMESTAMPTZ would otherwise interpret it in server-
        # local timezone (Codex review on PR #396).
        if shipped_at is not None and shipped_at.tzinfo is None:
            raise ValueError("shipped_at must be timezone-aware")
        now = _now()
        rid = _new_id()
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    f"""INSERT INTO product_delivery_releases ({_RELEASE_COLS})
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        rid,
                        sprint_id,
                        version,
                        notes_path,
                        shipped_at,
                        author,
                        now,
                        now,
                    ),
                )
        except psycopg_errors.ForeignKeyViolation as exc:
            raise UnknownProductDeliveryEntity(f"sprint {sprint_id!r} does not exist") from exc
        except psycopg_errors.UniqueViolation as exc:
            # Schema enforces UNIQUE(sprint_id, version) — a duplicate
            # ship (concurrent or retry) lands here. Map to a typed
            # domain error so the route returns 409 instead of 500
            # and the ReleaseManagerAgent can clean up the on-disk
            # notes file it just wrote (PR #424 Codex review).
            raise DuplicateReleaseVersion(
                f"release {version!r} for sprint {sprint_id!r} already exists"
            ) from exc
        return Release(
            id=rid,
            sprint_id=sprint_id,
            version=version,
            notes_path=notes_path,
            shipped_at=shipped_at,
            author=author,
            created_at=now,
            updated_at=now,
        )

    @timed_query(store=_STORE, op="get_release")
    def get_release(self, release_id: str) -> Release | None:
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_RELEASE_COLS} FROM product_delivery_releases WHERE id = %s",
                (release_id,),
            )
            row = cur.fetchone()
            return Release.model_validate(row) if row else None

    @timed_query(store=_STORE, op="list_releases_for_sprint")
    def list_releases_for_sprint(self, sprint_id: str) -> list[Release]:
        """List releases under a sprint.

        Raises ``UnknownProductDeliveryEntity`` when the sprint doesn't
        exist (matches the ``list_stories_for_product`` /
        ``list_feedback`` pattern) so callers can distinguish "sprint
        has no releases yet" from "you sent an invalid id". Both
        statements run inside one ``REPEATABLE READ`` transaction so a
        concurrent sprint delete can't turn a 404 into a ``200 []``.
        """
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            _begin_repeatable_read(cur)
            cur.execute(
                "SELECT 1 FROM product_delivery_sprints WHERE id = %s",
                (sprint_id,),
            )
            if cur.fetchone() is None:
                raise UnknownProductDeliveryEntity(f"unknown sprint: {sprint_id}")
            cur.execute(
                f"""SELECT {_RELEASE_COLS}
                   FROM product_delivery_releases
                   WHERE sprint_id = %s
                   ORDER BY created_at DESC""",
                (sprint_id,),
            )
            return [Release.model_validate(row) for row in cur.fetchall()]

    @timed_query(store=_STORE, op="list_releases_for_product")
    def list_releases_for_product(self, product_id: str) -> list[Release]:
        """List releases for every sprint under ``product_id``, newest first.

        Phase 3 (#371). Backs ``GET /api/product-delivery/releases?product_id=…``.
        Joins ``releases → sprints`` so a single round trip returns the
        full history without per-sprint fan-out at the route layer.
        Raises ``UnknownProductDeliveryEntity`` when the product is
        missing — same contract as ``list_feedback`` /
        ``list_releases_for_sprint`` so callers can branch on 404 vs
        ``200 []``. Both statements share one ``REPEATABLE READ``
        snapshot so a concurrent product delete can't turn a 404 into
        an empty list.

        Order is ``shipped_at DESC NULLS LAST, created_at DESC`` — most
        recently *shipped* releases bubble up, but unshipped (preview)
        rows still surface at the tail in stable created-time order.
        """
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            _begin_repeatable_read(cur)
            cur.execute(
                "SELECT 1 FROM product_delivery_products WHERE id = %s",
                (product_id,),
            )
            if cur.fetchone() is None:
                raise UnknownProductDeliveryEntity(f"unknown product: {product_id}")
            cur.execute(
                f"""SELECT {_RELEASE_COLS_ALIASED}
                   FROM product_delivery_releases r
                   JOIN product_delivery_sprints sp ON sp.id = r.sprint_id
                   WHERE sp.product_id = %s
                   ORDER BY r.shipped_at DESC NULLS LAST, r.created_at DESC""",
                (product_id,),
            )
            return [Release.model_validate(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _conn(self):
        if not is_postgres_enabled():
            raise ProductDeliveryStorageUnavailable(
                "POSTGRES_HOST is not configured; product_delivery storage is unavailable."
            )
        return _SafeConn()


class _SafeConn:
    """Context manager wrapper around ``shared_postgres.get_conn`` that
    surfaces both connection-acquisition *and* query-time infra
    failures as :class:`ProductDeliveryStorageUnavailable`.

    ``get_conn()`` returns a context manager, but the real connection
    isn't acquired until ``__enter__`` runs (the pool's ``connection()``
    method blocks waiting for a free slot, then opens the socket if
    needed). If the pool times out or the driver raises, the original
    ``_conn`` only caught errors at *creation* of that context manager
    — the ``__enter__`` exceptions surfaced as raw ``PoolTimeout`` /
    driver errors, bypassing the route-level 503 mapping and producing
    unhandled 500s.

    Codex also flagged that ``cur.execute(...)`` errors raised *inside*
    the body — most importantly ``UndefinedTable`` when schema
    registration applied only partially, plus ``OperationalError``
    when the connection drops mid-query — bypass the same 503 mapping.
    ``__exit__`` translates those infra errors to
    ``ProductDeliveryStorageUnavailable`` too, while leaving the store's
    own typed domain exceptions (``UnknownProductDeliveryEntity``,
    ``CrossProductFeedbackLink``, ``ForeignKeyViolation``) and
    ``ValueError`` from the validator helpers untouched.

    Exit semantics are otherwise unchanged — the pool's CM still
    commits on clean exit, rolls back on exception, and returns the
    connection to the pool.
    """

    # psycopg query-time exceptions that mean "infra is broken"
    # (transport down, schema not deployed, pool poisoned). Each maps
    # naturally to 503 — clients should retry. Order matters only for
    # the diagnostic log line (the most-specific match wins via the
    # ``type(exc).__name__`` we record).
    #
    # Also includes the *base classes* ``OperationalError`` and
    # ``InterfaceError`` so that transport-layer failures raised
    # without a SQLSTATE-derived subclass (e.g. raw socket-closed
    # errors from psycopg) still map to 503 instead of falling
    # through as 500.
    _INFRA_EXC_TYPES = (
        psycopg_errors.UndefinedTable,
        psycopg_errors.UndefinedColumn,
        psycopg_errors.UndefinedObject,
        psycopg_errors.AdminShutdown,
        psycopg_errors.CrashShutdown,
        psycopg_errors.ConnectionException,
        psycopg_errors.ConnectionFailure,
        psycopg_errors.SqlclientUnableToEstablishSqlconnection,
        psycopg.OperationalError,
        psycopg.InterfaceError,
    )

    def __init__(self) -> None:
        self._cm: Any | None = None

    def __enter__(self) -> Any:
        try:
            self._cm = get_conn()
            return self._cm.__enter__()
        except ProductDeliveryStorageUnavailable:
            raise
        except Exception as exc:  # pragma: no cover — infra paths
            self._cm = None
            raise ProductDeliveryStorageUnavailable(str(exc)) from exc

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        if self._cm is None:
            return False
        # Always let the pool's CM tear down (commit/rollback, return
        # connection). If teardown itself raises (e.g. connection lost
        # during commit on a write path), we *must* let the caller see
        # it — silently logging would let a route report 200 on a
        # write that never committed. Translate it to
        # ``ProductDeliveryStorageUnavailable`` so the route returns
        # 503 (retryable) rather than the previous "log + return False"
        # which lost the failure entirely.
        try:
            self._cm.__exit__(exc_type, exc, tb)
        except Exception as teardown_exc:  # pragma: no cover — infra paths
            logger.warning(
                "ProductDeliveryStore: pool teardown raised %s; mapping to "
                "ProductDeliveryStorageUnavailable so caller doesn't report success",
                type(teardown_exc).__name__,
                exc_info=True,
            )
            raise ProductDeliveryStorageUnavailable(str(teardown_exc)) from teardown_exc
        # Translate infra-class exceptions to ProductDeliveryStorageUnavailable
        # so the route handler returns 503. Domain exceptions raised
        # by the store itself (``UnknownProductDeliveryEntity``,
        # ``CrossProductFeedbackLink``) and ``ValueError`` from the
        # validator helpers, plus ``ForeignKeyViolation`` (translated
        # by callers to ``UnknownProductDeliveryEntity``), all flow
        # through unchanged.
        if exc is not None and isinstance(exc, self._INFRA_EXC_TYPES):
            logger.warning(
                "ProductDeliveryStore: infra error %s raised inside connection; "
                "mapping to ProductDeliveryStorageUnavailable",
                type(exc).__name__,
            )
            raise ProductDeliveryStorageUnavailable(str(exc)) from exc
        # Returning False (or None) lets the original exception, if
        # any, propagate; returning True would suppress it. Never
        # suppress — every store method needs the caller to see real
        # failures.
        return False


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
