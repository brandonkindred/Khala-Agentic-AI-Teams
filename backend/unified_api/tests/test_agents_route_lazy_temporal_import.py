"""Regression: importing ``unified_api.routes.agents`` must not load sandbox Temporal.

``routes/agents.py`` only needs the Temporal-aware sandbox acquire dispatch
(``agent_team_studio.agent_provisioning_team.temporal.sandbox_dispatch``, which pulls in
``temporalio``) inside the ``invoke_agent`` handler's warm-sandbox step. Request
paths that never touch a sandbox — registry listing, schema resolution,
samples, run history — must not pay for that import graph at module-import
time, i.e. on every cold boot.
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
        ``sys.path`` (so ``unified_api`` and ``agent_provisioning_team`` resolve).
    """
    assert _BACKEND_ROOT.is_dir()
    assert _AGENTS_ROOT.is_dir()
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(_AGENTS_ROOT), str(_BACKEND_ROOT)]
    if existing:
        parts.append(existing)
    return os.pathsep.join(parts)


def test_import_agents_route_does_not_load_sandbox_temporal_dispatch() -> None:
    """``import unified_api.routes.agents`` must leave ``temporalio`` unloaded.

    The invoke handler resolves the Temporal dispatch lazily via the
    module-level ``acquire`` wrapper, which must still exist (and be callable)
    so route tests can monkeypatch it, without itself importing
    ``sandbox_dispatch``/``temporalio`` at definition time.

    Preconditions: a fresh interpreter whose PYTHONPATH includes ``backend`` + ``agents``.
    Postconditions: subprocess exits 0; assertions inside the child pass.
    """
    script = """
import sys
assert "temporalio" not in sys.modules, "temporalio already loaded before import"
assert "agent_team_studio.agent_provisioning_team.temporal.sandbox_dispatch" not in sys.modules, (
    "sandbox_dispatch already loaded before import"
)
import unified_api.routes.agents as agents_route_mod
assert "temporalio" not in sys.modules, (
    f"importing unified_api.routes.agents loaded temporalio: "
    f"{[m for m in sys.modules if m.startswith('temporalio')]}"
)
assert "agent_team_studio.agent_provisioning_team.temporal.sandbox_dispatch" not in sys.modules, (
    "importing unified_api.routes.agents loaded sandbox_dispatch"
)
assert callable(agents_route_mod.acquire), (
    "acquire must remain a callable module-level attribute for route tests to monkeypatch"
)
print("ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": _subprocess_pythonpath()},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout
