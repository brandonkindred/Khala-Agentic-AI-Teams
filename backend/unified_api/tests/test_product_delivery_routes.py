"""Hermetic route-level tests for /api/product-delivery.

get_store() is monkeypatched directly on the routes module with a MagicMock
returning real product_delivery Pydantic model instances, so these tests
never touch Postgres. /groom and /sprints/{id}/plan (which construct
ProductOwnerAgent / SprintPlannerAgent) are covered separately with the
agent classes stubbed.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import unified_api.routes.product_delivery as routes_mod
from product_delivery import (
    AcceptanceCriterion,
    CrossProductFeedbackLink,
    Epic,
    FeedbackItem,
    Initiative,
    Product,
    Release,
    Sprint,
    SprintNotComplete,
    SprintWithStories,
    Story,
    Task,
    UnknownProductDeliveryEntity,
)

_NOW = datetime.now(tz=timezone.utc)


def _product(**overrides) -> Product:
    base = {"id": "p1", "author": "tester", "created_at": _NOW, "updated_at": _NOW, "name": "Widget"}
    base.update(overrides)
    return Product(**base)


@pytest.fixture()
def app_and_store():
    app = FastAPI()
    routes_mod.register_pd_exception_handlers(app)
    app.include_router(routes_mod.router)
    fake_store = MagicMock()
    original_get_store = routes_mod.get_store
    routes_mod.get_store = lambda: fake_store
    try:
        yield app, fake_store
    finally:
        routes_mod.get_store = original_get_store


@pytest.fixture()
def client(app_and_store) -> TestClient:
    app, _store = app_and_store
    return TestClient(app)


@pytest.fixture()
def store(app_and_store):
    return app_and_store[1]


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


def test_create_product(client: TestClient, store: MagicMock) -> None:
    store.create_product.return_value = _product()
    resp = client.post("/api/product-delivery/products", json={"name": "Widget"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Widget"


def test_list_products(client: TestClient, store: MagicMock) -> None:
    store.list_products.return_value = [_product()]
    resp = client.get("/api/product-delivery/products")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_get_backlog_unknown_product_is_404(client: TestClient, store: MagicMock) -> None:
    store.get_backlog_tree.return_value = None
    resp = client.get("/api/product-delivery/products/nope/backlog")
    assert resp.status_code == 404


def test_get_backlog_found(client: TestClient, store: MagicMock) -> None:
    from product_delivery import BacklogTree

    store.get_backlog_tree.return_value = BacklogTree(product=_product())
    resp = client.get("/api/product-delivery/products/p1/backlog")
    assert resp.status_code == 200
    assert resp.json()["product"]["id"] == "p1"


# ---------------------------------------------------------------------------
# Backlog hierarchy CRUD
# ---------------------------------------------------------------------------


def test_create_initiative(client: TestClient, store: MagicMock) -> None:
    store.create_initiative.return_value = Initiative(
        id="i1", author="tester", created_at=_NOW, updated_at=_NOW, product_id="p1", title="Init"
    )
    resp = client.post("/api/product-delivery/initiatives", json={"product_id": "p1", "title": "Init"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Init"


def test_create_epic(client: TestClient, store: MagicMock) -> None:
    store.create_epic.return_value = Epic(
        id="e1", author="tester", created_at=_NOW, updated_at=_NOW, initiative_id="i1", title="Epic"
    )
    resp = client.post("/api/product-delivery/epics", json={"initiative_id": "i1", "title": "Epic"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Epic"


def test_create_story(client: TestClient, store: MagicMock) -> None:
    store.create_story.return_value = Story(
        id="s1", author="tester", created_at=_NOW, updated_at=_NOW, epic_id="e1", title="Story"
    )
    resp = client.post("/api/product-delivery/stories", json={"epic_id": "e1", "title": "Story"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Story"


def test_create_task(client: TestClient, store: MagicMock) -> None:
    store.create_task.return_value = Task(
        id="t1", author="tester", created_at=_NOW, updated_at=_NOW, story_id="s1", title="Task"
    )
    resp = client.post("/api/product-delivery/tasks", json={"story_id": "s1", "title": "Task"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Task"


def test_create_acceptance_criterion(client: TestClient, store: MagicMock) -> None:
    store.create_acceptance_criterion.return_value = AcceptanceCriterion(
        id="ac1", author="tester", created_at=_NOW, updated_at=_NOW, story_id="s1", text="Must work"
    )
    resp = client.post("/api/product-delivery/acceptance-criteria", json={"story_id": "s1", "text": "Must work"})
    assert resp.status_code == 200
    assert resp.json()["text"] == "Must work"


# ---------------------------------------------------------------------------
# Status / score patches
# ---------------------------------------------------------------------------


def test_patch_status_success(client: TestClient, store: MagicMock) -> None:
    store.update_status.return_value = True
    resp = client.patch("/api/product-delivery/story/s1/status", json={"status": "done"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "kind": "story", "id": "s1", "status": "done"}


def test_patch_status_unknown_entity_is_404(client: TestClient, store: MagicMock) -> None:
    store.update_status.return_value = False
    resp = client.patch("/api/product-delivery/story/nope/status", json={"status": "done"})
    assert resp.status_code == 404


def test_patch_scores_requires_at_least_one_score(client: TestClient, store: MagicMock) -> None:
    resp = client.patch("/api/product-delivery/story/s1/scores", json={})
    assert resp.status_code == 400
    store.update_scores.assert_not_called()


def test_patch_scores_success(client: TestClient, store: MagicMock) -> None:
    store.update_scores.return_value = True
    resp = client.patch("/api/product-delivery/story/s1/scores", json={"wsjf_score": 4.2})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "kind": "story", "id": "s1"}


def test_patch_scores_unknown_entity_is_404(client: TestClient, store: MagicMock) -> None:
    store.update_scores.return_value = False
    resp = client.patch("/api/product-delivery/story/nope/scores", json={"rice_score": 1.0})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Feedback intake
# ---------------------------------------------------------------------------


def test_create_feedback(client: TestClient, store: MagicMock) -> None:
    store.create_feedback_item.return_value = FeedbackItem(
        id="f1", author="tester", created_at=_NOW, updated_at=_NOW, product_id="p1", source="support"
    )
    resp = client.post("/api/product-delivery/feedback", json={"product_id": "p1", "source": "support"})
    assert resp.status_code == 200
    assert resp.json()["source"] == "support"


def test_list_feedback(client: TestClient, store: MagicMock) -> None:
    store.list_feedback.return_value = []
    resp = client.get("/api/product-delivery/feedback", params={"product_id": "p1", "status": "open"})
    assert resp.status_code == 200
    assert resp.json() == []
    store.list_feedback.assert_called_once_with("p1", status="open")


def test_patch_feedback_link(client: TestClient, store: MagicMock) -> None:
    store.update_feedback_link.return_value = FeedbackItem(
        id="f1",
        author="tester",
        created_at=_NOW,
        updated_at=_NOW,
        product_id="p1",
        source="support",
        linked_story_id="s1",
    )
    resp = client.patch("/api/product-delivery/feedback/f1/link", json={"linked_story_id": "s1"})
    assert resp.status_code == 200
    assert resp.json()["linked_story_id"] == "s1"


def test_patch_feedback_link_cross_product_is_400(client: TestClient, store: MagicMock) -> None:
    store.update_feedback_link.side_effect = CrossProductFeedbackLink("mismatch")
    resp = client.patch("/api/product-delivery/feedback/f1/link", json={"linked_story_id": "s1"})
    assert resp.status_code == 400


def test_patch_feedback_link_unknown_is_404(client: TestClient, store: MagicMock) -> None:
    store.update_feedback_link.side_effect = UnknownProductDeliveryEntity("nope")
    resp = client.patch("/api/product-delivery/feedback/f1/link", json={"linked_story_id": "s1"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Sprints
# ---------------------------------------------------------------------------


def _sprint(**overrides) -> Sprint:
    base = {"id": "sp1", "author": "tester", "created_at": _NOW, "updated_at": _NOW, "product_id": "p1", "name": "S1"}
    base.update(overrides)
    return Sprint(**base)


def test_create_sprint(client: TestClient, store: MagicMock) -> None:
    store.create_sprint.return_value = _sprint()
    resp = client.post("/api/product-delivery/sprints", json={"product_id": "p1", "name": "S1"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "S1"


def test_list_sprints(client: TestClient, store: MagicMock) -> None:
    store.list_sprints_for_product.return_value = [_sprint()]
    resp = client.get("/api/product-delivery/sprints", params={"product_id": "p1"})
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_get_sprint_found(client: TestClient, store: MagicMock) -> None:
    store.get_sprint_with_stories.return_value = SprintWithStories(sprint=_sprint())
    resp = client.get("/api/product-delivery/sprints/sp1")
    assert resp.status_code == 200
    assert resp.json()["sprint"]["id"] == "sp1"


def test_get_sprint_unknown_is_404(client: TestClient, store: MagicMock) -> None:
    store.get_sprint_with_stories.return_value = None
    resp = client.get("/api/product-delivery/sprints/nope")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------


def test_create_release_success(client: TestClient, store: MagicMock) -> None:
    store.count_open_stories_in_sprint.return_value = 0
    store.create_release.return_value = Release(
        id="r1", author="tester", created_at=_NOW, updated_at=_NOW, sprint_id="sp1", version="1.0.0"
    )
    resp = client.post("/api/product-delivery/releases", json={"sprint_id": "sp1", "version": "1.0.0"})
    assert resp.status_code == 200
    assert resp.json()["version"] == "1.0.0"


def test_create_release_blocked_when_sprint_not_complete(client: TestClient, store: MagicMock) -> None:
    store.count_open_stories_in_sprint.return_value = 3
    resp = client.post("/api/product-delivery/releases", json={"sprint_id": "sp1", "version": "1.0.0"})
    assert resp.status_code == 409
    store.create_release.assert_not_called()


def test_list_releases(client: TestClient, store: MagicMock) -> None:
    store.list_releases_for_product.return_value = []
    resp = client.get("/api/product-delivery/releases", params={"product_id": "p1"})
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# register_pd_exception_handlers: exercise the remaining mapped exceptions
# so every entry in _EXC_STATUS is proven to reach its status code.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls,status_code",
    [(cls, code) for cls, code in routes_mod._EXC_STATUS.items()],
)
def test_every_registered_exception_maps_to_its_status_code(exc_cls, status_code) -> None:
    app = FastAPI()
    routes_mod.register_pd_exception_handlers(app)

    @app.get("/boom")
    def _boom():
        raise exc_cls("test failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/boom")
    assert resp.status_code == status_code
    assert resp.json() == {"detail": "test failure"}


def test_sprint_not_complete_is_409_via_route(client: TestClient, store: MagicMock) -> None:
    """SprintNotComplete raised directly by the route (not the store) also maps to 409."""
    store.count_open_stories_in_sprint.side_effect = SprintNotComplete("blocked")
    resp = client.post("/api/product-delivery/releases", json={"sprint_id": "sp1", "version": "1.0.0"})
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# /groom and /sprints/{id}/plan: agent classes stubbed (real llm_service /
# scoring logic is exercised by product_delivery's own test suite, not here)
# ---------------------------------------------------------------------------


def test_groom_route_delegates_to_product_owner_agent(client: TestClient, store: MagicMock, monkeypatch) -> None:
    from product_delivery import GroomResult

    fake_agent = MagicMock()
    fake_agent.groom.return_value = GroomResult(product_id="p1", method="wsjf")
    fake_agent_cls = MagicMock(return_value=fake_agent)
    monkeypatch.setattr(routes_mod, "ProductOwnerAgent", fake_agent_cls)

    resp = client.post("/api/product-delivery/groom", json={"product_id": "p1", "method": "wsjf"})

    assert resp.status_code == 200
    assert resp.json()["product_id"] == "p1"
    fake_agent.groom.assert_called_once_with(product_id="p1", method="wsjf", persist=True)
    # The agent is constructed with the route's store and a factory (not a client).
    _, kwargs = fake_agent_cls.call_args
    assert kwargs["store"] is store
    assert callable(kwargs["llm_factory"])


def test_plan_sprint_route_delegates_to_sprint_planner_agent(client: TestClient, store: MagicMock, monkeypatch) -> None:
    from product_delivery import SprintPlanResult

    fake_agent = MagicMock()
    fake_agent.plan.return_value = SprintPlanResult(sprint_id="sp1")
    fake_agent_cls = MagicMock(return_value=fake_agent)
    monkeypatch.setattr(routes_mod, "SprintPlannerAgent", fake_agent_cls)

    resp = client.post("/api/product-delivery/sprints/sp1/plan", json={"capacity_points": 12.5})

    assert resp.status_code == 200
    assert resp.json()["sprint_id"] == "sp1"
    fake_agent.plan.assert_called_once_with(sprint_id="sp1", capacity_points=12.5)


def test_llm_client_factory_bootstraps_client_via_llm_service(monkeypatch) -> None:
    fake_client = object()
    fake_get_client = MagicMock(return_value=fake_client)
    fake_llm_service = MagicMock(get_client=fake_get_client)
    monkeypatch.setitem(sys.modules, "llm_service", fake_llm_service)

    result = routes_mod._llm_client_factory()

    assert result is fake_client
    fake_get_client.assert_called_once_with("product_owner")


def test_plan_sprint_route_without_body_uses_stored_capacity(client: TestClient, store: MagicMock, monkeypatch) -> None:
    from product_delivery import SprintPlanResult

    fake_agent = MagicMock()
    fake_agent.plan.return_value = SprintPlanResult(sprint_id="sp1")
    monkeypatch.setattr(routes_mod, "SprintPlannerAgent", MagicMock(return_value=fake_agent))

    resp = client.post("/api/product-delivery/sprints/sp1/plan")

    assert resp.status_code == 200
    fake_agent.plan.assert_called_once_with(sprint_id="sp1", capacity_points=None)
