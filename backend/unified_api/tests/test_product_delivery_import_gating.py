"""Regression: ``product_delivery`` must stay out of the module graph when disabled.

``unified_api/main.py`` gates the ``product_delivery`` router import (and its
Postgres schema registration) behind ``TEAM_CONFIGS["product_delivery"].enabled``
so that disabling the team via config keeps ``product_delivery`` (models, store,
``ReleaseManagerAgent``, etc.) out of ``sys.modules`` entirely — not just unmounted
from the app. Because other test modules in this suite already ``import
unified_api.main`` (populating ``sys.modules`` for the rest of the pytest process),
the only reliable way to observe this is a fresh subprocess, per the same pattern
used by ``test_agents_route_lazy_temporal_import.py``.
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
    """Disabling the team must keep it out of ``sys.modules`` on unified-api import.

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
assert not any(
    getattr(r, "path", "").startswith("/api/product-delivery") for r in unified_api.main.app.routes
), "disabled product_delivery route was still mounted"
print("ok")
"""
    result = _run(script)
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout


def test_product_delivery_imported_when_enabled() -> None:
    """Default config (enabled) must preserve today's behavior: module loaded, route mounted.

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
assert any(
    getattr(r, "path", "").startswith("/api/product-delivery") for r in unified_api.main.app.routes
), "enabled product_delivery route was not mounted"
print("ok")
"""
    result = _run(script)
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout
