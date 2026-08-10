"""Unit tests for route_serves_prefix's own contract (see _route_gating.py).

These exercise scenarios none of the three real in-process teams currently
trigger (a router nested via router.include_router(...), and a sibling
prefix that is a raw string-prefix of another), so they're the only
regression coverage for _route_gating.route_serves_prefix itself: the
subprocess-based gating tests only ever probe today's flat router shape.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from unified_api.tests._route_gating import route_serves_prefix


def test_flat_router_matches_its_own_prefix() -> None:
    router = APIRouter(prefix="/api/agent-studio")

    @router.get("/agents")
    def _agents():  # pragma: no cover - never called, route object only
        return {}

    app = FastAPI()
    app.include_router(router)

    assert any(route_serves_prefix(r, "/api/agent-studio") for r in app.routes)


def test_flat_router_does_not_match_unrelated_prefix() -> None:
    router = APIRouter(prefix="/api/agent-studio")

    @router.get("/agents")
    def _agents():  # pragma: no cover
        return {}

    app = FastAPI()
    app.include_router(router)

    assert not any(route_serves_prefix(r, "/api/user-profile") for r in app.routes)


def test_nested_include_router_is_found_via_recursion() -> None:
    """A router built from router.include_router(sub) before being included
    on the app wraps to two levels of FastAPI's private _IncludedRouter —
    route_serves_prefix must recurse through both, not just one."""
    sub = APIRouter(prefix="/agents")

    @sub.get("/from-registry/{agent_id}")
    def _from_registry(agent_id: str):  # pragma: no cover
        return {}

    outer = APIRouter(prefix="/api/agent-studio")
    outer.include_router(sub)

    app = FastAPI()
    app.include_router(outer)

    assert any(route_serves_prefix(r, "/api/agent-studio") for r in app.routes)


def test_sibling_prefix_is_not_a_substring_false_positive() -> None:
    """ "/api/investment" must not match a sibling "/api/investment-strategy-lab"
    route — an exact path-segment match, not a raw string prefix match."""
    router = APIRouter(prefix="/api/investment-strategy-lab")

    @router.get("/runs")
    def _runs():  # pragma: no cover
        return {}

    app = FastAPI()
    app.include_router(router)

    assert not any(route_serves_prefix(r, "/api/investment") for r in app.routes)


def test_exact_bare_prefix_path_matches() -> None:
    """A route mounted at exactly the prefix (no further suffix) must match —
    not just "prefix + /..." — matching user_profile's bare "/api/user-profile"
    GET/POST routes."""
    router = APIRouter(prefix="/api/user-profile")

    @router.get("")
    def _bare():  # pragma: no cover
        return {}

    app = FastAPI()
    app.include_router(router)

    assert any(route_serves_prefix(r, "/api/user-profile") for r in app.routes)


def test_plain_mount_without_original_router_falls_back_to_path() -> None:
    """A route type this FastAPI-specific wrapping never touches (e.g. a
    Starlette Mount, as the team-assistant lazy-mount path uses) must still
    match via its own .path — the non-_IncludedRouter fallback."""
    from starlette.routing import Mount

    def _asgi_app(scope, receive, send):  # pragma: no cover
        raise NotImplementedError

    mount = Mount("/assistants/blogging", app=_asgi_app)

    assert route_serves_prefix(mount, "/assistants/blogging")
    assert not route_serves_prefix(mount, "/assistants/other-team")
