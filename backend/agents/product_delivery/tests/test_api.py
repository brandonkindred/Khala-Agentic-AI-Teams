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
    Story,
    StoryNode,
    Task,
)
from product_delivery.store import (
    CrossProductFeedbackLink,
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
        return self._insert(
            "feedback",
            product_id=product_id,
            source=source,
            raw_payload=raw_payload,
            severity=severity,
            status="open",
            linked_story_id=linked_story_id,
            author=author,
        )

    def list_feedback(self, product_id: str, *, status: str | None = None) -> list[FeedbackItem]:
        out = [f for f in self.feedback.values() if f.product_id == product_id]
        if status is not None:
            out = [f for f in out if f.status == status]
        return out


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
    # Simulate a Postgres outage hitting `store.get_product` (the failure
    # path covered by the P1 review comment). The route must return 503,
    # not 500, so clients can retry the same way they do for the
    # CRUD endpoints.
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]

    from unified_api.routes import product_delivery as router_module

    from product_delivery.store import ProductDeliveryStorageUnavailable

    def _boom(_self, _product_id):  # type: ignore[no-untyped-def]
        raise ProductDeliveryStorageUnavailable("postgres is down")

    monkeypatch.setattr(router_module.get_store().__class__, "get_product", _boom)

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
