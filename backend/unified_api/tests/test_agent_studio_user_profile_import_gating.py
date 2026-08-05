"""Regression: ``agent_studio`` and ``user_profile`` must stay out of the module
graph when disabled.

Sibling of ``test_product_delivery_import_gating.py`` (issue #3828). Verifying
issue #3829 found that, unlike ``product_delivery``, both remaining in-process
teams had a real gap:

* ``agent_studio``'s router mount was already correctly gated in
  ``unified_api/main.py``, but its lifespan Postgres schema-registration import
  (``from agent_studio.postgres import SCHEMA``) ran unconditionally, regardless
  of ``TEAM_CONFIGS["agent_studio"].enabled``.
* ``user_profile`` had two leaks: the router import itself
  (``from unified_api.routes.user_profile import router``) sat at module scope
  outside any ``if enabled:`` guard (only the ``app.include_router(...)`` call
  was gated), and its lifespan schema-registration import had the same
  unconditional shape as agent_studio's.

Both are now fixed in ``main.py`` to match ``product_delivery``'s pattern:
gate the *import statement* itself (not just the route mount / DDL call)
behind the team's `enabled` flag, for both the module-scope router import and
the lifespan's schema-registration import.

Same subprocess convention as ``test_product_delivery_import_gating.py`` /
``test_agents_route_lazy_temporal_import.py``: other test modules in this
suite already ``import unified_api.main``, so a same-process ``sys.modules``
check would be unreliable — each test spawns a fresh interpreter. The lifespan
gate is exercised by entering it via ``TestClient(app)`` (as
``test_team_proxy_stream.py`` already does) before asserting ``sys.modules``.
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
        ``sys.path`` (so ``unified_api``, ``agent_studio``, and ``user_profile`` resolve).
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


def test_agent_studio_not_imported_when_disabled() -> None:
    """Disabling agent_studio must keep it out of ``sys.modules`` on unified-api
    import *and* through a full ASGI lifespan run (the schema-registration gate).

    Preconditions: a fresh interpreter whose PYTHONPATH includes ``backend`` + ``agents``.
    Postconditions: subprocess exits 0; assertions inside the child pass.
    """
    script = """
import sys
import unified_api.config as config
config.TEAM_CONFIGS["agent_studio"].enabled = False
assert "agent_studio" not in sys.modules, "agent_studio already loaded before import"
import unified_api.main
assert "agent_studio" not in sys.modules, (
    f"disabled agent_studio was imported by unified_api.main: "
    f"{[m for m in sys.modules if m == 'agent_studio' or m.startswith('agent_studio.')]}"
)
assert "unified_api.routes.agent_studio" not in sys.modules, (
    "disabled agent_studio: unified_api.routes.agent_studio was still imported"
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
    _has_prefix(r, "/api/agent-studio") for r in unified_api.main.app.routes
), "disabled agent_studio route was still mounted"

from fastapi.testclient import TestClient

with TestClient(unified_api.main.app):
    pass

assert "agent_studio" not in sys.modules, (
    f"disabled agent_studio was imported during ASGI lifespan startup: "
    f"{[m for m in sys.modules if m == 'agent_studio' or m.startswith('agent_studio.')]}"
)
assert "agent_studio.postgres" not in sys.modules, (
    "disabled agent_studio: lifespan schema registration still imported agent_studio.postgres"
)
print("ok")
"""
    result = _run(script)
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout


def test_agent_studio_imported_when_enabled() -> None:
    """Default config (enabled) must preserve today's behavior: module loaded,
    route mounted, and the lifespan's schema-registration import still runs.

    Preconditions: a fresh interpreter whose PYTHONPATH includes ``backend`` + ``agents``.
    Postconditions: subprocess exits 0; assertions inside the child pass.
    """
    script = """
