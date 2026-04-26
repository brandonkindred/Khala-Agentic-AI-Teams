"""API tests for the product_delivery router.

Routes are exercised through a FastAPI ``TestClient`` against a minimal
app that mounts only the product_delivery router. The store is replaced
with an in-memory fake via ``monkeypatch`` so these tests do not require
Postgres.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

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


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class _FakeStore:
    """In-memory implementation of the subset of ProductDeliveryStore the API uses."""

    def __init__(self) -> None:
        self.products: dict[str, Product] = {}
        self.initiatives: dict[str, Initiative] = {}
        self.epics: dict[str, Epic] = {}
        self.stories: dict[str, Story] = {}
        self.tasks: dict[str, Task] = {}
        self.acs: dict[str, AcceptanceCriterion] = {}
        self.feedback: dict[str, FeedbackItem] = {}
        self._next = 0

    def _id(self) -> str:
        self._next += 1
        return f"id{self._next}"

    # products ----------------------------------------------------------

    def create_product(self, *, name: str, description: str, vision: str, author: str) -> Product:
        pid = self._id()
        now = _now()
        product = Product(
            id=pid,
            name=name,
            description=description,
            vision=vision,
            author=author,
            created_at=now,
            updated_at=now,
        )
        self.products[pid] = product
        return product

    def list_products(self) -> list[Product]:
        return list(self.products.values())

    def get_product(self, product_id: str) -> Product | None:
        return self.products.get(product_id)

    # initiatives / epics / stories / tasks / ac ------------------------

    def create_initiative(
        self,
        *,
        product_id: str,
        title: str,
        summary: str,
        status: str,
        author: str,
    ) -> Initiative:
        from product_delivery.store import UnknownProductDeliveryEntity

        if product_id not in self.products:
            raise UnknownProductDeliveryEntity(f"product {product_id!r} does not exist")
        iid = self._id()
        now = _now()
        i = Initiative(
            id=iid,
            product_id=product_id,
            title=title,
            summary=summary,
            status=status,
            author=author,
            created_at=now,
            updated_at=now,
        )
        self.initiatives[iid] = i
        return i

    def create_epic(
        self,
        *,
        initiative_id: str,
        title: str,
        summary: str,
        status: str,
        author: str,
    ) -> Epic:
        from product_delivery.store import UnknownProductDeliveryEntity

        if initiative_id not in self.initiatives:
            raise UnknownProductDeliveryEntity(f"initiative {initiative_id!r} does not exist")
        eid = self._id()
        now = _now()
        e = Epic(
            id=eid,
            initiative_id=initiative_id,
            title=title,
            summary=summary,
            status=status,
            author=author,
            created_at=now,
            updated_at=now,
        )
        self.epics[eid] = e
        return e

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
        from product_delivery.store import UnknownProductDeliveryEntity

        if epic_id not in self.epics:
            raise UnknownProductDeliveryEntity(f"epic {epic_id!r} does not exist")
        sid = self._id()
        now = _now()
        s = Story(
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
        self.stories[sid] = s
        return s

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
        from product_delivery.store import UnknownProductDeliveryEntity

        if story_id not in self.stories:
            raise UnknownProductDeliveryEntity(f"story {story_id!r} does not exist")
        tid = self._id()
        now = _now()
        t = Task(
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
        self.tasks[tid] = t
        return t

    def create_acceptance_criterion(
        self, *, story_id: str, text: str, satisfied: bool, author: str
    ) -> AcceptanceCriterion:
        from product_delivery.store import UnknownProductDeliveryEntity

        if story_id not in self.stories:
            raise UnknownProductDeliveryEntity(f"story {story_id!r} does not exist")
        aid = self._id()
        now = _now()
        ac = AcceptanceCriterion(
            id=aid,
            story_id=story_id,
            text=text,
            satisfied=satisfied,
            author=author,
            created_at=now,
            updated_at=now,
        )
        self.acs[aid] = ac
        return ac

    # status / score updates -------------------------------------------

    _kinds = {
        "initiative": "initiatives",
        "epic": "epics",
        "story": "stories",
        "task": "tasks",
    }

    def update_status(self, *, kind: str, entity_id: str, status: str) -> bool:
        attr = self._kinds.get(kind)
        if attr is None:
            raise ValueError(kind)
        bag = getattr(self, attr)
        if entity_id not in bag:
            return False
        existing = bag[entity_id]
        bag[entity_id] = existing.model_copy(update={"status": status, "updated_at": _now()})
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
        attr = self._kinds.get(kind)
        if attr is None:
            raise ValueError(kind)
        bag = getattr(self, attr)
        if entity_id not in bag:
            return False
        update: dict[str, Any] = {"updated_at": _now()}
        if wsjf_score is not None:
            update["wsjf_score"] = wsjf_score
        if rice_score is not None:
            update["rice_score"] = rice_score
        bag[entity_id] = bag[entity_id].model_copy(update=update)
        return True

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
        from product_delivery.store import (
            CrossProductFeedbackLink,
            UnknownProductDeliveryEntity,
        )

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
        fid = self._id()
        now = _now()
        f = FeedbackItem(
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
        self.feedback[fid] = f
        return f

    def list_feedback(self, product_id: str, *, status: str | None = None) -> list[FeedbackItem]:
        out = [f for f in self.feedback.values() if f.product_id == product_id]
        if status is not None:
            out = [f for f in out if f.status == status]
        return out


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from unified_api.routes import product_delivery as router_module

    fake = _FakeStore()
    monkeypatch.setattr(router_module, "get_store", lambda: fake)
    monkeypatch.setattr(router_module, "resolve_author", lambda: "tester")

    app = FastAPI()
    app.include_router(router_module.router)
    test_client = TestClient(app)
    test_client.fake_store = fake  # type: ignore[attr-defined]
    return test_client


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


def test_status_and_score_patches_apply(client: TestClient) -> None:
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

    fake = client.fake_store  # type: ignore[attr-defined]
    assert fake.initiatives[iid].status == "in_sprint"
    assert fake.initiatives[iid].wsjf_score == 12.5
    assert fake.initiatives[iid].rice_score == 80.0


def test_status_patch_404_for_unknown_id(client: TestClient) -> None:
    r = client.patch(
        "/api/product-delivery/story/missing/status",
        json={"status": "done"},
    )
    assert r.status_code == 404


def test_score_patch_empty_body_returns_400(client: TestClient) -> None:
    # Empty (or all-null) score payload is a client error, not a 404 —
    # otherwise clients can't tell "you sent nothing" from "the entity
    # doesn't exist" and may incorrectly trigger a create/retry flow.
    pid = client.post("/api/product-delivery/products", json={"name": "P"}).json()["id"]
    iid = client.post(
        "/api/product-delivery/initiatives",
        json={"product_id": pid, "title": "I"},
    ).json()["id"]

    r = client.patch(f"/api/product-delivery/initiative/{iid}/scores", json={})
    assert r.status_code == 400
    assert "wsjf_score" in r.json()["detail"]

    r = client.patch(
        f"/api/product-delivery/initiative/{iid}/scores",
        json={"wsjf_score": None, "rice_score": None},
    )
    assert r.status_code == 400


def test_score_patch_404_for_unknown_id_with_real_payload(client: TestClient) -> None:
    # Sanity check: a well-formed payload against an unknown id is still 404.
    r = client.patch(
        "/api/product-delivery/initiative/missing/scores",
        json={"wsjf_score": 1.0},
    )
    assert r.status_code == 404


def test_story_create_rejects_non_positive_estimate_points(client: TestClient) -> None:
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

    for bad in (0, -3.5):
        r = client.post(
            "/api/product-delivery/stories",
            json={"epic_id": eid, "title": "S", "estimate_points": bad},
        )
        assert r.status_code == 422, f"estimate_points={bad} should be rejected"

    # None is still allowed (means "unestimated").
    r = client.post(
        "/api/product-delivery/stories",
        json={"epic_id": eid, "title": "S", "estimate_points": None},
    )
    assert r.status_code == 200


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


def test_groom_uses_stubbed_llm_and_returns_ranked_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stub the llm_service module before the route's lazy import runs, so the
    # test doesn't pull in strands / ollama just to swap out get_client.
    import sys
    import types

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
    assert client.fake_store.stories[sid_b].wsjf_score == body["ranked"][0]["score"]  # type: ignore[attr-defined]
