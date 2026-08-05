"""Regression: ``product_delivery`` must stay out of the module graph when disabled.

``unified_api/main.py`` gates both product_delivery import sites behind
``TEAM_CONFIGS["product_delivery"].enabled``:

* the router import at module scope (``unified_api.routes.product_delivery``,
  pulled in when ``app.include_router(...)`` mounts it), and
* the Postgres schema-registration import inside the ``lifespan()`` async
  generator (``from product_delivery.postgres import SCHEMA``), which only
  executes once the ASGI lifespan actually runs.

Disabling the team must keep ``product_delivery`` (models, store,
``ReleaseManagerAgent``, etc.) out of ``sys.modules`` entirely — not just
unmounted from the app. Because other test modules in this suite already
``import unified_api.main`` (populating ``sys.modules`` for the rest of the
pytest process), the only reliable way to observe this is a fresh subprocess,
per the same pattern used by ``test_agents_route_lazy_temporal_import.py`` —
extended here to also enter the app's lifespan via ``TestClient`` (as
``test_team_proxy_stream.py`` already does) so the schema-registration gate
is exercised too, not just the router-mount gate.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_AGENTS_ROOT = _BACKEND_ROOT / "agents"


def _subprocess_pythonpath() -> str:
    """Build PYTHONPATH matching ``backend/pytest.ini`` (``agents`` + backend root).

    Preconditions: ``_BACKEND_ROOT`` and ``_AGENTS_ROOT`` exist.
    Postconditions: returned string puts the ``agents`` and ``backend`` roots on
        ``sys.path`` (so ``unified_api`` and ``product_delivery`` resolve).
    """
    assert _BACKEND_ROOT.is_dir()
    assert _AGENTS_ROOT.is_dir()
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(_AGENTS_ROOT), str(_BACKEND_ROOT)]
    if existing:
        parts.append(existing)
    return os.pathsep.join(parts)


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": _subprocess_pythonpath()},
        capture_output=True,
        text=True,
        check=False,
    )


def test_product_delivery_not_imported_when_disabled() -> None:
    """Disabling the team must keep it out of ``sys.modules`` on unified-api import
    *and* through a full ASGI lifespan run.

    ``unified_api.main`` module import only exercises the router-mount gate
    (``main.py`` around ``if TEAM_CONFIGS["product_delivery"].enabled:`` guarding
    the ``unified_api.routes.product_delivery`` import). The *other*
    product_delivery import — the lifespan's Postgres schema registration
    (``from product_delivery.postgres import SCHEMA``) — only runs once the
    ASGI lifespan actually executes, so this test enters it via
    ``TestClient(app)`` (the same pattern ``test_team_proxy_stream.py`` uses)
    before asserting ``sys.modules`` absence, covering both gates.

    Preconditions: a fresh interpreter whose PYTHONPATH includes ``backend`` + ``agents``.
    Postconditions: subprocess exits 0; assertions inside the child pass.
    """
    script = """
import sys
import unified_api.config as config
config.TEAM_CONFIGS["product_delivery"].enabled = False
assert "product_delivery" not in sys.modules, "product_delivery already loaded before import"
import unified_api.main
assert "product_delivery" not in sys.modules, (
    f"disabled product_delivery was imported by unified_api.main: "
    f"{[m for m in sys.modules if m == 'product_delivery' or m.startswith('product_delivery.')]}"
)
assert "unified_api.routes.product_delivery" not in sys.modules, (
    "disabled product_delivery: unified_api.routes.product_delivery was still imported"
)
def _has_prefix(route, prefix):
    # FastAPI 0.137+ wraps app.include_router()'s target in a private
    # _IncludedRouter with no .path attribute; the fully-prefixed leaf paths
    # live on original_router.routes instead. Falls back to route.path for
    # unwrapped route types (e.g. Mount), which this wrapping doesn't touch.
    inner_routes = getattr(getattr(route, "original_router", None), "routes", None)
    if inner_routes is not None:
        return any(getattr(r, "path", "").startswith(prefix) for r in inner_routes)
    return getattr(route, "path", "").startswith(prefix)

assert not any(
    _has_prefix(r, "/api/product-delivery") for r in unified_api.main.app.routes
), "disabled product_delivery route was still mounted"

from fastapi.testclient import TestClient

with TestClient(unified_api.main.app):
    pass

assert "product_delivery" not in sys.modules, (
    f"disabled product_delivery was imported during ASGI lifespan startup: "
    f"{[m for m in sys.modules if m == 'product_delivery' or m.startswith('product_delivery.')]}"
)
assert "product_delivery.postgres" not in sys.modules, (
    "disabled product_delivery: lifespan schema registration still imported product_delivery.postgres"
)
print("ok")
"""
    result = _run(script)
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout


def test_product_delivery_imported_when_enabled() -> None:
    """Default config (enabled) must preserve today's behavior: module loaded,
    route mounted, and the lifespan's schema-registration import still runs.

    Preconditions: a fresh interpreter whose PYTHONPATH includes ``backend`` + ``agents``.
    Postconditions: subprocess exits 0; assertions inside the child pass.
    """
    script = """
import sys
import unified_api.main
assert "product_delivery" in sys.modules, "enabled product_delivery was not imported"
assert "unified_api.routes.product_delivery" in sys.modules, (
    "enabled product_delivery: unified_api.routes.product_delivery was not imported"
)
def _has_prefix(route, prefix):
    inner_routes = getattr(getattr(route, "original_router", None), "routes", None)
    if inner_routes is not None:
        return any(getattr(r, "path", "").startswith(prefix) for r in inner_routes)
    return getattr(route, "path", "").startswith(prefix)

assert any(
    _has_prefix(r, "/api/product-delivery") for r in unified_api.main.app.routes
), "enabled product_delivery route was not mounted"

from fastapi.testclient import TestClient

with TestClient(unified_api.main.app):
    pass

assert "product_delivery.postgres" in sys.modules, (
    "enabled product_delivery: lifespan schema registration did not import product_delivery.postgres"
)
print("ok")
"""
    result = _run(script)
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout
