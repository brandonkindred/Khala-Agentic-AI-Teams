"""API tests for the product_delivery router.

Routes are exercised through a FastAPI ``TestClient`` against a minimal
app that mounts only the product_delivery router. The store is replaced
with an in-memory fake via ``monkeypatch`` so these tests do not require
Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from product_delivery.models import (
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
from product_delivery.store import (
    _TERMINAL_STORY_STATUSES,
    CrossProductFeedbackLink,
    CrossProductSprintAssignment,
    StoryAlreadyPlanned,
    UnknownProductDeliveryEntity,
)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Fake store — kind-driven CRUD over per-bucket dicts.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Kind:
    """Metadata table mirroring the real store's ``_ROW_SPECS``.

    ``model`` is the Pydantic class returned from create. ``bucket`` is
    the attribute name of the in-memory dict that holds rows of this
    kind. ``parent_kind`` (when set) names the kind whose dict is
    consulted for FK validation; ``parent_field`` is the field name on
    the row that holds the parent's id.
    """

    model: type
    bucket: str
    parent_kind: str | None = None
    parent_field: str | None = None
    fk_label: str | None = None


_KINDS: dict[str, _Kind] = {
    "product": _Kind(Product, "products"),
    "initiative": _Kind(Initiative, "initiatives", "product", "product_id", "product"),
    "epic": _Kind(Epic, "epics", "initiative", "initiative_id", "initiative"),
    "story": _Kind(Story, "stories", "epic", "epic_id", "epic"),
    "task": _Kind(Task, "tasks", "story", "story_id", "story"),
    "ac": _Kind(AcceptanceCriterion, "acs", "story", "story_id", "story"),
    "feedback": _Kind(FeedbackItem, "feedback"),
    "sprint": _Kind(Sprint, "sprints", "product", "product_id", "product"),
    "release": _Kind(Release, "releases", "sprint", "sprint_id", "sprint"),
}


@dataclass
class _FakeStore:
    """In-memory subset of ``ProductDeliveryStore`` used by the API tests.

    Kind-driven: every CRUD method is a thin shim over ``_insert`` /
    ``_update`` that consults :data:`_KINDS` for the bucket attribute,
    parent FK, and Pydantic model. Mirrors the real store's
    ``_ROW_SPECS`` pattern so the two stay in lockstep.
    """

    products: dict[str, Product] = field(default_factory=dict)
    initiatives: dict[str, Initiative] = field(default_factory=dict)
    epics: dict[str, Epic] = field(default_factory=dict)
    stories: dict[str, Story] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)
    acs: dict[str, AcceptanceCriterion] = field(default_factory=dict)
    feedback: dict[str, FeedbackItem] = field(default_factory=dict)
    sprints: dict[str, Sprint] = field(default_factory=dict)
    releases: dict[str, Release] = field(default_factory=dict)
    sprint_stories: list[tuple[str, str]] = field(default_factory=list)
    _next: ClassVar[int] = 0

    def _id(self) -> str:
        type(self)._next += 1
        return f"id{type(self)._next}"

    # ------------------------------------------------------------------
    # Generic create — used by every public create_* shim.
    # ------------------------------------------------------------------

    def _insert(self, kind: str, **fields: Any) -> Any:
        meta = _KINDS[kind]
        if meta.parent_kind:
            parent_bucket = getattr(self, _KINDS[meta.parent_kind].bucket)
            if fields[meta.parent_field] not in parent_bucket:  # type: ignore[index]
                raise UnknownProductDeliveryEntity(
                    f"{meta.fk_label} {fields[meta.parent_field]!r} does not exist"  # type: ignore[index]
                )
        now = _now()
        fields["id"] = self._id()
        fields["created_at"] = now
        fields["updated_at"] = now
        row = meta.model(**fields)
        getattr(self, meta.bucket)[row.id] = row
        return row

    # ------------------------------------------------------------------
    # Public store API — thin shims, signatures match ProductDeliveryStore.
    # ------------------------------------------------------------------

    def create_product(self, *, name: str, description: str, vision: str, author: str) -> Product:
        return self._insert(
            "product", name=name, description=description, vision=vision, author=author
        )

    def list_products(self) -> list[Product]:
        return list(self.products.values())

    def get_product(self, product_id: str) -> Product | None:
        return self.products.get(product_id)

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
            status=status,
            estimate_points=estimate_points,
            author=author,
        )

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

    def create_acceptance_criterion(
        self, *, story_id: str, text: str, satisfied: bool, author: str
    ) -> AcceptanceCriterion:
        return self._insert("ac", story_id=story_id, text=text, satisfied=satisfied, author=author)

    # status / score updates -------------------------------------------

    _UPDATE_KINDS: ClassVar[dict[str, str]] = {
        "initiative": "initiatives",
        "epic": "epics",
        "story": "stories",
        "task": "tasks",
    }

    def update_status(self, *, kind: str, entity_id: str, status: str) -> bool:
        bucket_name = self._UPDATE_KINDS.get(kind)
        if bucket_name is None:
            raise ValueError(kind)
        bag = getattr(self, bucket_name)
        if entity_id not in bag:
            return False
        bag[entity_id] = bag[entity_id].model_copy(update={"status": status, "updated_at": _now()})
        return True

    def update_scores(
        self,
        *,
        kind: str,
        entity_id: str,
        wsjf_score: float | None,
        rice_score: float | None,
    ) -> bool:
        if wsjf_score is None and rice_score is None:
            return False
        bucket_name = self._UPDATE_KINDS.get(kind)
        if bucket_name is None:
            raise ValueError(kind)
        bag = getattr(self, bucket_name)
        if entity_id not in bag:
            return False
        update: dict[str, Any] = {"updated_at": _now()}
        if wsjf_score is not None:
            update["wsjf_score"] = wsjf_score
        if rice_score is not None:
            update["rice_score"] = rice_score
        bag[entity_id] = bag[entity_id].model_copy(update=update)
        return True

    def bulk_update_story_scores(self, rows: list[tuple[str, float | None, float | None]]) -> int:
        n = 0
        for sid, w, r in rows:
            if sid not in self.stories:
                continue
            update: dict[str, Any] = {"updated_at": _now()}
            if w is not None:
                update["wsjf_score"] = w
            if r is not None:
                update["rice_score"] = r
            self.stories[sid] = self.stories[sid].model_copy(update=update)
            n += 1
        return n

    # backlog tree -----------------------------------------------------

    def get_backlog_tree(self, product_id: str) -> BacklogTree | None:
        p = self.products.get(product_id)
        if p is None:
            return None
        i_nodes: list[InitiativeNode] = []
        for i in self.initiatives.values():
            if i.product_id != product_id:
                continue
            e_nodes: list[EpicNode] = []
            for e in self.epics.values():
                if e.initiative_id != i.id:
                    continue
                s_nodes: list[StoryNode] = []
                for s in self.stories.values():
                    if s.epic_id != e.id:
                        continue
                    s_nodes.append(
                        StoryNode.model_validate(
                            {
                                **s.model_dump(),
                                "tasks": [t for t in self.tasks.values() if t.story_id == s.id],
                                "acceptance_criteria": [
                                    a for a in self.acs.values() if a.story_id == s.id
                                ],
                            }
                        )
                    )
                e_nodes.append(EpicNode.model_validate({**e.model_dump(), "stories": s_nodes}))
            i_nodes.append(InitiativeNode.model_validate({**i.model_dump(), "epics": e_nodes}))
        return BacklogTree(product=p, initiatives=i_nodes)

    def list_stories_for_product(self, product_id: str) -> list[Story]:
        # Mirror the real store's transactional contract: an unknown
        # product raises `UnknownProductDeliveryEntity` (→ 404 via the
        # global handler) so a concurrent delete can't slip past as
        # `200 []`.
        if product_id not in self.products:
            raise UnknownProductDeliveryEntity(f"unknown product: {product_id}")
        epic_ids = {
            e.id
            for e in self.epics.values()
            if e.initiative_id
            in {i.id for i in self.initiatives.values() if i.product_id == product_id}
        }
        return [s for s in self.stories.values() if s.epic_id in epic_ids]

    # feedback ---------------------------------------------------------

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
        if product_id not in self.products:
            raise UnknownProductDeliveryEntity(f"product {product_id!r} does not exist")
        if linked_story_id is not None:
            story = self.stories.get(linked_story_id)
            if story is None:
                raise UnknownProductDeliveryEntity(f"story {linked_story_id!r} does not exist")
            owning_product = self.initiatives[self.epics[story.epic_id].initiative_id].product_id
            if owning_product != product_id:
                raise CrossProductFeedbackLink(
                    f"story {linked_story_id!r} belongs to product "
                    f"{owning_product!r}, not {product_id!r}"
                )
        # Mirror the real store's #371 sprint-id existence check so a
        # bogus sprint id surfaces as 404 instead of being silently
        # nulled out by ON DELETE SET NULL semantics. Cross-product
        # tagging is rejected as 400 (Codex review on PR #424) — same
        # contract as the existing story-link check.
        if sprint_id is not None:
            sprint = self.sprints.get(sprint_id)
            if sprint is None:
                raise UnknownProductDeliveryEntity(f"sprint {sprint_id!r} does not exist")
            if sprint.product_id != product_id:
                raise CrossProductFeedbackLink(
                    f"sprint {sprint_id!r} belongs to product "
                    f"{sprint.product_id!r}, not {product_id!r}"
                )
        return self._insert(
            "feedback",
            product_id=product_id,
            source=source,
            raw_payload=raw_payload,
            severity=severity,
            status="open",
            linked_story_id=linked_story_id,
            sprint_id=sprint_id,
            author=author,
        )

    def list_feedback(self, product_id: str, *, status: str | None = None) -> list[FeedbackItem]:
        # Mirror the real store: unknown product raises 404 inside the
        # same transaction as the SELECT, so concurrent deletes don't
        # turn a 404 into `200 []`. Also normalise the status filter
        # the same way the real store does so the in-memory fake stays
        # in lockstep with the API contract.
        from product_delivery.store import _validate_status

        if product_id not in self.products:
            raise UnknownProductDeliveryEntity(f"unknown product: {product_id}")
        out = [f for f in self.feedback.values() if f.product_id == product_id]
        if status is not None:
            normalised = _validate_status(status)
            out = [f for f in out if f.status == normalised]
        return out

    # sprints (Phase 2 of #243) ----------------------------------------

    def create_sprint(
        self,
        *,
        product_id: str,
        name: str,
        capacity_points: float | None,
        starts_at: Any,
        ends_at: Any,
        status: str,
        author: str,
    ) -> Sprint:
        return self._insert(
            "sprint",
            product_id=product_id,
            name=name,
            capacity_points=capacity_points or 0.0,
            starts_at=starts_at,
            ends_at=ends_at,
            status=status,
            author=author,
        )

    def get_sprint(self, sprint_id: str) -> Sprint | None:
        return self.sprints.get(sprint_id)

    def add_story_to_sprint(self, *, sprint_id: str, story_id: str) -> bool:
        if sprint_id not in self.sprints or story_id not in self.stories:
            raise UnknownProductDeliveryEntity(
                f"sprint {sprint_id!r} or story {story_id!r} does not exist"
            )
        # Mirror the real store's transitive cross-product check.
        sprint_product = self.sprints[sprint_id].product_id
        story = self.stories[story_id]
        story_initiative_id = self.epics[story.epic_id].initiative_id
        story_product = self.initiatives[story_initiative_id].product_id
        if sprint_product != story_product:
            raise CrossProductSprintAssignment(
                f"story {story_id!r} belongs to product {story_product!r}, "
                f"not the sprint's product {sprint_product!r}"
            )
        if (sprint_id, story_id) in self.sprint_stories:
            return False
        # Mirror the schema-level UNIQUE(story_id) constraint: a story
        # can only live in one sprint at a time.
        if any(sid == story_id for _, sid in self.sprint_stories):
            raise StoryAlreadyPlanned(f"story {story_id!r} is already planned into another sprint")
        self.sprint_stories.append((sprint_id, story_id))
        return True

    def list_planned_story_ids(self, sprint_id: str) -> list[str]:
        return [s for sid, s in self.sprint_stories if sid == sprint_id]

    def list_releases_for_sprint(self, sprint_id: str) -> list[Release]:
        if sprint_id not in self.sprints:
            raise UnknownProductDeliveryEntity(f"unknown sprint: {sprint_id}")
        return [r for r in self.releases.values() if r.sprint_id == sprint_id]

    def list_releases_for_product(self, product_id: str) -> list[Release]:
        # Mirror the real store: unknown product → 404, otherwise return
        # every release whose sprint belongs to this product, ordered by
        # shipped_at desc-nulls-last, created_at desc.
        if product_id not in self.products:
            raise UnknownProductDeliveryEntity(f"unknown product: {product_id}")
        rels = [
            r
            for r in self.releases.values()
            if r.sprint_id in self.sprints and self.sprints[r.sprint_id].product_id == product_id
        ]
        return sorted(
            rels,
            key=lambda r: (
                r.shipped_at is None,
                # Negate via subtraction since datetimes don't support
                # unary minus; flip the sign by sorting -timestamp.
                -(r.shipped_at.timestamp() if r.shipped_at else 0.0),
                -r.created_at.timestamp(),
            ),
        )

    def count_open_stories_in_sprint(self, sprint_id: str) -> int:
        if sprint_id not in self.sprints:
            raise UnknownProductDeliveryEntity(f"unknown sprint: {sprint_id}")
        ids = [s for sid, s in self.sprint_stories if sid == sprint_id]
        return sum(
            1
            for i in ids
            if i in self.stories
            and (self.stories[i].status or "").strip().lower() not in _TERMINAL_STORY_STATUSES
        )

    def get_product_id_for_sprint(self, sprint_id: str) -> str | None:
        sprint = self.sprints.get(sprint_id)
        return sprint.product_id if sprint else None

    def create_release(
        self,
        *,
        sprint_id: str,
        version: str,
        notes_path: str | None,
        shipped_at: Any,
        author: str,
    ) -> Release:
        if sprint_id not in self.sprints:
            raise UnknownProductDeliveryEntity(f"sprint {sprint_id!r} does not exist")
        return self._insert(
            "release",
            sprint_id=sprint_id,
            version=version,
            notes_path=notes_path,
            shipped_at=shipped_at,
            author=author,
        )

    def get_sprint_with_stories(self, sprint_id: str) -> SprintWithStories | None:
        sprint = self.sprints.get(sprint_id)
        if sprint is None:
            return None
        ids = [s for sid, s in self.sprint_stories if sid == sprint_id]
        # Stable ordering: WSJF desc with NULL last, then created_at asc.
        # `(wsjf is None, -wsjf, created_at)` sorts None to the end via
        # the boolean-tuple trick; explicit `0.0` fallback for the
        # secondary key isn't reached when the bool is True.
        ordered = sorted(
            (self.stories[i] for i in ids if i in self.stories),
            key=lambda s: (
                s.wsjf_score is None,
                -(s.wsjf_score or 0.0),
                s.created_at,
            ),
        )
        # Bucket ACs by story_id, ordered by created_at — same shape
        # the real store fills in inside its single transaction.
        acs_by_story: dict[str, list[AcceptanceCriterion]] = {}
        for story in ordered:
            acs_by_story[story.id] = sorted(
                (a for a in self.acs.values() if a.story_id == story.id),
                key=lambda a: a.created_at,
            )
        return SprintWithStories(
            sprint=sprint, stories=ordered, acceptance_criteria_by_story_id=acs_by_story
        )

    def select_sprint_scope(
        self, *, sprint_id: str, capacity_points: float | None = None
    ) -> SprintPlanResult:
        sprint = self.sprints.get(sprint_id)
        if sprint is None:
            raise UnknownProductDeliveryEntity(f"unknown sprint: {sprint_id}")
        capacity = float(capacity_points if capacity_points is not None else 0.0)
        # Already-planned story ids across all sprints (mirrors the
        # real store's `NOT EXISTS` candidate filter).
        already = {sid for _, sid in self.sprint_stories}
        # Existing load on *this* sprint — drives the remaining-budget
        # calculation so repeated /plan calls don't over-commit.
        existing_planned = [
            self.stories[sid] for ssid, sid in self.sprint_stories if ssid == sprint_id
        ]
        existing_used = sum(
            float(s.estimate_points) if s.estimate_points is not None else 0.0
            for s in existing_planned
        )
        existing_count = len(existing_planned)
        # Candidates: same product, not in any sprint, not in a terminal
        # status — mirror the real store's WHERE clause.
        candidates = sorted(
            (
                s
                for s in self.stories.values()
                if self.epics[s.epic_id].initiative_id
                in {i.id for i in self.initiatives.values() if i.product_id == sprint.product_id}
                and s.id not in already
                and (s.status or "").strip().lower() not in _TERMINAL_STORY_STATUSES
            ),
            key=lambda s: (
                s.wsjf_score is None,
                -(s.wsjf_score or 0.0),
                s.created_at,
            ),
        )
        remaining_budget = max(0.0, capacity - existing_used)
        selected: list[Story] = []
        skipped: list[Story] = []
        new_used = 0.0
        for story in candidates:
            cost = float(story.estimate_points) if story.estimate_points is not None else 0.0
            if new_used + cost <= remaining_budget:
                selected.append(story)
                new_used += cost
            else:
                skipped.append(story)
        for s in selected:
            self.sprint_stories.append((sprint_id, s.id))
        total_used = existing_used + new_used
        return SprintPlanResult(
            sprint_id=sprint_id,
            selected_story_ids=[s.id for s in selected],
            skipped_story_ids=[s.id for s in skipped],
            used_capacity=total_used,
            remaining_capacity=max(0.0, capacity - total_used),
            rationale=(
                f"Selected {len(selected)} new stories totaling {new_used:g} points "
                f"({existing_count} already planned for {existing_used:g} points; "
                f"capacity {capacity:g}); skipped {len(skipped)} for capacity."
            ),
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client_and_store(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _FakeStore]:
    """Yield (TestClient, fake_store) so tests can assert on either side
    without the runtime ``client.fake_store = …`` monkey-attr hack.
    """
    from unified_api.routes import product_delivery as router_module

    fake = _FakeStore()
    monkeypatch.setattr(router_module, "get_store", lambda: fake)
    monkeypatch.setattr(router_module, "resolve_author", lambda: "tester")

    app = FastAPI()
    app.include_router(router_module.router)
    router_module.register_pd_exception_handlers(app)
    return TestClient(app), fake


@pytest.fixture
def client(client_and_store: tuple[TestClient, _FakeStore]) -> TestClient:
    return client_and_store[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_create_and_list_products(client: TestClient) -> None:
    resp = client.post(
        "/api/product-delivery/products",
        json={"name": "Demo", "description": "d", "vision": "v"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Demo"
    assert body["author"] == "tester"
    assert body["id"]

    listed = client.get("/api/product-delivery/products").json()
    assert len(listed) == 1
    assert listed[0]["id"] == body["id"]


def test_full_hierarchy_creation_and_backlog_tree(client: TestClient) -> None:
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]
    eid = client.post(
        "/api/product-delivery/epics",
        json={"initiative_id": iid, "title": "E"},
    ).json()["id"]
    sid = client.post(
        "/api/product-delivery/stories",
        json={"epic_id": eid, "title": "S", "estimate_points": 5},
    ).json()["id"]
    client.post(
        "/api/product-delivery/tasks",
        json={"story_id": sid, "title": "T", "owner": "alice"},
    )
    client.post(
        "/api/product-delivery/acceptance-criteria",
        json={"story_id": sid, "text": "must work"},
    )

    tree = client.get(f"/api/product-delivery/products/{pid}/backlog").json()
    assert tree["product"]["id"] == pid
    assert tree["initiatives"][0]["id"] == iid
    epic = tree["initiatives"][0]["epics"][0]
    assert epic["id"] == eid
    story = epic["stories"][0]
    assert story["id"] == sid
    assert story["estimate_points"] == 5
    assert len(story["tasks"]) == 1
    assert len(story["acceptance_criteria"]) == 1


def test_initiative_create_404_when_product_missing(client: TestClient) -> None:
    resp = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": "missing", "title": "x"},
    )
    assert resp.status_code == 404


def test_status_and_score_patches_apply(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    client, fake = client_and_store
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]

    r = client.patch(
        f"/api/product-delivery/initiative/{iid}/status",
        json={"status": "in_sprint"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "in_sprint"

    r = client.patch(
        f"/api/product-delivery/initiative/{iid}/scores",
        json={"wsjf_score": 12.5, "rice_score": 80.0},
    )
    assert r.status_code == 200

    assert fake.initiatives[iid].status == "in_sprint"
    assert fake.initiatives[iid].wsjf_score == 12.5
    assert fake.initiatives[iid].rice_score == 80.0


def test_status_patch_404_for_unknown_id(client: TestClient) -> None:
    r = client.patch(
        "/api/product-delivery/story/missing/status",
        json={"status": "done"},
    )
    assert r.status_code == 404


@pytest.mark.parametrize("bad", [True, False])
def test_score_patch_rejects_boolean_values(client: TestClient, bad: bool) -> None:
    # Pydantic's default `float` coercion accepts JSON booleans
    # (`true → 1.0`, `false → 0.0`). The validator now rejects them
    # so PATCH /scores can't silently mutate ranking data.
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]
    r = client.patch(
        f"/api/product-delivery/initiative/{iid}/scores",
        json={"wsjf_score": bad},
    )
    assert r.status_code == 422


@pytest.mark.parametrize("bad", [True, False])
def test_story_create_rejects_boolean_estimate_points(client: TestClient, bad: bool) -> None:
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]
    eid = client.post(
        "/api/product-delivery/epics",
        json={"initiative_id": iid, "title": "E"},
    ).json()["id"]
    r = client.post(
        "/api/product-delivery/stories",
        json={"epic_id": eid, "title": "S", "estimate_points": bad},
    )
    assert r.status_code == 422


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_score_patch_rejects_non_finite_values(client: TestClient, bad: str) -> None:
    # NaN / ±Infinity must be rejected at validation: persisting them
    # would later break Starlette's JSON encoder when /backlog or /groom
    # serialize the row, manifesting as a 500 long after the bad write.
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]
    # JSON itself doesn't allow NaN / Infinity tokens, so send them as
    # strings to exercise Pydantic's float coercion.
    r = client.request(
        "PATCH",
        f"/api/product-delivery/initiative/{iid}/scores",
        content=f'{{"wsjf_score": "{bad}"}}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    "body",
    [{}, {"wsjf_score": None, "rice_score": None}],
    ids=["empty", "explicit-nulls"],
)
def test_score_patch_empty_body_returns_400(client: TestClient, body: dict[str, Any]) -> None:
    # Empty (or all-null) score payload is a client error, not a 404 —
    # otherwise clients can't tell "you sent nothing" from "the entity
    # doesn't exist" and may incorrectly trigger a create/retry flow.
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]

    r = client.patch(f"/api/product-delivery/initiative/{iid}/scores", json=body)
    assert r.status_code == 400
    assert "wsjf_score" in r.json()["detail"]


def test_score_patch_404_for_unknown_id_with_real_payload(client: TestClient) -> None:
    # Sanity check: a well-formed payload against an unknown id is still 404.
    r = client.patch(
        "/api/product-delivery/initiative/missing/scores",
        json={"wsjf_score": 1.0},
    )
    assert r.status_code == 404


@pytest.mark.parametrize("bad", [0, -3.5])
def test_story_create_rejects_non_positive_estimate_points(client: TestClient, bad: float) -> None:
    # Negative / zero estimates would feed into WSJF/RICE denominators
    # which clamp to 1, silently inflating priority. Reject at the API.
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]
    eid = client.post(
        "/api/product-delivery/epics",
        json={"initiative_id": iid, "title": "E"},
    ).json()["id"]

    r = client.post(
        "/api/product-delivery/stories",
        json={"epic_id": eid, "title": "S", "estimate_points": bad},
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    "field, value",
    [
        ("status", ""),  # empty
        ("status", "x" * 41),  # over the 40-char cap
    ],
    ids=["empty", "over-cap"],
)
def test_initiative_create_rejects_out_of_bounds_status(
    client: TestClient, field: str, value: str
) -> None:
    # Codex flagged that Create payloads accepted unbounded `status`
    # while StatusUpdate enforced 1..40. Now both go through the same
    # `StatusStr` annotated alias, so create + patch agree.
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    r = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I", field: value},
    )
    assert r.status_code == 422


@pytest.mark.parametrize("blank", [" ", "   ", "\t", "\n", " \t\n "])
def test_product_create_rejects_whitespace_only_name(client: TestClient, blank: str) -> None:
    """Whitespace-only `name` must 422 at the API, not 500 at the store.

    Codex flagged that ``min_length=1`` accepts ``"   "`` because it
    counts the spaces; the value then hit the store's ``_validate_title``
    helper, which raises ``ValueError`` — and ``ValueError`` isn't in
    the domain-exception map, so clients saw a 500 instead of a 4xx.
    """
    r = client.post("/api/product-delivery/products", json={"name": blank})
    assert r.status_code == 422


@pytest.mark.parametrize("blank", [" ", "   ", "\t", "\n"])
def test_initiative_create_rejects_whitespace_only_title(client: TestClient, blank: str) -> None:
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    r = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": blank},
    )
    assert r.status_code == 422


@pytest.mark.parametrize("blank", [" ", "   ", "\t"])
def test_initiative_create_rejects_whitespace_only_status(client: TestClient, blank: str) -> None:
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    r = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I", "status": blank},
    )
    assert r.status_code == 422


def test_status_patch_rejects_whitespace_only_status(client: TestClient) -> None:
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]
    r = client.patch(
        f"/api/product-delivery/initiative/{iid}/status",
        json={"status": "   "},
    )
    assert r.status_code == 422


def test_status_with_trailing_whitespace_is_accepted_after_trim(client: TestClient) -> None:
    """Codex-flagged ordering bug: ``StatusStr`` used to apply ``max_length=40``
    *before* the AfterValidator stripped whitespace, so a 40-char status
    plus a trailing space (41 chars total, but trims to 40) was rejected
    by the API while the store accepted the trimmed value. With the
    validator applied first, both paths agree.
    """
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    # 40-char status + trailing space = 41 chars raw, 40 after trim
    status_with_trailing_space = "a" * 40 + " "
    r = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I", "status": status_with_trailing_space},
    )
    assert r.status_code == 200, r.text
    # And the persisted value is the trimmed form, not the raw input.
    assert r.json()["status"] == "a" * 40


def test_title_with_trailing_whitespace_is_accepted_after_trim(client: TestClient) -> None:
    """Same length-after-trim contract for ``TitleStr``."""
    title_with_trailing_space = "T" * 200 + " "
    r = client.post(
        "/api/product-delivery/products",
        json={"name": title_with_trailing_space},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "T" * 200


def test_status_overlong_after_trim_still_rejected(client: TestClient) -> None:
    """Defence-in-depth: 41 chars after stripping must still be 422
    (the trim doesn't bypass the cap, just moves the cap behind it).
    """
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    r = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I", "status": "a" * 41},
    )
    assert r.status_code == 422


@pytest.mark.parametrize("blank", [" ", "   ", "\t", "\n"])
def test_acceptance_criterion_create_rejects_whitespace_only_text(
    client: TestClient, blank: str
) -> None:
    """Whitespace-only acceptance-criterion text degrades the satisfied/total ratio.

    Codex flagged that ``min_length=1`` lets ``'   '`` through, which
    persists a meaningless criterion. Now rejected at the API boundary
    via the shared ``_reject_blank_str`` validator.
    """
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]
    eid = client.post(
        "/api/product-delivery/epics",
        json={"initiative_id": iid, "title": "E"},
    ).json()["id"]
    sid = client.post(
        "/api/product-delivery/stories",
        json={"epic_id": eid, "title": "S"},
    ).json()["id"]
    r = client.post(
        "/api/product-delivery/acceptance-criteria",
        json={"story_id": sid, "text": blank},
    )
    assert r.status_code == 422


@pytest.mark.parametrize("blank", [" ", "   ", "\t", "\n"])
def test_feedback_create_rejects_whitespace_only_source(client: TestClient, blank: str) -> None:
    """Blank ``source`` would corrupt feedback provenance / triage filters."""
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    r = client.post(
        "/api/product-delivery/feedback",
        json={"product_id": pid, "source": blank, "raw_payload": {"k": "v"}},
    )
    assert r.status_code == 422


def test_story_create_accepts_none_estimate_points(client: TestClient) -> None:
    """``None`` is the legitimate "unestimated" value — must still be accepted."""
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]
    eid = client.post(
        "/api/product-delivery/epics",
        json={"initiative_id": iid, "title": "E"},
    ).json()["id"]

    r = client.post(
        "/api/product-delivery/stories",
        json={"epic_id": eid, "title": "S", "estimate_points": None},
    )
    assert r.status_code == 200


@pytest.mark.parametrize("bad", ["Infinity", "-Infinity", "NaN"])
def test_story_create_rejects_non_finite_estimate_points(client: TestClient, bad: str) -> None:
    # Infinity passes `gt=0` but is non-finite; later it would propagate
    # into WSJF `job_size` / RICE `effort` and serialise as null/invalid.
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]
    eid = client.post(
        "/api/product-delivery/epics",
        json={"initiative_id": iid, "title": "E"},
    ).json()["id"]
    r = client.request(
        "POST",
        "/api/product-delivery/stories",
        content=(f'{{"epic_id": "{eid}", "title": "S", "estimate_points": "{bad}"}}'),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


def test_groom_returns_503_when_llm_call_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end LLM call failure (model unreachable, JSON parse blew up)
    # must propagate as 503 — same shape as a Postgres outage — so
    # callers retry instead of accepting a 200 with all-zero scores.
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]
    eid = client.post(
        "/api/product-delivery/epics",
        json={"initiative_id": iid, "title": "E"},
    ).json()["id"]
    client.post(
        "/api/product-delivery/stories",
        json={"epic_id": eid, "title": "S", "estimate_points": 5},
    )

    import sys
    import types

    stub_module = types.ModuleType("llm_service")

    class _BoomClient:
        def complete_json(self, *a: Any, **kw: Any) -> dict[str, Any]:
            raise RuntimeError("model unreachable")

    stub_module.get_client = lambda *a, **kw: _BoomClient()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llm_service", stub_module)

    resp = client.post(
        "/api/product-delivery/groom",
        json={"product_id": pid, "method": "wsjf"},
    )
    assert resp.status_code == 503
    assert "LLM scoring call failed" in resp.json()["detail"]


def test_groom_returns_503_when_llm_client_bootstrap_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `get_client("product_owner")` can fail on misconfigured provider
    # / missing credentials. The route must surface that as 503 (not a
    # bare 500) so clients see the same "transient infra" signal they
    # do for a Postgres outage. We need a non-empty backlog here —
    # an empty backlog short-circuits before the LLM bootstrap.
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]
    eid = client.post(
        "/api/product-delivery/epics",
        json={"initiative_id": iid, "title": "E"},
    ).json()["id"]
    client.post(
        "/api/product-delivery/stories",
        json={"epic_id": eid, "title": "S", "estimate_points": 5},
    )

    import sys
    import types

    stub_module = types.ModuleType("llm_service")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("OLLAMA_API_KEY missing")

    stub_module.get_client = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llm_service", stub_module)

    resp = client.post(
        "/api/product-delivery/groom",
        json={"product_id": pid, "method": "wsjf"},
    )
    assert resp.status_code == 503
    assert "LLM scoring call failed" in resp.json()["detail"]
    assert "OLLAMA_API_KEY missing" in resp.json()["detail"]


def test_feedback_list_404_for_unknown_product(client: TestClient) -> None:
    # Match the 404 semantics of /backlog, /groom, and feedback POST
    # when the product doesn't exist. Otherwise clients can't
    # distinguish "no feedback yet" from "you sent the wrong id".
    r = client.get("/api/product-delivery/feedback", params={"product_id": "ghost"})
    assert r.status_code == 404
    assert "ghost" in r.json()["detail"]


def test_feedback_unknown_product_with_valid_story_returns_404(client: TestClient) -> None:
    # When both product_id and linked_story_id are supplied and the
    # product doesn't exist, the route must return 404 (unknown product),
    # not 400 (cross-product link). Otherwise clients misclassify the
    # error and may trigger the wrong recovery flow.
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]
    eid = client.post(
        "/api/product-delivery/epics",
        json={"initiative_id": iid, "title": "E"},
    ).json()["id"]
    sid = client.post(
        "/api/product-delivery/stories",
        json={"epic_id": eid, "title": "S"},
    ).json()["id"]

    r = client.post(
        "/api/product-delivery/feedback",
        json={
            "product_id": "ghost-product",
            "source": "qa",
            "linked_story_id": sid,
        },
    )
    assert r.status_code == 404
    assert "ghost-product" in r.json()["detail"]


def test_feedback_create_and_list_filters_by_status(client: TestClient) -> None:
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    client.post(
        "/api/product-delivery/feedback",
        json={
            "product_id": pid,
            "source": "user-survey",
            "raw_payload": {"note": "love it"},
            "severity": "low",
        },
    )

    listed = client.get("/api/product-delivery/feedback", params={"product_id": pid}).json()
    assert len(listed) == 1
    assert listed[0]["status"] == "open"

    listed_open = client.get(
        "/api/product-delivery/feedback",
        params={"product_id": pid, "status": "open"},
    ).json()
    assert len(listed_open) == 1

    listed_closed = client.get(
        "/api/product-delivery/feedback",
        params={"product_id": pid, "status": "closed"},
    ).json()
    assert listed_closed == []


@pytest.mark.parametrize("filter_value", ["open ", " open", "  open  ", "open\t", "\nopen"])
def test_feedback_status_filter_normalised_before_query(
    client: TestClient, filter_value: str
) -> None:
    """Codex flagged: feedback statuses are normalised on write but the
    read filter used the raw query input, so ``status=open%20`` returned
    an empty list even when ``open`` rows existed. The store now strips
    the filter through the same ``_validate_status`` helper used on
    write, so the API/store contract holds.
    """
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    client.post(
        "/api/product-delivery/feedback",
        json={
            "product_id": pid,
            "source": "user-survey",
            "raw_payload": {},
            "severity": "low",
        },
    )
    listed = client.get(
        "/api/product-delivery/feedback",
        params={"product_id": pid, "status": filter_value},
    )
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 1, (
        f"filter_value={filter_value!r} should match the persisted 'open' status"
    )


def test_feedback_rejects_cross_product_story_link(client: TestClient) -> None:
    # Two products, each with one story. Linking a feedback item for
    # product A to a story that lives under product B must be rejected.
    pid_a = client.post("/api/product-delivery/products", json={"name": "A"}).json()["id"]
    pid_b = client.post("/api/product-delivery/products", json={"name": "B"}).json()["id"]
    iid_b = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid_b, "title": "I"},
    ).json()["id"]
    eid_b = client.post(
        "/api/product-delivery/epics",
        json={"initiative_id": iid_b, "title": "E"},
    ).json()["id"]
    sid_b = client.post(
        "/api/product-delivery/stories",
        json={"epic_id": eid_b, "title": "S"},
    ).json()["id"]

    resp = client.post(
        "/api/product-delivery/feedback",
        json={
            "product_id": pid_a,
            "source": "qa",
            "linked_story_id": sid_b,
        },
    )
    assert resp.status_code == 400
    assert "belongs to product" in resp.json()["detail"]


def test_feedback_accepts_same_product_story_link(client: TestClient) -> None:
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]
    eid = client.post(
        "/api/product-delivery/epics",
        json={"initiative_id": iid, "title": "E"},
    ).json()["id"]
    sid = client.post(
        "/api/product-delivery/stories",
        json={"epic_id": eid, "title": "S"},
    ).json()["id"]

    resp = client.post(
        "/api/product-delivery/feedback",
        json={"product_id": pid, "source": "qa", "linked_story_id": sid},
    )
    assert resp.status_code == 200
    assert resp.json()["linked_story_id"] == sid


def test_groom_returns_503_when_storage_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate a Postgres outage hitting the agent's
    # `store.list_stories_for_product` call (the new TOCTTOU-safe
    # entry point that combines product-existence + story-listing).
    # The route must return 503, not 500, so clients can retry the
    # same way they do for the other persistence endpoints.
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]

    from unified_api.routes import product_delivery as router_module

    from product_delivery.store import ProductDeliveryStorageUnavailable

    def _boom(_self, _product_id):  # type: ignore[no-untyped-def]
        raise ProductDeliveryStorageUnavailable("postgres is down")

    monkeypatch.setattr(router_module.get_store().__class__, "list_stories_for_product", _boom)

    resp = client.post(
        "/api/product-delivery/groom",
        json={"product_id": pid, "method": "wsjf"},
    )
    assert resp.status_code == 503
    assert "postgres is down" in resp.json()["detail"]


def test_groom_unknown_product_returns_404(client: TestClient) -> None:
    resp = client.post(
        "/api/product-delivery/groom",
        json={"product_id": "nope", "method": "wsjf"},
    )
    assert resp.status_code == 404


def test_groom_empty_backlog_does_not_bootstrap_llm(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Empty backlog short-circuits to GroomResult(ranked=[]) without any
    # LLM call. Even if `get_client` would raise, the route must return
    # 200 — otherwise newly-created products with zero stories 503
    # whenever the LLM provider is down.
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]

    import sys
    import types

    stub_module = types.ModuleType("llm_service")
    calls: list[tuple] = []

    def _boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("LLM provider down")

    stub_module.get_client = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llm_service", stub_module)

    resp = client.post(
        "/api/product-delivery/groom",
        json={"product_id": pid, "method": "wsjf"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ranked"] == []
    # `get_client` must not have been called for the empty-backlog path.
    assert calls == []


def test_groom_returns_503_when_llm_service_import_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Module-level import errors (missing strands/ollama, broken plugin)
    # must surface as 503, the same way `get_client` failures do — the
    # route's error contract is "transient infra problem, retry".
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]
    eid = client.post(
        "/api/product-delivery/epics",
        json={"initiative_id": iid, "title": "E"},
    ).json()["id"]
    client.post(
        "/api/product-delivery/stories",
        json={"epic_id": eid, "title": "S", "estimate_points": 5},
    )

    import sys

    # Make `from llm_service import get_client` fail at import time.
    class _BoomModule:
        def __getattr__(self, name):
            raise ImportError(f"llm_service plugin broken: {name}")

    monkeypatch.setitem(sys.modules, "llm_service", _BoomModule())

    resp = client.post(
        "/api/product-delivery/groom",
        json={"product_id": pid, "method": "wsjf"},
    )
    assert resp.status_code == 503
    assert "LLM scoring call failed" in resp.json()["detail"]


def test_groom_uses_stubbed_llm_and_returns_ranked_result(
    client_and_store: tuple[TestClient, _FakeStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stub the llm_service module before the route's lazy import runs, so the
    # test doesn't pull in strands / ollama just to swap out get_client.
    import sys
    import types

    client, fake = client_and_store
    stub_module = types.ModuleType("llm_service")
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]
    eid = client.post(
        "/api/product-delivery/epics",
        json={"initiative_id": iid, "title": "E"},
    ).json()["id"]
    sid_a = client.post(
        "/api/product-delivery/stories",
        json={"epic_id": eid, "title": "low", "estimate_points": 5},
    ).json()["id"]
    sid_b = client.post(
        "/api/product-delivery/stories",
        json={"epic_id": eid, "title": "high", "estimate_points": 5},
    ).json()["id"]

    class _Stub:
        def complete_json(self, prompt: str, **_: Any) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "id": sid_a,
                        "inputs": {
                            "user_business_value": 1,
                            "time_criticality": 1,
                            "risk_reduction_or_opportunity_enablement": 1,
                            "job_size": 5,
                        },
                        "rationale": "low",
                    },
                    {
                        "id": sid_b,
                        "inputs": {
                            "user_business_value": 9,
                            "time_criticality": 9,
                            "risk_reduction_or_opportunity_enablement": 9,
                            "job_size": 5,
                        },
                        "rationale": "high",
                    },
                ]
            }

    stub_module.get_client = lambda *a, **kw: _Stub()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llm_service", stub_module)

    resp = client.post(
        "/api/product-delivery/groom",
        json={"product_id": pid, "method": "wsjf", "persist": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["product_id"] == pid
    assert [r["id"] for r in body["ranked"]] == [sid_b, sid_a]
    # persistence ran on the fake
    assert fake.stories[sid_b].wsjf_score == body["ranked"][0]["score"]


# ---------------------------------------------------------------------------
# Sprints (Phase 2 of #243)
# ---------------------------------------------------------------------------


def _make_product_with_stories(
    client: TestClient,
    fake: _FakeStore,
    *,
    stories: list[dict[str, Any]],
) -> str:
    """Helper: create product → initiative → epic → N stories and apply
    optional ``wsjf_score`` patches via the fake's bucket directly.

    Stories don't accept ``wsjf_score`` on create (it lives on
    ``ScoreUpdate``), so we mutate the fake's row in place — that matches
    what the real store would do after a separate
    ``PATCH /story/{id}/scores`` call. Keeping the test setup terse
    avoids 5 HTTP calls per fixture.
    """
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]
    eid = client.post(
        "/api/product-delivery/epics",
        json={"initiative_id": iid, "title": "E"},
    ).json()["id"]
    for spec in stories:
        body: dict[str, Any] = {"epic_id": eid, "title": spec["title"]}
        if "estimate_points" in spec:
            body["estimate_points"] = spec["estimate_points"]
        sid = client.post("/api/product-delivery/stories", json=body).json()["id"]
        wsjf = spec.get("wsjf")
        if wsjf is not None:
            fake.stories[sid] = fake.stories[sid].model_copy(update={"wsjf_score": wsjf})
        # Tag the test-supplied id alias for assertions.
        spec["_id"] = sid
    return pid


def test_sprint_create_round_trip(client_and_store: tuple[TestClient, _FakeStore]) -> None:
    client, fake = client_and_store
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    resp = client.post(
        "/api/product-delivery/sprints",
        json={
            "product_id": pid,
            "name": "S1",
            "capacity_points": 13,
            "status": "planned",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "S1"
    assert body["capacity_points"] == 13.0
    assert body["status"] == "planned"
    assert body["author"] == "tester"
    assert body["id"] in fake.sprints


def test_sprint_create_404_when_product_missing(client: TestClient) -> None:
    r = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": "missing", "name": "S1", "capacity_points": 5},
    )
    assert r.status_code == 404


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_sprint_create_rejects_non_finite_or_negative_capacity(
    client: TestClient, bad: float
) -> None:
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    # JSON itself doesn't allow NaN/Infinity literals — send via raw
    # content so Pydantic's coercion exercises the validator.
    if bad != bad or bad in (float("inf"), float("-inf")):
        token = "NaN" if bad != bad else ("Infinity" if bad > 0 else "-Infinity")
        r = client.request(
            "POST",
            "/api/product-delivery/sprints",
            content=f'{{"product_id": "{pid}", "name": "S", "capacity_points": "{token}"}}',
            headers={"content-type": "application/json"},
        )
    else:
        r = client.post(
            "/api/product-delivery/sprints",
            json={"product_id": pid, "name": "S", "capacity_points": bad},
        )
    assert r.status_code == 422


def test_sprint_plan_selects_highest_wsjf_within_capacity(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    client, fake = client_and_store
    stories = [
        {"title": "low", "estimate_points": 5, "wsjf": 1.0},
        {"title": "mid", "estimate_points": 5, "wsjf": 5.0},
        {"title": "high", "estimate_points": 5, "wsjf": 9.0},
    ]
    pid = _make_product_with_stories(client, fake, stories=stories)
    sid = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S1", "capacity_points": 10},
    ).json()["id"]
    plan = client.post(
        f"/api/product-delivery/sprints/{sid}/plan",
        json={},  # use sprint row's stored capacity (10)
    ).json()
    # Capacity 10, three 5-point stories — pick the top two by WSJF.
    selected_titles = {fake.stories[s].title for s in plan["selected_story_ids"]}
    skipped_titles = {fake.stories[s].title for s in plan["skipped_story_ids"]}
    assert selected_titles == {"high", "mid"}
    assert skipped_titles == {"low"}
    assert plan["used_capacity"] == 10.0
    assert plan["remaining_capacity"] == 0.0


def test_sprint_plan_zero_capacity_picks_nothing(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    client, fake = client_and_store
    pid = _make_product_with_stories(
        client,
        fake,
        stories=[{"title": "a", "estimate_points": 1, "wsjf": 5.0}],
    )
    sid = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S0", "capacity_points": 0},
    ).json()["id"]
    plan = client.post(
        f"/api/product-delivery/sprints/{sid}/plan", json={"capacity_points": 0}
    ).json()
    assert plan["selected_story_ids"] == []
    assert len(plan["skipped_story_ids"]) == 1


def test_sprint_plan_story_bigger_than_capacity_is_skipped(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    client, fake = client_and_store
    pid = _make_product_with_stories(
        client,
        fake,
        stories=[
            {"title": "huge", "estimate_points": 100, "wsjf": 9.0},
            {"title": "small", "estimate_points": 1, "wsjf": 1.0},
        ],
    )
    sid = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S1", "capacity_points": 5},
    ).json()["id"]
    plan = client.post(
        f"/api/product-delivery/sprints/{sid}/plan", json={"capacity_points": 5}
    ).json()
    selected_titles = {fake.stories[s].title for s in plan["selected_story_ids"]}
    # `huge` exceeds capacity even though it has highest WSJF; greedy
    # rolls forward to the next-best fit.
    assert selected_titles == {"small"}


def test_sprint_plan_null_estimate_points_is_size_zero(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    client, fake = client_and_store
    pid = _make_product_with_stories(
        client,
        fake,
        stories=[
            {"title": "unestimated", "wsjf": 9.0},  # estimate_points absent → None
            {"title": "sized", "estimate_points": 3, "wsjf": 5.0},
        ],
    )
    sid = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S1", "capacity_points": 3},
    ).json()["id"]
    plan = client.post(
        f"/api/product-delivery/sprints/{sid}/plan", json={"capacity_points": 3}
    ).json()
    titles = {fake.stories[s].title for s in plan["selected_story_ids"]}
    # Both fit: the unestimated story counts as 0 points, leaving the
    # full 3-point budget for the sized one.
    assert titles == {"unestimated", "sized"}
    assert plan["used_capacity"] == 3.0


def test_sprint_plan_excludes_stories_already_in_other_sprint(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    client, fake = client_and_store
    pid = _make_product_with_stories(
        client,
        fake,
        stories=[
            {"title": "a", "estimate_points": 2, "wsjf": 5.0},
            {"title": "b", "estimate_points": 2, "wsjf": 4.0},
        ],
    )
    s1 = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S1", "capacity_points": 2},
    ).json()["id"]
    plan1 = client.post(
        f"/api/product-delivery/sprints/{s1}/plan", json={"capacity_points": 2}
    ).json()
    assert len(plan1["selected_story_ids"]) == 1
    locked_id = plan1["selected_story_ids"][0]

    s2 = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S2", "capacity_points": 100},
    ).json()["id"]
    plan2 = client.post(
        f"/api/product-delivery/sprints/{s2}/plan", json={"capacity_points": 100}
    ).json()
    # The story already planned into S1 must not show up in S2's pool.
    assert locked_id not in plan2["selected_story_ids"]
    assert locked_id not in plan2["skipped_story_ids"]


def test_get_sprint_returns_planned_stories_ordered_by_wsjf(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    client, fake = client_and_store
    pid = _make_product_with_stories(
        client,
        fake,
        stories=[
            {"title": "a", "estimate_points": 1, "wsjf": 1.0},
            {"title": "c", "estimate_points": 1, "wsjf": 9.0},
            {"title": "b", "estimate_points": 1, "wsjf": 5.0},
        ],
    )
    sid = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S1", "capacity_points": 100},
    ).json()["id"]
    client.post(f"/api/product-delivery/sprints/{sid}/plan", json={"capacity_points": 100})
    body = client.get(f"/api/product-delivery/sprints/{sid}").json()
    # Highest WSJF first.
    assert [s["title"] for s in body["stories"]] == ["c", "b", "a"]


def test_get_sprint_404_when_missing(client: TestClient) -> None:
    r = client.get("/api/product-delivery/sprints/missing")
    assert r.status_code == 404


def test_sprint_plan_404_when_sprint_missing(client: TestClient) -> None:
    r = client.post("/api/product-delivery/sprints/missing/plan", json={})
    assert r.status_code == 404


def test_sprint_plan_re_run_does_not_over_commit_capacity(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    """Re-running plan on a partially-planned sprint must respect existing scope.

    Codex flagged on PR #396 that the previous implementation always
    started ``used`` at 0.0, so a second `/plan` call would fit
    another full-capacity batch on top of the first.
    """
    client, fake = client_and_store
    pid = _make_product_with_stories(
        client,
        fake,
        stories=[
            {"title": "a", "estimate_points": 3, "wsjf": 9.0},
            {"title": "b", "estimate_points": 3, "wsjf": 5.0},
            {"title": "c", "estimate_points": 3, "wsjf": 1.0},
        ],
    )
    sid = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S1", "capacity_points": 5},
    ).json()["id"]
    plan1 = client.post(
        f"/api/product-delivery/sprints/{sid}/plan", json={"capacity_points": 5}
    ).json()
    # First call fits one 3-point story (highest WSJF) under capacity 5.
    assert len(plan1["selected_story_ids"]) == 1
    assert plan1["used_capacity"] == 3.0
    assert plan1["remaining_capacity"] == 2.0

    plan2 = client.post(
        f"/api/product-delivery/sprints/{sid}/plan", json={"capacity_points": 5}
    ).json()
    # Second call: budget is `5 - 3 = 2`, no remaining 3-point story
    # fits, so nothing new is selected. Total used is still 3.0.
    assert plan2["selected_story_ids"] == []
    assert plan2["used_capacity"] == 3.0
    assert plan2["remaining_capacity"] == 2.0


def test_sprint_plan_skips_done_stories(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    """Stories in a terminal status are not re-planned (Codex review on PR #396)."""
    client, fake = client_and_store
    stories = [
        {"title": "active", "estimate_points": 1, "wsjf": 5.0},
        {"title": "done", "estimate_points": 1, "wsjf": 9.0},  # higher WSJF, but done
    ]
    pid = _make_product_with_stories(client, fake, stories=stories)
    # Mark the second story as done via the fake bucket directly.
    done_id = stories[1]["_id"]
    fake.stories[done_id] = fake.stories[done_id].model_copy(update={"status": "done"})

    sid = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S1", "capacity_points": 100},
    ).json()["id"]
    plan = client.post(
        f"/api/product-delivery/sprints/{sid}/plan", json={"capacity_points": 100}
    ).json()
    # Only the active story is selected; the done one is filtered out
    # before the capacity check (so it doesn't appear in skipped either).
    titles = {fake.stories[s].title for s in plan["selected_story_ids"]}
    assert titles == {"active"}
    assert done_id not in plan["selected_story_ids"]
    assert done_id not in plan["skipped_story_ids"]