import sys
import unified_api.main
assert "agent_studio" in sys.modules, "enabled agent_studio was not imported"
assert "unified_api.routes.agent_studio" in sys.modules, (
    "enabled agent_studio: unified_api.routes.agent_studio was not imported"
)
def _has_prefix(route, prefix):
    inner_routes = getattr(getattr(route, "original_router", None), "routes", None)
    if inner_routes is not None:
        return any(getattr(r, "path", "").startswith(prefix) for r in inner_routes)
    return getattr(route, "path", "").startswith(prefix)

assert any(
    _has_prefix(r, "/api/agent-studio") for r in unified_api.main.app.routes
), "enabled agent_studio route was not mounted"

from fastapi.testclient import TestClient

with TestClient(unified_api.main.app):
    pass

assert "agent_studio.postgres" in sys.modules, (
    "enabled agent_studio: lifespan schema registration did not import agent_studio.postgres"
)
print("ok")
"""
    result = _run(script)
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout


def test_user_profile_not_imported_when_disabled() -> None:
    """Disabling user_profile must keep it out of ``sys.modules`` on unified-api
    import *and* through a full ASGI lifespan run — covers both leaks fixed for
    this team: the module-scope router import and the lifespan schema import.

    Preconditions: a fresh interpreter whose PYTHONPATH includes ``backend`` + ``agents``.
    Postconditions: subprocess exits 0; assertions inside the child pass.
    """
    script = """
import sys
import unified_api.config as config
config.TEAM_CONFIGS["user_profile"].enabled = False
assert "user_profile" not in sys.modules, "user_profile already loaded before import"
import unified_api.main
assert "user_profile" not in sys.modules, (
    f"disabled user_profile was imported by unified_api.main: "
    f"{[m for m in sys.modules if m == 'user_profile' or m.startswith('user_profile.')]}"
)
assert "unified_api.routes.user_profile" not in sys.modules, (
    "disabled user_profile: unified_api.routes.user_profile was still imported"
)
def _has_prefix(route, prefix):
    inner_routes = getattr(getattr(route, "original_router", None), "routes", None)
    if inner_routes is not None:
        return any(getattr(r, "path", "").startswith(prefix) for r in inner_routes)
    return getattr(route, "path", "").startswith(prefix)

assert not any(
    _has_prefix(r, "/api/user-profile") for r in unified_api.main.app.routes
), "disabled user_profile route was still mounted"

from fastapi.testclient import TestClient

with TestClient(unified_api.main.app):
    pass

assert "user_profile" not in sys.modules, (
    f"disabled user_profile was imported during ASGI lifespan startup: "
    f"{[m for m in sys.modules if m == 'user_profile' or m.startswith('user_profile.')]}"
)
assert "user_profile.postgres" not in sys.modules, (
    "disabled user_profile: lifespan schema registration still imported user_profile.postgres"
)
print("ok")
"""
    result = _run(script)
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout


def test_user_profile_imported_when_enabled() -> None:
    """Default config (enabled) must preserve today's behavior: module loaded,
    route mounted, and the lifespan's schema-registration import still runs.

    Preconditions: a fresh interpreter whose PYTHONPATH includes ``backend`` + ``agents``.
    Postconditions: subprocess exits 0; assertions inside the child pass.
    """
    script = """
import sys
import unified_api.main
assert "user_profile" in sys.modules, "enabled user_profile was not imported"
assert "unified_api.routes.user_profile" in sys.modules, (
    "enabled user_profile: unified_api.routes.user_profile was not imported"
)
def _has_prefix(route, prefix):
    inner_routes = getattr(getattr(route, "original_router", None), "routes", None)
    if inner_routes is not None:
        return any(getattr(r, "path", "").startswith(prefix) for r in inner_routes)
    return getattr(route, "path", "").startswith(prefix)

assert any(
    _has_prefix(r, "/api/user-profile") for r in unified_api.main.app.routes
), "enabled user_profile route was not mounted"

from fastapi.testclient import TestClient

with TestClient(unified_api.main.app):
    pass

assert "user_profile.postgres" in sys.modules, (
    "enabled user_profile: lifespan schema registration did not import user_profile.postgres"
)
print("ok")
"""
    result = _run(script)
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout
