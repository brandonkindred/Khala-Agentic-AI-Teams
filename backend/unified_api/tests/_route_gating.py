"""Shared route-mount-detection helper for the in-process-team import-gating
regression tests (agent_studio / user_profile / product_delivery).

FastAPI 0.137+ wraps every ``app.include_router(...)`` target in a private
``fastapi.routing._IncludedRouter`` that carries no ``.path`` attribute, so
naively reading ``route.path`` off ``app.routes`` can no longer tell whether
a given team's router was mounted. This is the single source of truth for
that check, shared by every subprocess script that needs it instead of each
redefining its own (divergence-prone) copy.
"""

from __future__ import annotations

from collections.abc import Iterator


def _matches(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def yield_leaf_routes(route: object) -> Iterator[object]:
    """Yield the leaf route-like object(s) *route* resolves to.

    For a FastAPI 0.137+ ``_IncludedRouter`` (what ``app.routes`` entries
    become for every ``include_router(...)`` target — it has no ``.path`` of
    its own), delegates to its own ``effective_route_contexts()``, which is
    FastAPI's own mechanism for resolving each leaf route's final absolute
    path/methods/response_model through arbitrarily many levels of nested
    ``include_router(...)`` calls — used internally for OpenAPI generation,
    so it already correctly solves the general case rather than this helper
    reimplementing prefix accumulation (and risking getting it wrong the way
    a naive one-level ``original_router.routes`` unwrap does). Falls back to
    the route itself for route types that have no ``effective_route_contexts``
    (e.g. a plain Starlette ``Mount``/``Route`` appended directly to
    ``app.routes``, as the team-assistant lazy-mount path does, or any other
    already-unwrapped route) — unaffected by any FastAPI version's
    ``include_router`` changes.

    Preconditions:
        - None; any ``app.routes`` entry (or nested route object) is accepted.
    Postconditions:
        - Yields at least one object. Never raises.
        - Depends on ``effective_route_contexts`` — a real but non-underscored
          method on FastAPI's private ``_IncludedRouter``, remaining stable;
          it is the same mechanism FastAPI's own OpenAPI schema generation
          relies on, so it is unlikely to be removed without a replacement,
          but a future FastAPI release could still rename or restructure it.
    """
    effective_route_contexts = getattr(route, "effective_route_contexts", None)
    if effective_route_contexts is not None:
        yield from effective_route_contexts()
    else:
        yield route


def route_serves_prefix(route: object, prefix: str) -> bool:
    """Return whether *route* (an ``app.routes`` entry) serves any path under *prefix*.

    Preconditions:
        - ``prefix`` is a non-empty path prefix with no trailing slash (e.g.
          ``"/api/agent-studio"``).
    Postconditions:
        - Returns True iff *route* is, or (at any nesting depth, and
          regardless of whether each level's prefix was supplied via
          ``APIRouter(prefix=...)`` at construction or ``include_router(...,
          prefix=...)`` at the call site) wraps, a leaf route whose final
          resolved path equals *prefix* or starts with ``prefix + "/"`` — an
          exact path-segment match, not a raw substring match, so e.g.
          ``"/api/investment"`` does not false-positive-match a sibling
          ``"/api/investment-strategy-lab"`` route.
        - Leaf resolution (including the ``_IncludedRouter`` unwrapping) is
          delegated to :func:`yield_leaf_routes` — see its docstring.
        - Never raises: a leaf with neither ``effective_route_contexts`` nor
          ``.path`` contributes no match rather than erroring.
    """
    return any(_matches(getattr(leaf, "path", "") or "", prefix) for leaf in yield_leaf_routes(route))