@pytest.mark.parametrize("terminal_status", ["Done", "DONE", " done ", "Cancelled"])
def test_sprint_plan_terminal_filter_is_case_insensitive(
    client_and_store: tuple[TestClient, _FakeStore],
    terminal_status: str,
) -> None:
    """Status comparison must be case-insensitive (Codex review on PR #396).

    `_validate_status` only strips whitespace; without `LOWER(s.status)`
    in the candidate query a row stored as `Done` would otherwise
    smuggle past the lowercase ``TERMINAL_STORY_STATUSES`` set.
    """
    client, fake = client_and_store
    stories = [
        {"title": "active", "estimate_points": 1, "wsjf": 5.0},
        {"title": "x", "estimate_points": 1, "wsjf": 9.0},
    ]
    pid = _make_product_with_stories(client, fake, stories=stories)
    target_id = stories[1]["_id"]
    fake.stories[target_id] = fake.stories[target_id].model_copy(update={"status": terminal_status})

    sid = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S1", "capacity_points": 100},
    ).json()["id"]
    plan = client.post(
        f"/api/product-delivery/sprints/{sid}/plan", json={"capacity_points": 100}
    ).json()
    # The mixed-case terminal status must still be filtered out.
    assert target_id not in plan["selected_story_ids"]
    assert target_id not in plan["skipped_story_ids"]


def test_plan_endpoint_accepts_empty_body(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    """``POST /sprints/{id}/plan`` should work with no body — falls back
    to the sprint row's stored capacity (Codex review on PR #396).
    """
    client, fake = client_and_store
    pid = _make_product_with_stories(
        client,
        fake,
        stories=[{"title": "a", "estimate_points": 1, "wsjf": 5.0}],
    )
    sid = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S1", "capacity_points": 5},
    ).json()["id"]
    # No body at all.
    r1 = client.post(f"/api/product-delivery/sprints/{sid}/plan")
    assert r1.status_code == 200, r1.text
    # Empty JSON body.
    r2 = client.post(f"/api/product-delivery/sprints/{sid}/plan", json={})
    assert r2.status_code == 200, r2.text


def test_get_sprint_returns_acs_alongside_stories(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    """``get_sprint_with_stories`` populates ``acceptance_criteria_by_story_id``
    so callers don't need follow-up queries (Codex review on PR #396).
    """
    client, fake = client_and_store
    pid = _make_product_with_stories(
        client,
        fake,
        stories=[{"title": "x", "estimate_points": 1, "wsjf": 5.0}],
    )
    sid = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S1", "capacity_points": 5},
    ).json()["id"]
    story_id = next(s.id for s in fake.stories.values() if s.title == "x")
    client.post(
        "/api/product-delivery/acceptance-criteria",
        json={"story_id": story_id, "text": "must work"},
    )
    client.post(f"/api/product-delivery/sprints/{sid}/plan", json={})
    body = client.get(f"/api/product-delivery/sprints/{sid}").json()
    # ACs are nested under `acceptance_criteria_by_story_id` keyed by id.
    assert "acceptance_criteria_by_story_id" in body
    assert [ac["text"] for ac in body["acceptance_criteria_by_story_id"][story_id]] == ["must work"]


def test_create_sprint_rejects_inverted_window(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    """``ends_at < starts_at`` must 422 (Codex review on PR #396)."""
    client, _ = client_and_store
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    r = client.post(
        "/api/product-delivery/sprints",
        json={
            "product_id": pid,
            "name": "S1",
            "capacity_points": 5,
            "starts_at": "2026-05-01T00:00:00Z",
            "ends_at": "2026-04-01T00:00:00Z",
        },
    )
    assert r.status_code == 422


def test_create_sprint_rejects_naive_datetime(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    """Naive timestamps (no tz suffix) must 422 — otherwise the post-
    validator's ``ends_at < starts_at`` compare can hit a tz-aware
    vs. naive mix and raise ``TypeError`` -> 500 (Codex review on PR #396).
    """
    client, _ = client_and_store
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    # Mixed: aware start, naive end.
    r = client.post(
        "/api/product-delivery/sprints",
        json={
            "product_id": pid,
            "name": "S1",
            "capacity_points": 5,
            "starts_at": "2026-05-01T00:00:00Z",
            "ends_at": "2026-05-15T00:00:00",  # no tz
        },
    )
    assert r.status_code == 422


def test_validate_sprint_window_rejects_single_naive_endpoint() -> None:
    """`_validate_sprint_window` must reject a single naive bound on its
    own — even when the other side is None (Codex review on PR #396).
    Otherwise non-route callers could slip a naive `starts_at` through,
    insert the row, and crash inside the post-commit `Sprint(...)`
    validation, leaving invalid persisted data behind.
    """
    from datetime import datetime, timezone

    from product_delivery.store import _validate_sprint_window

    naive = datetime(2026, 5, 1, 0, 0, 0)  # no tz
    aware = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)

    # Both None → no-op.
    _validate_sprint_window(None, None)
    # One side None + other side aware → no-op.
    _validate_sprint_window(aware, None)
    _validate_sprint_window(None, aware)
    # Single-ended naive must raise.
    with pytest.raises(ValueError, match="starts_at must be timezone-aware"):
        _validate_sprint_window(naive, None)
    with pytest.raises(ValueError, match="ends_at must be timezone-aware"):
        _validate_sprint_window(None, naive)
    # Mixed naive/aware also raises.
    with pytest.raises(ValueError, match="ends_at must be timezone-aware"):
        _validate_sprint_window(aware, naive)


def test_add_story_to_sprint_rejects_cross_product_assignment(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    """A story under product A cannot be added to a sprint under product B
    (Codex review on PR #396). Mapped to 400 by the route handler.
    """
    client, fake = client_and_store
    # Product A with a story
    pid_a = _make_product_with_stories(
        client,
        fake,
        stories=[{"title": "a-story", "estimate_points": 1, "wsjf": 5.0}],
    )
    story_a_id = next(s.id for s in fake.stories.values() if s.title == "a-story")
    # Product B with a sprint
    pid_b = client.post("/api/product-delivery/products", json={"name": "B"}).json()["id"]
    sprint_b = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid_b, "name": "B-1", "capacity_points": 5},
    ).json()["id"]
    # Manual cross-product assignment via fake store — the real store
    # rejects with `CrossProductSprintAssignment` and the route maps
    # it to 400.
    with pytest.raises(CrossProductSprintAssignment):
        fake.add_story_to_sprint(sprint_id=sprint_b, story_id=story_a_id)
    # Sanity: same-product still works.
    assert pid_a != pid_b


def test_list_releases_for_sprint_404_when_sprint_missing(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    """`list_releases_for_sprint` distinguishes 404 from 200 [] (Codex review)."""
    _, fake = client_and_store
    with pytest.raises(UnknownProductDeliveryEntity):
        fake.list_releases_for_sprint("missing")


def test_add_story_to_sprint_409_when_story_already_in_other_sprint(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    """Schema enforces one-sprint-per-story (Codex review on PR #396).

    The route layer doesn't expose ``add_story_to_sprint`` directly
    (planner does), but the underlying constraint should still
    surface as 409 when a planner ever tries to plant the same story
    into a different sprint. We exercise the fake's invariant directly
    via the store's ``StoryAlreadyPlanned`` raise — the route's
    exception handler maps it to 409.
    """
    client, fake = client_and_store
    pid = _make_product_with_stories(
        client,
        fake,
        stories=[{"title": "a", "estimate_points": 1, "wsjf": 5.0}],
    )
    s1 = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S1", "capacity_points": 5},
    ).json()["id"]
    s2 = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S2", "capacity_points": 5},
    ).json()["id"]
    # Plant the only story into S1 via the planner.
    plan1 = client.post(
        f"/api/product-delivery/sprints/{s1}/plan", json={"capacity_points": 5}
    ).json()
    assert len(plan1["selected_story_ids"]) == 1

    # Manually attempt to plant it into S2 via the fake store directly —
    # mirrors what a concurrent racing planner would hit at the schema
    # level. The store raises StoryAlreadyPlanned, which the route's
    # global handler maps to 409.
    with pytest.raises(StoryAlreadyPlanned):
        fake.add_story_to_sprint(sprint_id=s2, story_id=plan1["selected_story_ids"][0])


# ---------------------------------------------------------------------------
# Releases (Phase 3 of #243 / #371) — POST /releases, GET /releases?product_id
# ---------------------------------------------------------------------------


def test_create_release_via_post_returns_row(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    """`POST /releases` records a release row for an existing sprint."""
    client, _ = client_and_store
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    sid = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S1", "capacity_points": 5},
    ).json()["id"]
    resp = client.post(
        "/api/product-delivery/releases",
        json={
            "sprint_id": sid,
            "version": "2026-05-02",
            "notes_path": "/repo/plan/releases/2026-05-02.md",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sprint_id"] == sid
    assert body["version"] == "2026-05-02"
    assert body["notes_path"].endswith("2026-05-02.md")
    assert body["author"] == "tester"


def test_create_release_unknown_sprint_returns_404(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    client, _ = client_and_store
    resp = client.post(
        "/api/product-delivery/releases",
        json={"sprint_id": "missing", "version": "v1"},
    )
    assert resp.status_code == 404


def test_list_releases_for_product_orders_shipped_first(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    """`GET /releases?product_id=…` returns every release across sprints.

    Sort order: ``shipped_at`` desc with NULLS LAST, then ``created_at``
    desc. Verifies the cross-sprint join + order contract that backs
    AC #3 ("manual smoke: release file appears → GET /releases lists it").
    """
    client, _ = client_and_store
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    s1 = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S1", "capacity_points": 5},
    ).json()["id"]
    s2 = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S2", "capacity_points": 5},
    ).json()["id"]
    client.post(
        "/api/product-delivery/releases",
        json={
            "sprint_id": s1,
            "version": "2026-05-01",
            "shipped_at": "2026-05-01T12:00:00Z",
        },
    )
    client.post(
        "/api/product-delivery/releases",
        json={
            "sprint_id": s2,
            "version": "2026-05-02",
            "shipped_at": "2026-05-02T12:00:00Z",
        },
    )
    # Unshipped — falls to the tail under NULLS LAST.
    client.post(
        "/api/product-delivery/releases",
        json={"sprint_id": s2, "version": "preview"},
    )
    resp = client.get(f"/api/product-delivery/releases?product_id={pid}")
    assert resp.status_code == 200, resp.text
    versions = [r["version"] for r in resp.json()]
    assert versions[0] == "2026-05-02"
    assert versions[1] == "2026-05-01"
    assert versions[-1] == "preview"


def test_list_releases_unknown_product_returns_404(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    client, _ = client_and_store
    resp = client.get("/api/product-delivery/releases?product_id=missing")
    assert resp.status_code == 404


def test_create_feedback_with_sprint_id_round_trips(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    """`POST /feedback` accepts a `sprint_id` (#371) and round-trips it back."""
    client, _ = client_and_store
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    sid = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S1", "capacity_points": 5},
    ).json()["id"]
    resp = client.post(
        "/api/product-delivery/feedback",
        json={
            "product_id": pid,
            "source": "se-integration",
            "raw_payload": {"description": "missing endpoint"},
            "severity": "high",
            "sprint_id": sid,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sprint_id"] == sid
    assert body["severity"] == "high"


def test_create_feedback_with_unknown_sprint_id_returns_404(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    client, _ = client_and_store
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    resp = client.post(
        "/api/product-delivery/feedback",
        json={
            "product_id": pid,
            "source": "se-integration",
            "raw_payload": {},
            "severity": "high",
            "sprint_id": "missing",
        },
    )
    assert resp.status_code == 404


def test_create_feedback_rejects_cross_product_sprint(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    """Codex P2 review (PR #424): a feedback row for product A must not be
    tagged with a sprint under product B.

    The two FKs on ``feedback_items`` (``product_id`` → product,
    ``sprint_id`` → sprint) can't enforce the transitive
    sprint→product invariant on their own — we validate at the store
    layer and surface as 400, the same shape as the existing
    ``CrossProductFeedbackLink`` story-link check.
    """
    client, _ = client_and_store
    pid_a = client.post("/api/product-delivery/products", json={"name": "A"}).json()["id"]
    pid_b = client.post("/api/product-delivery/products", json={"name": "B"}).json()["id"]
    sid_b = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid_b, "name": "Sprint-B", "capacity_points": 5},
    ).json()["id"]

    resp = client.post(
        "/api/product-delivery/feedback",
        json={
            "product_id": pid_a,
            "source": "se-integration",
            "raw_payload": {},
            "severity": "high",
            "sprint_id": sid_b,
        },
    )
    assert resp.status_code == 400, resp.text
    assert "sprint" in resp.json()["detail"].lower()


def test_post_releases_gates_on_sprint_completion(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    """Codex P1 review (PR #424): manual ``POST /releases`` must not
    mint a "shipped" release row for a sprint with open stories.
    Mirrors the in-process ``ReleaseManagerAgent`` invariant.
    """
    client, fake = client_and_store
    pid = _make_product_with_stories(
        client,
        fake,
        stories=[{"title": "s1", "estimate_points": 3, "wsjf": 5.0}],
    )
    sid = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S1", "capacity_points": 5},
    ).json()["id"]
    # Plan the story into the sprint so the sprint has work to ship.
    plan = client.post(f"/api/product-delivery/sprints/{sid}/plan").json()
    assert plan["selected_story_ids"], "sprint should have at least one planned story"

    # The story is still in its default 'proposed' status — sprint is
    # not complete; release attempt must be rejected.
    resp = client.post(
        "/api/product-delivery/releases",
        json={"sprint_id": sid, "version": "premature"},
    )
    assert resp.status_code == 409, resp.text
    assert "open" in resp.json()["detail"].lower()


def test_post_releases_rejects_duplicate_sprint_version(
    client_and_store: tuple[TestClient, _FakeStore],
) -> None:
    """Codex P2 review (PR #424): the schema's ``UNIQUE(sprint_id, version)``
    surfaces as 409 instead of silently writing a second row pointing
    at the same notes file.
    """
    client, fake = client_and_store
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    sid = client.post(
        "/api/product-delivery/sprints",
        json={"product_id": pid, "name": "S1", "capacity_points": 5},
    ).json()["id"]
    # First release lands.
    r1 = client.post(
        "/api/product-delivery/releases",
        json={"sprint_id": sid, "version": "v1.0.0"},
    )
    assert r1.status_code == 200, r1.text

    # Mirror the schema-level UNIQUE constraint in the fake store: a
    # second insert with the same (sprint_id, version) pair must
    # raise ``DuplicateReleaseVersion``. The route's exception handler
    # then maps it to 409.
    from product_delivery.store import DuplicateReleaseVersion

    original = fake.create_release

    def _enforce_unique(**kwargs: Any) -> Release:
        existing = [
            r
            for r in fake.releases.values()
            if r.sprint_id == kwargs["sprint_id"] and r.version == kwargs["version"]
        ]
        if existing:
            raise DuplicateReleaseVersion(
                f"release {kwargs['version']!r} for sprint {kwargs['sprint_id']!r} already exists"
            )
        return original(**kwargs)

    fake.create_release = _enforce_unique  # type: ignore[assignment]

    r2 = client.post(
        "/api/product-delivery/releases",
        json={"sprint_id": sid, "version": "v1.0.0"},
    )
    assert r2.status_code == 409, r2.text
    assert "v1.0.0" in r2.json()["detail"]
